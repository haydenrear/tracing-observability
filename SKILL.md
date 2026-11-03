---
name: tracing-observability
description: Use when instrumenting Python libraries or JVM-native applications with standardized structured JSON logging, OpenTelemetry spans and metrics, W3C propagation, and bounded fail-open flushes; package-owned providers default to OTLP delivery to the shared monitoring cluster.
skill-imports: []
metadata:
  focus: client-side-python-instrumentation
  package: tracing-skill-observability
  import_module: tracing_skill_observability
  source: sources/python
  references:
    - references/python/structured-logging.md
    - references/python/jaeger-spans.md
    - references/python/opentelemetry-metrics.md
    - references/python/distribution.md
---

# tracing-observability

Use this skill for client-side Python instrumentation and for the shared wire and
lifecycle contract implemented with native OpenTelemetry SDKs in JVM consumers.
Deployment and cluster operations are handled by the deploy skill, not this skill.

## Where Telemetry Goes

When this package owns a signal provider, it pushes that signal over OTLP to
the OTel gateway on the **shared monitoring cluster**, which fans out to
Jaeger, Loki, and Prometheus. It is deployed once and pointed at thereafter;
nothing is stored on a service cluster, and nothing scrapes your app. A
preinstalled host provider remains externally managed: the host defines its
transport, resource, delivery, flush, and lifecycle behavior.

| Caller | OTLP/HTTP base |
| --- | --- |
| A service pod | the explicit platform-profile pod endpoint (Docker Desktop: `http://host.docker.internal:4318`) |
| The host (tests, native runners) | `http://localhost:4318` |

In a pod the chart injects `OTEL_EXPORTER_OTLP_ENDPOINT`, so **you
normally configure no endpoint at all**; set one explicitly only for a
bare-host process. `deploy-helm:references/monitoring-cluster.md` is the
source of truth for the endpoints, the `monitoring` CLI, and retention.
Use `monitoring trace <id>` to see a trace across all three signals.

## Provides

- Importable Python package: `tracing_skill_observability`
- Structured JSON logging with trace/span correlation, delivered through
  container stdout or direct OTLP HTTP export
- OpenTelemetry span helpers and OTLP HTTP export
- Span annotation for sync and async Python functions
- Native OpenTelemetry metrics instruments with package-owned SDK/OTLP defaults
  or unchanged use of an externally managed host provider
- TOML config loading for application/library setup
- Skill-managed helper CLI: `tracing-observability-install`
- Immutable Python source-pin and JVM-native consumption contract

## Start Here

Use these Python references:

- [Structured logging](references/python/structured-logging.md):
  configure JSON stdout/OTLP delivery modes, standard log keys, and library
  logging expectations for containers and bare-host processes.
- [Jaeger spans](references/python/jaeger-spans.md): configure OTLP
  tracing, create spans, attach useful attributes, and correlate logs
  with traces.
- [OpenTelemetry metrics](references/python/opentelemetry-metrics.md): author
  standard instruments, use the SDK-owned OTLP route, and create deliberately
  trace-correlated measurements and opt-in process-lifetime cardinality caps.
- [Distribution and JVM consumption](references/python/distribution.md): pin
  the Python package immutably and implement the same W3C/flush contract in
  JVM consumers without embedding Python.

## Span Rules

For errors that may contain input values, select `configure_span_errors(mode="type_only")`
before the first shared span (package 0.4.1+). The [Jaeger guide](references/python/jaeger-spans.md#exception-data-policy-041)
explains the helper-only scope; raw SDK/third-party spans and logs need their own policy.

- Prefer the library-provided `@traced_span` annotation for function
  instrumentation. It records exceptions on the span, marks failures as
  `ERROR`, and re-raises the original exception.
- Do not create a local tracing decorator, wrapper, fallback, shim, or
  guard around `traced_span`. Assume `tracing_skill_observability` is an
  available dependency when this skill is being used.
- Do not replace `@traced_span` with a `span(...)` context manager for a
  whole function. Use the context manager only for short inner operations
  when annotating the whole function would exceed the span duration rule.
- Every span should represent work expected to finish in less than 1
  second. Do not wrap long-running loops, servers, daemons, pollers,
  watches, consumers, training runs, migrations, or batch jobs in one
  span. Instead, span individual iterations, requests, messages, queries,
  chunks, or other bounded operations.

Minimal usage:

```python
from tracing_skill_observability import configure_observability, get_logger, traced_span

configure_observability(service_name="orders-api", service_version="0.1.0")
log = get_logger(__name__)

@traced_span("load-order", component="orders")
def load_order(order_id: str):
    log.info("order.loaded", extra={"order_id": "ord_123"})
```

## Correlation Selection

Legacy helpers make every valid parent eligible for the dedicated correlation
counter, including unsampled parents. Recording is best-effort: lock contention
or SDK failure may suppress an otherwise eligible operation. For long-lived consumers, choose an explicit
startup policy with `configure_trace_correlation(sampled_only=True,
max_traces=<workload budget>)` before any operation. Its process-lifetime cap
does not bound fleet retention or restart churn. See the metrics reference for
suppression diagnostics and the monitoring CLI missing-metric behavior.

## Startup-only owned helper capture (0.4.3)

`configure_helper_capture(enabled=False)` before the first shared helper invocation
turns `@traced_span` into a direct sync/async call and makes `span` yield an invalid
handle without attaching it. Decoration alone does not freeze the policy. Strict
booleans only; the first invocation freezes selection, and repeating that selection
is idempotent. Configuration racing first use is serialized. Defaults remain enabled.
This gates helper spans/errors/correlation only: explicit provider setup, logs and
metrics must be gated separately by the owning application before configuration.
It never disables external SDKs/providers or public W3C inject/extract. Keep direct
shared decorators; wrapper dispatch remains common cost in enabled/off comparisons.
