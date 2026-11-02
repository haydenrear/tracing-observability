# Runtime distribution and JVM consumption

Use an immutable dependency reference for every consumer. Do not commit a
developer machine path such as `.skill-manager/skills/...` or `file:///Users/...`
to a project manifest or lockfile.

## Python

The package version is `0.4.0` (additive correlation policy API). Until that wheel is published to a package
registry, pin the tracing-skill repository by its full 40-character commit SHA:

```bash
uv add "tracing-skill-observability @ git+https://github.com/haydenrear/tracing_skill.git@618d33169d9aa3e168c60ab9100fb7efb24a13e6#subdirectory=sources/python"
uv lock --check
```

This pin is the first remotely fetchable tracing-skill commit containing the
reviewed `0.3.0` provider. Commit both `pyproject.toml` and `uv.lock`; the lock
must retain this resolved commit, not a branch or tag that can move.

The example revision above remains the reviewed 0.3.0 compatibility floor.
Consumers using `configure_trace_correlation` must pin a merged immutable
revision containing 0.4.0; record the resolved revision in their lockfile rather
than assuming the older example provides the new API.

The skill-manager installer builds a versioned wheel once and installs that
wheel into selected environments. Its generated `tracing-observability-install`
helper also installs the built wheel, never the live source directory.

## JVM

JVM consumers do not embed Python and tracing-skill does not publish a JVM
wrapper. They implement the same wire and lifecycle contract with the native
OpenTelemetry Java SDK:

- Pin the repository's OpenTelemetry BOM/version catalog and enable Gradle
  dependency locking.
- Configure traces, metrics, and structured logs at process startup. Pods emit
  logs once on stdout; native processes export logs over OTLP.
- Use `W3CTraceContextPropagator` for `traceparent`/`tracestate` injection and
  extraction. Invalid or absent headers start a root operation and never fail
  the business operation.
- Expose `Span.current().getSpanContext().getTraceId()` while the operation span
  is active. Accept only a valid lowercase 32-hex identifier as the agent
  handle.
- Before a short-lived process exits, end its operation span and synchronously
  join the SDK providers' `forceFlush()` results with a bounded timeout, then
  close the SDK. Report a failed flush separately; do not change an otherwise
  successful business result.
- Keep every span below one second and attach `trace_id` only to the dedicated
  correlation metric.

This is a shared behavior and wire contract, not a requirement for Python and
JVM consumers to share an implementation artifact.
