# Python OpenTelemetry Metrics over OTLP

Use this reference when Python services need metrics in the shared monitoring
cluster. Applications author standard OpenTelemetry instruments. When no host
provider exists, `tracing_skill_observability` installs one `MeterProvider`
with one `PeriodicExportingMetricReader` and `OTLPMetricExporter`.

## Configure Metrics Export

Configure observability once near application startup:

```python
from tracing_skill_observability import configure_observability

configure_observability(
    service_name="orders-worker",
    service_version="0.3.0",
)
```

In a pod, pass no endpoint: the chart injects
`OTEL_EXPORTER_OTLP_ENDPOINT=http://host.k3d.internal:4318`. A bare-host process
defaults to `http://localhost:4318`. The metrics-specific
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` wins over the function argument and
general environment variable.

Endpoint precedence is signal-specific environment variable, explicit
argument, general environment variable, then the localhost fallback. An
explicit `metrics_export_interval_seconds` wins over
`OTEL_METRIC_EXPORT_INTERVAL`; when it is omitted, the reader uses that
standard environment variable and SDK default. An aggregate-provided resource
wins outright. Otherwise an explicit `service_name` wins over
`OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`; OpenTelemetry resource
detectors supply attributes not explicitly set.

Equivalent TOML:

```toml
[observability]
service_name = "orders-worker"
service_version = "0.3.0"
metrics_enabled = true
metrics_export_interval_seconds = 15.0
```

If the host already installed a real provider, the package uses it without
adding or inspecting readers. In that case the host owns export and lifecycle;
configuration does not claim its transport, resource identity, scraping
behavior, delivery, flush result, or backend reachability.

## Author Standard Instruments

Use `get_meter()` and the OpenTelemetry Metrics API directly:

```python
from tracing_skill_observability import get_meter

meter = get_meter("orders")
completed = meter.create_counter(
    "orders.completed",
    unit="{order}",
    description="Completed orders.",
)
duration = meter.create_histogram(
    "orders.duration",
    unit="s",
    description="Order processing duration.",
)

completed.add(1, {"result": "ok"})
duration.record(0.042, {"result": "ok"})
```

Use low-cardinality attributes and bounded values such as route templates.
Create asynchronous gauges through standard ObservableGauge callbacks; the
package does not own mutable gauge state.

## Trace Correlation

The package owns one correlation Counter named
`tracing_observability.trace_correlation`. `span(...)` and `@traced_span`
record it once when their bounded span begins. If a host creates a bounded span
directly, call the fail-open API once inside it:

```python
from tracing_skill_observability import get_tracer, record_trace_correlation

with get_tracer().start_as_current_span("orders.dispatch"):
    record_trace_correlation()
    dispatch_order()
```

This Counter is the only package instrument with a `trace_id` attribute. Never
add trace IDs to broad operational instruments.

## Bound Correlation Cardinality Explicitly

The compatible default still makes every valid trace eligible, including
unsampled parents. Recording is best-effort; nonblocking lock contention or SDK
failure can suppress an eligible operation even after instrument initialization. A head sampler controls spans, not metric-series retention. For a
long-lived service, choose the policy once at startup, **before any bounded
operation helper runs**:

```python
from tracing_skill_observability import configure_trace_correlation

# Example control, not an acceptance threshold; size it from the workload budget.
configure_trace_correlation(sampled_only=True, max_traces=100)
```

`max_traces` limits distinct admitted trace IDs for the **process lifetime**.
There is no timer or eviction: the SDK can retain an admitted series until exit.
Repeated operations with an admitted ID remain selected after saturation; new
IDs are suppressed. Reservations whose metric add fails still consume budget.
`max_traces=0` admits none; `enabled=False` disables this counter. Repeating the
same policy is idempotent, while changing it after the first operation raises
instead of silently resetting the lifetime cap. Omit these options to retain
legacy behavior. The policy does not change trace or log sampling/export.

An explicitly configured policy adds
`tracing_observability.trace_correlation_decisions` with only fixed `decision`
and `reason` labels: selected, disabled, invalid_context, unsampled,
budget_exhausted, busy or error. Diagnostics are best-effort when the meter is
unavailable or locks contend; counters do not claim backend acknowledgement.
No trace ID or operation name is added to these diagnostics.

The cap is per process, **not a fleet or retention-window bound**. Restarts,
service instances and backend retention multiply retained series; budget those
separately and measure selected/suppressed counts and actual storage volume.
Once saturated, no new IDs are admitted until process exit; do not repeatedly
restart or reconfigure to evade the cap.

The translated dedicated counter name remains
`tracing_observability_trace_correlation_total`, preserving the monitoring CLI
contract. A suppressed correlation metric means `monitoring trace <id>
--require-all` fails even when spans/logs arrived. Use `--signal jaeger --signal
logs --require-all` to diagnose only those requested signals; never report a
suppressed or missing metric as delivered. Select an admitted sampled operation
when demonstrating all-three-signal integration, and retain suppression evidence.

## Finite Processes

`ObservabilityHandle.flush()` requests the standard SDK flush alongside logs
and traces:

```python
observability.flush(timeout_millis=5_000)
```

The result reports only surfaced SDK completion. It does not promise exporter
or backend acknowledgement. For an externally managed provider, the host
defines the meaning of its public flush result. Failures are logged and remain
fail-open.


### Caller deadline (0.4.2)

`force_flush_metrics(timeout_millis=...)` bounds the caller's wait even if a
public SDK flush ignores its advisory timeout. A daemon worker retains the
selected provider's lifecycle lock until SDK work really finishes. A timeout
returns false; subsequent calls wait only their own remaining budget and do not
start overlapping workers. The work is not cancelled and may finish later.
The aggregate flush still uses one inclusive budget across signals.

This governs the flush API call, not separate SDK `atexit` shutdown callbacks or
an externally owned provider's lifecycle. Measure process exit separately.
A true flush reports surfaced SDK completion, never backend acknowledgement.
