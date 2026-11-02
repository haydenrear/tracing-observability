# Python Jaeger Spans

Use this reference when Python code needs trace spans that appear in
Jaeger. Spans are exported over OTLP/HTTP to the OTel gateway on the
shared monitoring cluster, which fans them out to Jaeger. There is
exactly one gateway, and it is the only place telemetry is sent.

## Install

```bash
tracing-observability-install --project /path/to/project
```

## Configure

**In a pod, set no endpoint at all.** The chart injects the OTel
environment variables, and the library reads them:

```python
from tracing_skill_observability import configure_observability

configure_observability(service_name="orders-api", service_version="0.1.0")
```

Hardcoding an `otlp_endpoint=` in a deployed service overrides the value
the platform injected, which is how a service ends up pointing at a
collector that no longer exists. Pass one only when nothing injects it.

Set it explicitly for a bare-host process — a test, a native runner, a
local script — because there is no chart to configure it:

```python
configure_observability(
    service_name="orders-worker",
    service_version="0.1.0",
    otlp_endpoint="http://localhost:4318",
)
```

## Runtime handle and W3C propagation

Aggregate setup returns a runtime handle. Read its trace ID inside a bounded
operation span, end that span, then flush before a short-lived process exits:

```python
from tracing_skill_observability import (
    configure_observability,
    get_tracer,
    span,
)

observability = configure_observability(service_name="orders-worker")

with span("publish-order"):
    print(observability.trace_id)
    outgoing_headers = observability.inject({})
    publish_order(outgoing_headers)

flushed = observability.flush()
```

The ID is a lowercase 32-hex OpenTelemetry trace ID. `flush()` reports exporter
failure with `False` but does not raise into business code.

For an incoming carrier, pass the extracted context to the operation span:

```python
parent = observability.extract(incoming_headers)
with get_tracer().start_as_current_span("consume-order", context=parent):
    ...
```

Valid W3C context preserves the upstream trace ID. Absent or malformed context
fails open to an empty context, so the operation starts a new root trace.

## Endpoints

The monitoring cluster maps its gateway ports to the Docker host, so one
exposure serves callers on both sides:

| Caller | OTLP/HTTP base |
| --- | --- |
| A service pod | `http://host.k3d.internal:4318` |
| The host (tests, native runners) | `http://localhost:4318` |

`http://localhost:4318` is the library default, so an unconfigured
bare-host process works and an unconfigured pod logs a warning naming the
variable to set. Pass the **base** URL: the exporter appends `/v1/traces`
itself.

`deploy-helm:references/monitoring-cluster.md` is the source of truth for
these endpoints and for every other signal (Loki, Prometheus, Grafana).
Read it there rather than trusting a literal copied out of this file.

## Environment Variables

The platform chart injects these; the library honors them, and an
explicitly passed argument is what you use when nothing does:

- `OTEL_SERVICE_NAME`
- `OTEL_EXPORTER_OTLP_ENDPOINT` — base URL, no `/v1/...` suffix
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` — full traces URL for this signal only
- `OTEL_EXPORTER_OTLP_PROTOCOL` — `http/protobuf`
- `OTEL_EXPORTER_OTLP_HEADERS`
- `OTEL_RESOURCE_ATTRIBUTES`
- `DEPLOYMENT_ENVIRONMENT`

The traces endpoint resolves in this order, and the signal-specific
variable deliberately wins outright — it is how an operator redirects one
signal of an already-configured process without editing it:

1. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
2. the `otlp_endpoint=` argument
3. `OTEL_EXPORTER_OTLP_ENDPOINT`
4. `http://localhost:4318` (the default, which warns)

## Span Duration Rule

Every span should represent work expected to complete in less than 1
second. Do not create spans around long-running loops, servers, daemons,
pollers, watches, consumers, training runs, migrations, or whole batch
jobs. For those workflows, create spans around bounded units of work such
as one request, one message, one query, one retry, one chunk, or one loop
iteration.

If an operation commonly takes longer than 1 second, either split it into
smaller spans or use logs and metrics instead of one long span.

## Create Function Spans With The Annotation

Use the library-provided `@traced_span` annotation for function
instrumentation. This works for synchronous and asynchronous functions.
If the wrapped function raises, the annotation records the exception on
the active span, marks the span status as `ERROR`, and re-raises the
original exception.

```python
from tracing_skill_observability import traced_span

@traced_span("load-order", component="orders")
def load_order(order_id: str):
    ...
```

You can omit the span name to use the function's module-qualified name:

```python
from tracing_skill_observability import traced_span

@traced_span
def publish_order_event(order_id: str):
    ...
```

Async functions are supported:

```python
from tracing_skill_observability import traced_span

@traced_span("fetch-price")
async def fetch_price(symbol: str):
    ...
```

Do not create a local tracing decorator, wrapper, fallback, shim, or
guard around `traced_span`. Assume `tracing_skill_observability` is
installed and importable when this skill is being used.

Do not replace `@traced_span` with a `span(...)` context manager for a
whole function. Use `@traced_span` so exceptions are recorded as span
errors and propagated normally.

## Create Short Inner Spans With A Context Manager

Use `span(...)` only for short inner operations when the whole function is
too broad or could exceed the 1-second span duration rule. Good examples
are external calls, queue message handling, database queries, expensive
local computation blocks, and bounded business workflow steps.

```python
from tracing_skill_observability import span

with span("load-order-query", order_id=order_id):
    order = load_order_query(order_id)
```

Span attributes should be useful for filtering and debugging:

```python
with span(
    "publish-event",
    topic="orders.created",
    tenant=tenant,
    order_id=order_id,
):
    publish_event(order)
```

Avoid secrets, raw payloads, large documents, and high-cardinality fields
that are not needed for debugging.

## Logs And Spans

When logs are emitted inside an active span, the JSON log formatter adds
`trace_id` and `span_id`. This makes it possible to move from a log line
to the corresponding trace.

```python
from tracing_skill_observability import get_logger, traced_span

log = get_logger(__name__)

@traced_span("reprice-position", component="positions")
def reprice_position(position_id: str):
    log.info("position.reprice.started", extra={"position_id": position_id})
```

## Find A Trace

Reach for the `monitoring` CLI, not a UI. It resolves one trace id across
all three signals at once, which is the reason the shared cluster exists:

```bash
monitoring trace <id>                  # spans + log lines + metric series
monitoring trace <id> --require-all    # fails unless all three arrived
monitoring trace <id> --json           # machine-readable
```

`--require-all` is the end-to-end check on instrumentation: it passes only
when the span reached Jaeger, the log line reached Loki carrying the trace
id, and a metric series carrying a `trace_id` label reached Prometheus.

An explicitly selected correlation policy may suppress a metric after its
process-lifetime cap or when its parent is unsampled. In that case a missing
metric is expected and `--require-all` remains a failed all-three-signal check;
select only the requested Jaeger/log signals to inspect them. See the metrics
reference for the policy, bounded diagnostics and retention caveats.

The Jaeger UI is on the monitoring cluster at `http://localhost:16686`.

## What The Backends Remember

A trace that is missing is often a trace that expired. Before concluding
the instrumentation is broken, check the age of what you are looking for:

| Backend | Retention |
| --- | --- |
| Jaeger (spans) | in-memory, 50k traces; **does not survive a monitoring-cluster restart** |
| Prometheus (metrics) | 15 days, or 10GB |
| Loki (logs) | 72 hours |

Jaeger's storage is in-memory by design. Traces do survive a *service*
cluster being destroyed — which is the property the split-out was for —
but they do not survive the monitoring cluster itself restarting. A
week-old trace is gone, and its logs are gone at three days.
