from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from inspect import iscoroutinefunction
from threading import Event, Lock, Thread
from typing import (
    Any,
    Callable,
    Iterator,
    Mapping,
    MutableMapping,
    ParamSpec,
    TypeVar,
    overload,
)

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ._resource import create_observability_resource

# The monitoring cluster maps its gateway ports to the Docker host, so the host
# view is the only one that works with no configuration at all. A pod reaches
# the same gateway at http://host.k3d.internal:4318, which the chart injects as
# OTEL_EXPORTER_OTLP_ENDPOINT.
DEFAULT_OTLP_ENDPOINT = "http://localhost:4318"
P = ParamSpec("P")
R = TypeVar("R")
_warned_default_endpoint = False
_tracer_provider: trace.TracerProvider | None = None
_tracer_provider_owned = False
_tracer_processor_configured = False
_tracer_provider_lock = Lock()
_w3c_propagator = TraceContextTextMapPropagator()
_trace_id_pattern = re.compile(r"[0-9a-f]{32}")
_span_error_mode = "full"
_span_error_policy_started = False
_span_error_policy_lock = Lock()
_helper_capture_enabled = True
_helper_capture_started = False
_helper_capture_lock = Lock()


def configure_helper_capture(*, enabled: bool = True) -> None:
    """Select helper capture before its first invocation; external SDKs are untouched.

    Decoration does not start capture. Repeating the selected value is idempotent,
    including after first use. A different value after first use is an error.
    """
    if type(enabled) is not bool:
        raise TypeError("enabled must be a bool")
    global _helper_capture_enabled
    with _helper_capture_lock:
        if _helper_capture_started and enabled != _helper_capture_enabled:
            raise RuntimeError("helper capture is immutable after first invocation")
        _helper_capture_enabled = enabled


def _capture_for_operation() -> bool:
    global _helper_capture_started
    with _helper_capture_lock:
        _helper_capture_started = True
        return _helper_capture_enabled


@dataclass(frozen=True)
class TraceHandle:
    """Agent-visible identifier for one valid OpenTelemetry trace."""

    trace_id: str

    def __post_init__(self) -> None:
        if (
            not _trace_id_pattern.fullmatch(self.trace_id)
            or int(self.trace_id, 16) == 0
        ):
            raise ValueError("trace_id must be a valid lowercase 32-hex trace ID")

    def __str__(self) -> str:
        return self.trace_id


def configure_tracing(
    *,
    service_name: str | None = None,
    service_version: str | None = None,
    otlp_endpoint: str | None = None,
    resource: Resource | None = None,
) -> trace.TracerProvider:
    global _tracer_processor_configured, _tracer_provider, _tracer_provider_owned

    with _tracer_provider_lock:
        if _tracer_provider is not None:
            if _tracer_provider_owned and not _tracer_processor_configured:
                _install_span_processor(_tracer_provider, otlp_endpoint)
            return _tracer_provider

        provider = trace.get_tracer_provider()
        if not isinstance(provider, trace.ProxyTracerProvider):
            _tracer_provider = provider
            return provider

        candidate = TracerProvider(
            resource=(
                resource
                if resource is not None
                else _trace_resource(service_name, service_version)
            )
        )
        trace.set_tracer_provider(candidate)
        provider = trace.get_tracer_provider()
        _tracer_provider_owned = provider is candidate
        _tracer_provider = provider
        if _tracer_provider_owned:
            _install_span_processor(provider, otlp_endpoint)
        return provider


def _install_span_processor(
    provider: trace.TracerProvider,
    otlp_endpoint: str | None,
) -> None:
    global _tracer_processor_configured

    processor = BatchSpanProcessor(
        OTLPSpanExporter(endpoint=_trace_endpoint(otlp_endpoint))
    )
    provider.add_span_processor(processor)
    _tracer_processor_configured = True


def get_tracer(name: str | None = None) -> trace.Tracer:
    return trace.get_tracer(name or os.getenv("OTEL_SERVICE_NAME") or "tracing-skill")


def current_trace_handle() -> TraceHandle | None:
    """Return the active lowercase 32-hex trace identifier, if one exists."""

    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return TraceHandle(format(span_context.trace_id, "032x"))


def current_trace_id() -> str | None:
    """Return the active trace ID directly for agent output and artifacts."""

    handle = current_trace_handle()
    return handle.trace_id if handle is not None else None


def inject_trace_context(
    carrier: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Inject the active W3C trace context, failing open on bad carriers."""

    target = carrier if carrier is not None else {}
    try:
        _w3c_propagator.inject(target)
    except Exception:
        logging.getLogger(__name__).exception(
            "observability.trace_context.inject_failed"
        )
    return target


def extract_trace_context(carrier: Mapping[str, str] | None) -> Context:
    """Extract a W3C parent context; absent or malformed input becomes a root."""

    try:
        normalized = {
            str(key).lower(): value for key, value in (carrier or {}).items()
        }
        return _w3c_propagator.extract(normalized, context=Context())
    except Exception:
        logging.getLogger(__name__).exception(
            "observability.trace_context.extract_failed"
        )
        return Context()


def force_flush_tracing(timeout_millis: int = 5_000) -> bool:
    """Flush completed spans without raising into application code."""

    deadline = time.monotonic() + max(0, timeout_millis) / 1_000
    if not _tracer_provider_lock.acquire(
        timeout=max(0.0, deadline - time.monotonic())
    ):
        return False
    provider = _tracer_provider
    if provider is None:
        _tracer_provider_lock.release()
        return True
    if time.monotonic() >= deadline:
        _tracer_provider_lock.release()
        return False
    complete = Event()
    outcome = {"success": False}

    def flush_selected_provider() -> None:
        try:
            force_flush = getattr(provider, "force_flush", None)
            outcome["success"] = force_flush is None or bool(
                force_flush(
                    timeout_millis=max(
                        0,
                        int((deadline - time.monotonic()) * 1_000 + 0.999),
                    )
                )
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "observability.tracing.flush_failed"
            )
        finally:
            _tracer_provider_lock.release()
            complete.set()

    try:
        Thread(
            target=flush_selected_provider,
            name="observability-tracing-flush",
            daemon=True,
        ).start()
    except Exception:
        _tracer_provider_lock.release()
        logging.getLogger(__name__).exception(
            "observability.tracing.flush_worker_failed"
        )
        return False
    if not complete.wait(timeout=max(0.0, deadline - time.monotonic())):
        return False
    return bool(outcome["success"]) and time.monotonic() < deadline


def configure_span_errors(*, mode: str = "full") -> None:
    """Choose shared-helper exception capture before the first shared span.

    full retains legacy SDK message/stacktrace recording. type_only records
    only the exception class and never formats the exception value. This does
    not redact explicit attributes, logs, or spans created outside our helpers.
    """
    global _span_error_mode
    if not isinstance(mode, str) or mode not in ("full", "type_only"):
        raise ValueError("mode must be 'full' or 'type_only'")
    with _span_error_policy_lock:
        if _span_error_policy_started and mode != _span_error_mode:
            raise RuntimeError("configure span errors before the first shared span")
        _span_error_mode = mode


def _span_error_mode_for_operation() -> str:
    global _span_error_policy_started
    with _span_error_policy_lock:
        _span_error_policy_started = True
        return _span_error_mode


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    if not _capture_for_operation():
        # Yield a handle without attaching it over an externally owned context.
        yield trace.INVALID_SPAN
        return
    mode = _span_error_mode_for_operation()
    # Preserve the SDK's existing full-mode exception handling.
    if mode == "full":
        with get_tracer().start_as_current_span(name) as active_span:
            _set_attributes(active_span, attributes)
            _record_trace_correlation()
            yield active_span
        return
    with get_tracer().start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as active_span:
        _set_attributes(active_span, attributes)
        _record_trace_correlation()
        try:
            yield active_span
        except Exception as exc:
            _record_span_error(active_span, exc, mode=mode)
            raise


@overload
def traced_span(func: Callable[P, R]) -> Callable[P, R]: ...


@overload
def traced_span(
    name: str | None = None, **attributes: Any
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def traced_span(
    func: Callable[P, R] | str | None = None,
    **attributes: Any,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a function so each invocation runs inside a span.

    Use as `@traced_span`, `@traced_span()`, or
    `@traced_span("operation.name", key="value")`.
    """

    if callable(func):
        return _decorate_with_span(func, None, attributes)

    span_name = func

    def decorator(wrapped: Callable[P, R]) -> Callable[P, R]:
        return _decorate_with_span(wrapped, span_name, attributes)

    return decorator


def _decorate_with_span(
    func: Callable[P, R],
    span_name: str | None,
    attributes: dict[str, Any],
) -> Callable[P, R]:
    name = span_name or f"{func.__module__}.{func.__qualname__}"

    if iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs):
            if not _capture_for_operation():
                return await func(*args, **kwargs)
            mode = _span_error_mode_for_operation()
            with get_tracer().start_as_current_span(
                name,
                record_exception=False,
                set_status_on_exception=False,
            ) as active_span:
                _set_attributes(active_span, attributes)
                _record_trace_correlation()
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    _record_span_error(active_span, exc, mode=mode)
                    raise

        return async_wrapper

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs):
        if not _capture_for_operation():
            return func(*args, **kwargs)
        mode = _span_error_mode_for_operation()
        with get_tracer().start_as_current_span(
            name,
            record_exception=False,
            set_status_on_exception=False,
        ) as active_span:
            _set_attributes(active_span, attributes)
            _record_trace_correlation()
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                _record_span_error(active_span, exc, mode=mode)
                raise

    return wrapper


def _set_attributes(active_span: trace.Span, attributes: dict[str, Any]) -> None:
    for key, value in attributes.items():
        active_span.set_attribute(key, value)


def _record_span_error(
    active_span: trace.Span, exc: Exception, *, mode: str = "full"
) -> None:
    if mode == "type_only":
        error_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
        active_span.add_event("exception", {"exception.type": error_type})
        active_span.set_status(Status(StatusCode.ERROR, error_type))
        return
    active_span.record_exception(exc)
    active_span.set_status(Status(StatusCode.ERROR, str(exc)))


def _record_trace_correlation() -> None:
    from .metrics import record_trace_correlation

    try:
        record_trace_correlation()
    except Exception:
        logging.getLogger(__name__).exception(
            "observability.trace_correlation.record_failed"
        )


def _trace_resource(
    service_name: str | None,
    service_version: str | None,
) -> Resource:
    return create_observability_resource(service_name, service_version)


def default_endpoint(signal: str) -> str:
    """Return the fallback OTLP base URL, warning the first time it is used.

    Reaching this means nothing configured an endpoint: no argument, no
    `OTEL_EXPORTER_OTLP_ENDPOINT`, no signal-specific variable. That is correct
    on the Docker host, where the monitoring gateway is published on localhost,
    and wrong everywhere else -- so say so once rather than dropping telemetry
    into a socket nobody is listening on.
    """

    global _warned_default_endpoint
    if not _warned_default_endpoint:
        _warned_default_endpoint = True
        logging.getLogger(__name__).warning(
            "observability.endpoint.defaulted",
            extra={
                "signal": signal,
                "endpoint": DEFAULT_OTLP_ENDPOINT,
                "hint": (
                    "No OTLP endpoint configured; assuming the monitoring gateway is "
                    "published on this host. In a pod, set OTEL_EXPORTER_OTLP_ENDPOINT "
                    "to http://host.k3d.internal:4318 (the chart injects it)."
                ),
            },
        )
    return DEFAULT_OTLP_ENDPOINT


def _trace_endpoint(otlp_endpoint: str | None) -> str:
    explicit = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if explicit:
        return explicit

    base = (
        otlp_endpoint
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or default_endpoint("traces")
    )
    return base if base.endswith("/v1/traces") else f"{base.rstrip('/')}/v1/traces"
