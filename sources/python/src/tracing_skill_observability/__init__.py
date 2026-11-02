import logging as stdlib_logging
import math
import time
from threading import Lock
from typing import Mapping, MutableMapping

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource

from ._resource import create_observability_resource
from .config import ObservabilityConfig, configure_observability_from_file, load_config
from .logging import (
    JsonLogFormatter,
    configure_logging,
    force_flush_logging,
    get_logger,
)
from .metrics import (
    configure_metrics,
    configure_trace_correlation,
    force_flush_metrics,
    get_meter,
    record_trace_correlation,
)
from .tracing import (
    TraceHandle,
    configure_tracing,
    configure_span_errors,
    current_trace_handle,
    current_trace_id,
    extract_trace_context,
    force_flush_tracing,
    get_tracer,
    inject_trace_context,
    span,
    traced_span,
)

_configure_lock = Lock()
_configured_signals: set[str] = set()
_requested_signals: set[str] = set()
_failed_signals: set[str] = set()
_aggregate_resource: Resource | None = None
_RESOURCE_BOUND_SIGNALS = {"logging", "metrics"}


class ObservabilityHandle:
    """Process observability controls returned by aggregate configuration."""

    @property
    def trace_handle(self) -> TraceHandle | None:
        return current_trace_handle()

    @property
    def trace_id(self) -> str | None:
        return current_trace_id()

    def inject(
        self, carrier: MutableMapping[str, str] | None = None
    ) -> MutableMapping[str, str]:
        return inject_trace_context(carrier)

    def extract(self, carrier: Mapping[str, str] | None) -> Context:
        return extract_trace_context(carrier)

    def flush(self, timeout_millis: int = 5_000) -> bool:
        return flush_observability(timeout_millis=timeout_millis)


_HANDLE = ObservabilityHandle()


def configure_observability(
    *,
    service_name: str | None = None,
    service_version: str | None = None,
    otlp_endpoint: str | None = None,
    log_level: str = "INFO",
    log_mode: str | None = None,
    logs_endpoint: str | None = None,
    metrics_enabled: bool = True,
    metrics_export_interval_seconds: float | None = None,
) -> ObservabilityHandle:
    """Configure all signals once and return fail-open runtime controls."""

    global _aggregate_resource

    with _configure_lock:
        candidate_resource = _aggregate_resource
        if candidate_resource is None:
            candidate_resource = create_observability_resource(
                service_name,
                service_version,
            )
        provider = _configure_requested_signal(
            "tracing",
            configure_tracing,
            service_name=service_name,
            service_version=service_version,
            otlp_endpoint=otlp_endpoint,
            resource=candidate_resource,
        )
        provider_resource = getattr(provider, "resource", None)
        if isinstance(provider_resource, Resource):
            stale_signals = _configured_signals & _RESOURCE_BOUND_SIGNALS
            resource_changed = (
                _aggregate_resource is not None
                and not _same_resource(_aggregate_resource, provider_resource)
            )
            _aggregate_resource = provider_resource
            if resource_changed and stale_signals:
                _configured_signals.difference_update(stale_signals)
                _failed_signals.update(stale_signals)
                return _HANDLE
        elif _aggregate_resource is None:
            _aggregate_resource = candidate_resource

        _configure_requested_signal(
            "logging",
            configure_logging,
            service_name=service_name,
            service_version=service_version,
            log_level=log_level,
            log_mode=log_mode,
            logs_endpoint=logs_endpoint,
            otlp_endpoint=otlp_endpoint,
            resource=_aggregate_resource,
        )
        if metrics_enabled:
            _configure_requested_signal(
                "metrics",
                configure_metrics,
                interval_seconds=metrics_export_interval_seconds,
                service_name=service_name,
                service_version=service_version,
                otlp_endpoint=otlp_endpoint,
                resource=_aggregate_resource,
            )
    return _HANDLE


def flush_observability(timeout_millis: int = 5_000) -> bool:
    """Synchronously flush metrics, logs, and completed spans without raising."""

    deadline = time.monotonic() + max(0, timeout_millis) / 1_000
    if not _configure_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        return False
    try:
        requested_signals = set(_requested_signals)
        configuration_healthy = not (_failed_signals & requested_signals)
    finally:
        _configure_lock.release()
    if time.monotonic() >= deadline:
        return False
    results = []
    for name, flush in (
        ("metrics", force_flush_metrics),
        ("logging", force_flush_logging),
        ("tracing", force_flush_tracing),
    ):
        if name not in requested_signals:
            continue
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return False
        remaining = math.ceil(remaining_seconds * 1_000)
        results.append(
            _flush_signal(name, flush, timeout_millis=remaining)
        )
    return (
        configuration_healthy
        and all(results)
        and time.monotonic() < deadline
    )


def _configure_requested_signal(name: str, configure, *args, **kwargs):
    _requested_signals.add(name)
    if name in _configured_signals:
        return None
    try:
        configured = configure(*args, **kwargs)
    except Exception:
        _failed_signals.add(name)
        stdlib_logging.getLogger(__name__).exception(
            "observability.configure_failed", extra={"signal": name}
        )
        return None
    else:
        _configured_signals.add(name)
        _failed_signals.discard(name)
        return configured


def _flush_signal(name: str, flush, **kwargs) -> bool:
    try:
        return bool(flush(**kwargs))
    except Exception:
        stdlib_logging.getLogger(__name__).exception(
            "observability.flush_failed", extra={"signal": name}
        )
        return False


def _same_resource(left: Resource, right: Resource) -> bool:
    return dict(left.attributes) == dict(right.attributes)


__all__ = [
    "JsonLogFormatter",
    "ObservabilityConfig",
    "ObservabilityHandle",
    "TraceHandle",
    "configure_logging",
    "configure_metrics",
    "configure_trace_correlation",
    "configure_observability",
    "configure_observability_from_file",
    "configure_tracing",
    "configure_span_errors",
    "current_trace_handle",
    "current_trace_id",
    "extract_trace_context",
    "flush_observability",
    "get_logger",
    "get_meter",
    "get_tracer",
    "load_config",
    "inject_trace_context",
    "record_trace_correlation",
    "span",
    "traced_span",
]
