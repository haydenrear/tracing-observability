"""Type-only errors protect helper-managed span data without changing business errors."""
import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from tracing_skill_observability import configure_span_errors, span, traced_span
from tracing_skill_observability import tracing

MARKER = "synthetic-sensitive-payload"


@pytest.fixture
def exported(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "get_tracer", lambda name=None: provider.get_tracer("policy"))
    monkeypatch.setattr(tracing, "_span_error_mode", "full")
    monkeypatch.setattr(tracing, "_span_error_policy_started", False)
    monkeypatch.setattr(tracing, "_record_trace_correlation", lambda: None)
    yield exporter, provider
    provider.shutdown()


def assert_type_only(exporter, error_type):
    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    recorded = finished[0]
    expected = error_type.__module__ + "." + error_type.__qualname__
    assert recorded.status.status_code == StatusCode.ERROR
    assert recorded.status.description == expected
    assert len(recorded.events) == 1
    assert dict(recorded.events[0].attributes) == {"exception.type": expected}
    assert MARKER not in recorded.to_json()
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("kind", ["sync", "async", "context"])
def test_type_only_omits_sensitive_message_stacktrace_and_status(exported, kind):
    exporter, _ = exported
    configure_span_errors(mode="type_only")
    original = RuntimeError(MARKER)
    @traced_span("failure")
    def sync_failure():
        raise original
    @traced_span("failure")
    async def async_failure():
        raise original
    with pytest.raises(RuntimeError) as caught:
        if kind == "sync":
            sync_failure()
        elif kind == "async":
            asyncio.run(async_failure())
        else:
            with span("failure"):
                raise original
    assert caught.value is original
    assert_type_only(exporter, RuntimeError)


@pytest.mark.parametrize("kind", ["sync", "async", "context"])
def test_type_only_never_formats_exception_value(exported, kind):
    exporter, _ = exported
    class NoFormatting(RuntimeError):
        def __str__(self):
            raise AssertionError("exception value must not be formatted")
        def __repr__(self):
            raise AssertionError("exception value must not be represented")
    configure_span_errors(mode="type_only")
    original = NoFormatting(MARKER)
    @traced_span("unformatted")
    def sync_failure():
        raise original
    @traced_span("unformatted")
    async def async_failure():
        raise original
    with pytest.raises(NoFormatting) as caught:
        if kind == "sync":sync_failure()
        elif kind == "async":asyncio.run(async_failure())
        else:
            with span("unformatted"):raise original
    assert caught.value is original
    assert_type_only(exporter, NoFormatting)


@pytest.mark.parametrize("kind", ["sync", "async", "context"])
def test_default_retains_legacy_full_exception_capture(exported, kind):
    exporter, _ = exported
    @traced_span("legacy")
    def sync_failure():raise RuntimeError(MARKER)
    @traced_span("legacy")
    async def async_failure():raise RuntimeError(MARKER)
    with pytest.raises(RuntimeError):
        if kind == "sync":sync_failure()
        elif kind == "async":asyncio.run(async_failure())
        else:
            with span("legacy"):raise RuntimeError(MARKER)
    recorded = exporter.get_finished_spans()[0]
    assert MARKER in recorded.to_json()
    assert "exception.stacktrace" in recorded.events[0].attributes


def test_policy_is_validated_and_immutable_after_first_shared_operation(exported):
    for mode in ["redact", "", None, True, []]:
        with pytest.raises(ValueError):configure_span_errors(mode=mode)
    configure_span_errors(mode="type_only")
    with span("success"):pass
    configure_span_errors(mode="type_only")
    with pytest.raises(RuntimeError):configure_span_errors(mode="full")
    recorded = exported[0].get_finished_spans()[0]
    assert not recorded.events
    assert recorded.status.status_code == StatusCode.UNSET


def test_raw_sdk_spans_are_explicitly_outside_shared_helper_policy(exported):
    exporter, provider = exported
    configure_span_errors(mode="type_only")
    with pytest.raises(RuntimeError):
        with provider.get_tracer("raw").start_as_current_span("raw-sdk"):
            raise RuntimeError(MARKER)
    assert MARKER in exporter.get_finished_spans()[0].to_json()


@pytest.mark.parametrize("kind", ["decorator", "context"])
def test_async_cancellation_restores_context_without_changing_legacy_classification(exported, kind):
    configure_span_errors(mode="type_only")
    @traced_span("cancel")
    async def cancelled():raise asyncio.CancelledError()
    async def context_cancelled():
        with span("cancel"):raise asyncio.CancelledError(MARKER)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled() if kind == "decorator" else context_cancelled())
    recorded = exported[0].get_finished_spans()[0]
    assert recorded.status.status_code == StatusCode.UNSET
    assert not recorded.events
    assert not trace.get_current_span().get_span_context().is_valid


def test_nested_shared_helpers_keep_both_spans_type_only(exported):
    exporter, _ = exported
    configure_span_errors(mode="type_only")
    original = RuntimeError(MARKER)
    @traced_span("inner-shared")
    def inner():raise original
    with pytest.raises(RuntimeError) as caught:
        with span("outer-shared"):inner()
    assert caught.value is original
    recorded = exporter.get_finished_spans()
    assert len(recorded) == 2
    assert all(MARKER not in item.to_json() for item in recorded)
    assert all(item.status.status_code == StatusCode.ERROR for item in recorded)
    assert all(len(item.events) == 1 for item in recorded)


def test_outer_raw_sdk_can_recapture_original_error_despite_safe_child(exported):
    exporter, provider = exported
    configure_span_errors(mode="type_only")
    original = RuntimeError(MARKER)
    @traced_span("safe-child")
    def inner():raise original
    with pytest.raises(RuntimeError) as caught:
        with provider.get_tracer("external").start_as_current_span("external-parent"):
            inner()
    assert caught.value is original
    recorded = {item.name:item for item in exporter.get_finished_spans()}
    assert MARKER not in recorded["safe-child"].to_json()
    assert MARKER in recorded["external-parent"].to_json()
