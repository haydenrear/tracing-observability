# tracing-skill-observability

Importable Python package for standardized application observability. Signals
owned by the package use OpenTelemetry and OTLP/HTTP for delivery to the shared
monitoring gateway. Preinstalled host providers retain their own transport and
lifecycle.

- JSON logs on stdout for Kubernetes collection, or direct OTLP for bare-host
  processes.
- OpenTelemetry spans exported over OTLP/HTTP.
- Native OpenTelemetry metrics with package-owned periodic OTLP export by
  default, or unchanged use of an externally managed host provider.
- Trace and span IDs in log records emitted inside active spans.
- W3C `traceparent`/`tracestate` propagation, an agent-visible trace handle, and a
  fail-open flush barrier for finite processes.

## Configure

```python
from tracing_skill_observability import (
    configure_observability,
    get_logger,
    get_meter,
    span,
)

observability = configure_observability(
    service_name="orders-api",
    service_version="0.3.0",
)
log = get_logger(__name__)
completed = get_meter("orders").create_counter(
    "orders.completed",
    unit="{order}",
    description="Completed orders.",
)

with span("load-order", order_id="ord_123"):
    completed.add(1, {"result": "ok"})
    log.info("order.loaded", extra={"order_id": "ord_123"})

observability.flush()
```

`trace_id` is `None` outside a valid active span and otherwise is the lowercase
32-hex OpenTelemetry trace ID accepted by `monitoring trace <id>`.
`flush()` reports only public SDK completion; exporter failures are logged and
never raised into business code.

Use `observability.inject(headers)` for outgoing W3C headers. Preserve an
incoming valid parent with the fail-open extracted context:

```python
parent = observability.extract(request_headers)
with get_tracer().start_as_current_span("handle-request", context=parent):
    ...
```

Configure from TOML when preferred:

```toml
[observability]
service_name = "orders-api"
service_version = "0.3.0"
log_level = "INFO"
log_mode = "stdout"
metrics_enabled = true
metrics_export_interval_seconds = 15.0
```

```python
from tracing_skill_observability import configure_observability_from_file

configure_observability_from_file("observability.toml")
```

## Endpoints and Ownership

In a service pod, configure no endpoint: the chart injects
`OTEL_EXPORTER_OTLP_ENDPOINT=http://host.k3d.internal:4318`. Bare-host
processes default to `http://localhost:4318`. Signal-specific environment
variables take precedence.

When no global metrics provider exists, the package creates one
`MeterProvider` with one `PeriodicExportingMetricReader` and
`OTLPMetricExporter`. When a host provider already exists, it is externally
managed: the package uses its meters without inspecting readers, adding an
exporter, or claiming its transport, resource identity, scraping behavior,
delivery, flush result, or lifecycle.

## Traces and Correlation

Use `@traced_span` for bounded function work and `span(...)` for short inner
operations. Both record the package-owned
`tracing_observability.trace_correlation` Counter once per span. Host-created
bounded spans may call `record_trace_correlation()` once inside the active
span.

Every span should finish in under one second. Keep operational metric
attributes low-cardinality; the dedicated correlation Counter is the only
package instrument carrying `trace_id`.

## Migrating to 0.3.0

Version 0.3.0 intentionally removes the unpublished Prometheus compatibility
surface: `PrometheusOtlpPusher`, arbitrary registry push, `metrics_app`,
`start_metrics_server`, `start_metrics_otlp_pusher`, `trace_metric_labels`,
package HTTP metric helpers, `metrics_port`, and `metrics_addr`. It also
removes the `prometheus-client` dependency.

Replace Prometheus instruments with standard OpenTelemetry instruments from
`get_meter()`. Replace generic trace-label injection with the package-owned
correlation Counter. There are no compatibility shims; consumers should stay
on their prior pin until their migration ticket lands.

Consumers must use a versioned wheel or a full Git-commit pin; never commit an
integration-machine path. See
`../../references/python/distribution.md` for distribution guidance.

## Optional Lifetime Correlation Budget

Before operation helpers run, call
`configure_trace_correlation(sampled_only=True, max_traces=<budget>)` to cap
distinct dedicated correlation metric series for this process lifetime. Default eligibility
remains uncapped, including valid unsampled parents. Recording is best-effort;
nonblocking lock contention or SDK failures can suppress eligible operations. Head sampling
alone does not bound metric cardinality. Fixed-label selection diagnostics
report suppression; suppressed metric IDs will not satisfy the monitoring CLI's
all-three-signal requirement. See `references/python/opentelemetry-metrics.md`
for restart/fleet retention limits and explicit policy semantics.
