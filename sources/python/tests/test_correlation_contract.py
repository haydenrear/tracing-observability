"""Cross-operation context isolation and opt-in lifetime correlation budgets."""
import asyncio
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from tracing_skill_observability import (
    JsonLogFormatter, configure_trace_correlation, current_trace_id,
    extract_trace_context, inject_trace_context, traced_span,
)
from tracing_skill_observability import metrics as metrics_module, tracing


@pytest.fixture
def policy(monkeypatch):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics_module, 'get_meter', lambda *a: provider.get_meter('contract'))
    monkeypatch.setattr(metrics_module, '_trace_correlation_counter', None)
    monkeypatch.setattr(metrics_module, '_correlation_decisions_counter', None)
    monkeypatch.setattr(metrics_module, '_correlation_trace_ids', set())
    monkeypatch.setattr(metrics_module, '_correlation_policy', (True, False, None))
    monkeypatch.setattr(metrics_module, '_correlation_started', False)
    monkeypatch.setattr(metrics_module, '_correlation_configured', False)
    yield reader
    provider.shutdown()


def record(identifier, sampled=True):
    context = SpanContext(identifier, identifier, False, TraceFlags(1 if sampled else 0))
    with trace.use_span(NonRecordingSpan(context)):
        return metrics_module.record_trace_correlation()


def points(reader, name):
    data = reader.get_metrics_data()
    return [p for r in data.resource_metrics for s in r.scope_metrics
            for m in s.metrics if m.name == name for p in m.data.data_points]


def test_legacy_default_keeps_unsampled_trace_correlation(policy):
    assert record(1, sampled=False)
    assert [dict(p.attributes) for p in points(policy, 'tracing_observability.trace_correlation')] == [
        {'trace_id': '00000000000000000000000000000001'}]


def test_lifetime_budget_rejects_new_ids_but_keeps_existing_ids(policy):
    configure_trace_correlation(sampled_only=True, max_traces=2)
    assert not record(1, sampled=False)
    assert record(2) and record(3)
    assert not record(4)
    configure_trace_correlation(sampled_only=True, max_traces=2)
    assert record(2)
    assert len(points(policy, 'tracing_observability.trace_correlation')) == 2
    decisions = {p.attributes['reason']: p.value for p in points(policy, 'tracing_observability.trace_correlation_decisions')}
    assert decisions == {'selected': 3, 'unsampled': 1, 'budget_exhausted': 1}
    assert all(set(p.attributes) == {'decision', 'reason'} for p in points(policy, 'tracing_observability.trace_correlation_decisions'))
    with pytest.raises(RuntimeError):
        configure_trace_correlation(sampled_only=True, max_traces=3)
    with pytest.raises(RuntimeError):
        configure_trace_correlation(sampled_only=True, max_traces=2, enabled=False)


def test_zero_budget_and_disabled_policy_never_admit_a_trace(policy):
    configure_trace_correlation(max_traces=0)
    assert not record(1)
    assert points(policy, 'tracing_observability.trace_correlation') == []
    assert points(policy, 'tracing_observability.trace_correlation_decisions')[0].attributes['reason'] == 'budget_exhausted'


def test_disabled_policy_reports_suppression(policy):
    configure_trace_correlation(enabled=False)
    assert not record(1)
    assert points(policy, 'tracing_observability.trace_correlation') == []
    assert points(policy, 'tracing_observability.trace_correlation_decisions')[0].attributes['reason'] == 'disabled'


def test_saturation_is_atomic_across_threads(policy):
    configure_trace_correlation(max_traces=3)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(record, range(1, 51)))
    assert 0 < sum(outcomes) <= 3  # nonblocking contention may suppress a contender
    assert len(metrics_module._correlation_trace_ids) <= 3
    assert len(points(policy, 'tracing_observability.trace_correlation')) <= 3


@pytest.mark.parametrize('kwargs', [{'max_traces': -1}, {'max_traces': True}, {'max_traces': 1.5}, {'enabled': 1}, {'sampled_only': 'yes'}])
def test_invalid_policy_is_rejected_without_mutation(policy, kwargs):
    with pytest.raises(ValueError):
        configure_trace_correlation(**kwargs)
    assert metrics_module._correlation_policy == (True, False, None)


def test_policy_stats_failure_cannot_mask_selected_business_operation(policy, monkeypatch):
    configure_trace_correlation(max_traces=1)
    class BrokenCounter:
        def add(self, *args): raise RuntimeError('stats unavailable')
    monkeypatch.setattr(metrics_module, '_correlation_decisions_counter', BrokenCounter())
    assert record(1)
    assert not record(2)


def test_async_w3c_tracestate_logs_and_exception_cancel_isolation(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, 'get_tracer', lambda *a: provider.get_tracer('contract'))
    monkeypatch.setattr(tracing, '_record_trace_correlation', lambda: None)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.Logger('context-contract', level=logging.INFO)
    logger.addHandler(handler)
    initial = current_trace_id()
    ids = {name: f'{index:032x}' for index, name in enumerate(['ok', 'error', 'cancel'], 1)}

    async def scenario():
        arrived = 0
        ready = asyncio.Event()
        release = asyncio.Event()
        @traced_span('bounded.operation')
        async def operation(name):
            nonlocal arrived
            before = inject_trace_context()
            logger.info('before', extra={'branch': name})
            arrived += 1
            if arrived == 3: ready.set()
            await release.wait()
            after = inject_trace_context()
            assert before['traceparent'] == after['traceparent']
            assert before['tracestate'] == after['tracestate'] == 'vendor=value'
            assert current_trace_id() == ids[name]
            logger.info('after', extra={'branch': name})
            if name == 'error': raise ValueError('expected')
            if name == 'cancel': raise asyncio.CancelledError()
            return name
        async def branch(name):
            context = extract_trace_context({'traceparent': f'00-{ids[name]}-0000000000000009-01', 'tracestate': 'vendor=value'})
            with trace.use_span(trace.get_current_span(context)):
                try: return await operation(name)
                finally: assert current_trace_id() == ids[name]
        tasks = [asyncio.create_task(branch(name)) for name in ids]
        await asyncio.wait_for(ready.wait(), timeout=1)
        release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert outcomes[0] == 'ok' and isinstance(outcomes[1], ValueError)
        assert isinstance(outcomes[2], asyncio.CancelledError)
        assert current_trace_id() == initial
    try:
        asyncio.run(scenario())
        assert current_trace_id() == initial
        spans = {f'{s.context.trace_id:032x}': s for s in exporter.get_finished_spans()}
        assert set(spans) == set(ids.values())
        logs = [json.loads(line) for line in output.getvalue().splitlines()]
        assert len(logs) == 6
        for event in logs:
            active = spans[ids[event['branch']]]
            assert event['trace_id'] == ids[event['branch']]
            assert event['span_id'] == f'{active.context.span_id:016x}'
            assert active.parent.span_id == 9
    finally:
        provider.shutdown()


def test_failed_metric_add_does_not_recycle_lifetime_reservation(policy, monkeypatch):
    configure_trace_correlation(max_traces=1)
    class BrokenCounter:
        def add(self, *args): raise RuntimeError('export instrument unavailable')
    monkeypatch.setattr(metrics_module, '_trace_correlation_counter', BrokenCounter())
    assert not record(1)
    assert metrics_module._correlation_trace_ids == {1}
    assert not record(2)
    assert metrics_module._correlation_trace_ids == {1}
    reasons = {p.attributes['reason'] for p in points(policy, 'tracing_observability.trace_correlation_decisions')}
    assert reasons == {'error', 'budget_exhausted'}
