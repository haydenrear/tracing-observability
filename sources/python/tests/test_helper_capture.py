"""Owned helper capture can be disabled without disturbing external tracing."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tracing_skill_observability import configure_helper_capture, inject_trace_context, extract_trace_context, span, traced_span
from tracing_skill_observability import tracing

@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(tracing, '_helper_capture_enabled', True)
    monkeypatch.setattr(tracing, '_helper_capture_started', False)

@pytest.mark.parametrize('value', [0,1,None,'false',[],object()])
def test_strict_boolean(value):
    with pytest.raises(TypeError):configure_helper_capture(enabled=value)

def test_decoration_does_not_freeze_and_disabled_use_does():
    @traced_span
    def business(value):return value
    configure_helper_capture(enabled=False)
    assert business(42)==42
    configure_helper_capture(enabled=False)
    with pytest.raises(RuntimeError):configure_helper_capture(enabled=True)

def test_disabled_external_context_transport_and_no_capture(monkeypatch):
    provider=TracerProvider();exporter=InMemorySpanExporter();provider.add_span_processor(SimpleSpanProcessor(exporter))
    def forbidden(*a,**kw):raise AssertionError('owned capture touched')
    for name in ('get_tracer','_span_error_mode_for_operation','_record_trace_correlation','_record_span_error'):
        monkeypatch.setattr(tracing,name,forbidden)
    configure_helper_capture(enabled=False)
    original=ValueError('original')
    @traced_span('business')
    def business():
        assert trace.get_current_span() is parent
        with span('inner') as inactive:
            assert inactive is trace.INVALID_SPAN
            assert trace.get_current_span() is parent
        carrier=inject_trace_context({})
        extracted=trace.get_current_span(extract_trace_context(carrier)).get_span_context()
        assert extracted.trace_id==parent.get_span_context().trace_id
        raise original
    with provider.get_tracer('external').start_as_current_span('parent') as parent:
        with pytest.raises(ValueError) as caught:business()
        assert caught.value is original
        assert trace.get_current_span() is parent
        assert parent.is_recording()
    assert len(exporter.get_finished_spans())==1
    assert not exporter.get_finished_spans()[0].events
    provider.shutdown()

def test_disabled_async_exception_and_cancellation(monkeypatch):
    configure_helper_capture(enabled=False)
    monkeypatch.setattr(tracing,'get_tracer',lambda *a:pytest.fail('tracer lookup'))
    original=RuntimeError('business')
    @traced_span
    async def work(fail=False):
        if fail:raise original
        await asyncio.Event().wait()
    async def run():
        with pytest.raises(RuntimeError) as caught:await work(True)
        assert caught.value is original
        task=asyncio.create_task(work());await asyncio.sleep(0);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())

def test_enabled_first_use_and_configuration_race_is_serialized(monkeypatch):
    monkeypatch.setattr(tracing,'get_tracer',lambda *a:trace.NoOpTracer())
    monkeypatch.setattr(tracing,'_record_trace_correlation',lambda:None)
    @traced_span
    def work():return 42
    barrier=Barrier(2)
    def configure():
        barrier.wait()
        try:configure_helper_capture(enabled=False);return 'disabled'
        except RuntimeError:return 'enabled'
    def invoke():barrier.wait();return work()
    with ThreadPoolExecutor(max_workers=2) as pool:
        config=pool.submit(configure);call=pool.submit(invoke)
        selected=config.result();assert call.result()==42
    assert tracing._helper_capture_started
    assert tracing._helper_capture_enabled is (selected=='enabled')
    configure_helper_capture(enabled=tracing._helper_capture_enabled)
    with pytest.raises(RuntimeError):configure_helper_capture(enabled=not tracing._helper_capture_enabled)
