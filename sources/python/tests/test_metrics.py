import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from tracing_skill_observability import metrics as observability_metrics

OTLP_ENV_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)


@pytest.fixture(autouse=True)
def reset_metrics_state(monkeypatch):
    for name in OTLP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(observability_metrics, "_meter_provider", None)
    monkeypatch.setattr(observability_metrics, "_trace_correlation_counter", None)


def test_metrics_endpoint_falls_back_to_normalized_default(monkeypatch):
    monkeypatch.setattr(
        observability_metrics,
        "default_endpoint",
        lambda signal: "http://localhost:4318",
    )

    assert (
        observability_metrics._metrics_endpoint(None)
        == "http://localhost:4318/v1/metrics"
    )


def test_metrics_endpoint_precedence_and_normalization(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://base:4318/")

    assert (
        observability_metrics._metrics_endpoint("http://argument:4318")
        == "http://argument:4318/v1/metrics"
    )

    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://signal:4318/custom-metrics",
    )
    assert (
        observability_metrics._metrics_endpoint("http://argument:4318")
        == "http://signal:4318/custom-metrics"
    )


def test_configure_metrics_installs_one_sdk_provider_and_reader(monkeypatch):
    selected_provider = object()
    exporters = []
    readers = []
    providers = []

    class FakeExporter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            exporters.append(self)

    class FakeReader:
        def __init__(self, exporter, *, export_interval_millis):
            self.exporter = exporter
            self.export_interval_millis = export_interval_millis
            readers.append(self)

    class FakeProvider:
        def __init__(self, *, resource, metric_readers):
            self.resource = resource
            self.metric_readers = metric_readers
            self.shutdown_calls = 0
            providers.append(self)

        def shutdown(self):
            self.shutdown_calls += 1

    def get_provider():
        return selected_provider

    def set_provider(provider):
        nonlocal selected_provider
        selected_provider = provider

    monkeypatch.setattr(observability_metrics.metrics, "get_meter_provider", get_provider)
    monkeypatch.setattr(observability_metrics.metrics, "set_meter_provider", set_provider)
    monkeypatch.setattr(observability_metrics, "OTLPMetricExporter", FakeExporter)
    monkeypatch.setattr(
        observability_metrics,
        "PeriodicExportingMetricReader",
        FakeReader,
    )
    monkeypatch.setattr(observability_metrics, "SdkMeterProvider", FakeProvider)
    resource = Resource.create({"service.name": "orders"})

    first = observability_metrics.configure_metrics(
        otlp_endpoint="http://collector:4318/",
        interval_seconds=2.5,
        resource=resource,
    )
    second = observability_metrics.configure_metrics(
        otlp_endpoint="http://ignored:4318",
        interval_seconds=99,
    )

    assert first is second is providers[0]
    assert len(providers) == len(readers) == len(exporters) == 1
    assert providers[0].resource is resource
    assert providers[0].metric_readers == readers
    assert readers[0].exporter is exporters[0]
    assert readers[0].export_interval_millis == 2_500
    assert exporters[0].kwargs["endpoint"] == "http://collector:4318/v1/metrics"


def test_configure_metrics_reuses_externally_managed_provider(monkeypatch):
    host_provider = object()
    candidates = []
    set_attempts = []

    class FakeProvider:
        def __init__(self, **kwargs):
            self.shutdown_calls = 0
            self.shutdown_outside_lock = False
            candidates.append(self)

        def shutdown(self):
            self.shutdown_calls += 1
            self.shutdown_outside_lock = (
                observability_metrics._meter_provider_lock.acquire(blocking=False)
            )
            if self.shutdown_outside_lock:
                observability_metrics._meter_provider_lock.release()

    monkeypatch.setattr(
        observability_metrics.metrics,
        "get_meter_provider",
        lambda: host_provider,
    )
    monkeypatch.setattr(
        observability_metrics.metrics,
        "set_meter_provider",
        lambda provider: set_attempts.append(provider),
    )
    monkeypatch.setattr(
        observability_metrics,
        "SdkMeterProvider",
        FakeProvider,
    )
    monkeypatch.setattr(
        observability_metrics,
        "OTLPMetricExporter",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        observability_metrics,
        "PeriodicExportingMetricReader",
        lambda *args, **kwargs: object(),
    )

    configured = observability_metrics.configure_metrics(
        otlp_endpoint="http://ignored:4318"
    )

    assert configured is host_provider
    assert set_attempts == candidates
    assert len(candidates) == 1
    assert candidates[0].shutdown_calls == 1
    assert candidates[0].shutdown_outside_lock is True


def test_configure_metrics_rejects_nonpositive_interval():
    with pytest.raises(ValueError, match="greater than 0"):
        observability_metrics.configure_metrics(interval_seconds=0)


def test_get_meter_returns_standard_api_meter(monkeypatch):
    expected = object()
    calls = []
    monkeypatch.setattr(
        observability_metrics.metrics,
        "get_meter",
        lambda name, version: calls.append((name, version)) or expected,
    )

    assert observability_metrics.get_meter("orders", "1.2.3") is expected
    assert calls == [("orders", "1.2.3")]


def test_record_trace_correlation_uses_one_owned_counter(monkeypatch):
    counter = FakeCounter()
    meter = FakeMeter(counter)
    monkeypatch.setattr(observability_metrics, "get_meter", lambda *args: meter)

    with trace.use_span(NonRecordingSpan(_span_context())):
        assert observability_metrics.record_trace_correlation() is True
        assert observability_metrics.record_trace_correlation() is True

    assert meter.created == [
        (
            "tracing_observability.trace_correlation",
            "{operation}",
            "Bounded operations selected for trace correlation.",
        )
    ]
    assert counter.additions == [
        (1, {"trace_id": "0123456789abcdef0123456789abcdef"}),
        (1, {"trace_id": "0123456789abcdef0123456789abcdef"}),
    ]


def test_record_trace_correlation_is_fail_open_without_a_valid_span(monkeypatch):
    monkeypatch.setattr(
        observability_metrics,
        "get_meter",
        lambda *args: pytest.fail("no instrument should be created"),
    )

    assert observability_metrics.record_trace_correlation() is False


def test_record_trace_correlation_is_fail_open_when_counter_raises(
    monkeypatch,
    caplog,
):
    counter = FakeCounter(error=RuntimeError("meter unavailable"))
    monkeypatch.setattr(
        observability_metrics,
        "get_meter",
        lambda *args: FakeMeter(counter),
    )

    with trace.use_span(NonRecordingSpan(_span_context())):
        assert observability_metrics.record_trace_correlation() is False

    assert "observability.metrics.trace_correlation_failed" in caplog.text


def test_record_trace_correlation_does_not_wait_for_configuration(monkeypatch):
    locked = threading.Event()
    release = threading.Event()

    def hold_metrics_lock():
        with observability_metrics._meter_provider_lock:
            locked.set()
            release.wait(timeout=1)

    blocker = threading.Thread(target=hold_metrics_lock)
    blocker.start()
    assert locked.wait(timeout=1)
    monkeypatch.setattr(
        observability_metrics,
        "get_meter",
        lambda *args: pytest.fail("contended first use must fail open"),
    )

    started = time.monotonic()
    try:
        with trace.use_span(NonRecordingSpan(_span_context())):
            assert observability_metrics.record_trace_correlation() is False
        elapsed = time.monotonic() - started
    finally:
        release.set()
        blocker.join(timeout=1)

    assert elapsed < 0.1
    assert not blocker.is_alive()


@pytest.mark.parametrize("result", [True, False])
def test_force_flush_metrics_reports_the_public_sdk_result(monkeypatch, result):
    provider = FakeFlushProvider(result=result)
    monkeypatch.setattr(observability_metrics, "_meter_provider", provider)

    assert observability_metrics.force_flush_metrics(timeout_millis=1234) is result
    assert len(provider.timeouts) == 1
    assert 0 < provider.timeouts[0] <= 1234


def test_force_flush_metrics_forwards_only_the_remaining_budget(monkeypatch):
    provider = FakeFlushProvider(result=True)
    clock_values = iter((10.0, 10.0, 10.25))
    monkeypatch.setattr(
        observability_metrics.time,
        "monotonic",
        lambda: next(clock_values, 10.25),
    )
    monkeypatch.setattr(observability_metrics, "_meter_provider", provider)

    assert observability_metrics.force_flush_metrics(timeout_millis=1_000) is True
    assert len(provider.timeouts) == 1
    assert 749 <= provider.timeouts[0] <= 751


def test_force_flush_metrics_returns_false_on_lock_contention(monkeypatch):
    provider = FakeFlushProvider(result=True)
    locked = threading.Event()
    release = threading.Event()

    def hold_metrics_lock():
        with observability_metrics._meter_provider_lock:
            locked.set()
            release.wait(timeout=1)

    blocker = threading.Thread(target=hold_metrics_lock)
    blocker.start()
    assert locked.wait(timeout=1)
    monkeypatch.setattr(observability_metrics, "_meter_provider", provider)

    started = time.monotonic()
    try:
        result = observability_metrics.force_flush_metrics(timeout_millis=10)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        blocker.join(timeout=1)

    assert result is False
    assert elapsed < 0.1
    assert provider.timeouts == []
    assert not blocker.is_alive()


def test_force_flush_metrics_fails_open_for_unconfigured_or_raised_sdk(monkeypatch):
    assert observability_metrics.force_flush_metrics(timeout_millis=1) is False

    provider = FakeFlushProvider(error=RuntimeError("export failed"))
    monkeypatch.setattr(observability_metrics, "_meter_provider", provider)
    assert observability_metrics.force_flush_metrics(timeout_millis=1) is False


def test_clean_import_starts_no_package_metrics_worker_and_needs_no_prometheus():
    script = textwrap.dedent(
        """
        import sys
        import threading
        import tracing_skill_observability

        assert "prometheus_client" not in sys.modules
        assert not [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith(("prometheus-otlp", "tracing-skill-metrics"))
        ]
        for removed in (
            "PrometheusOtlpPusher",
            "http_request_duration",
            "http_requests_total",
            "metrics_app",
            "start_metrics_otlp_pusher",
            "start_metrics_server",
            "trace_metric_labels",
        ):
            assert not hasattr(tracing_skill_observability, removed), removed
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_real_otlp_http_payload_contains_resource_instruments_and_correlation():
    script = textwrap.dedent(
        """
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from opentelemetry import metrics, trace
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
        from tracing_skill_observability import (
            configure_metrics,
            force_flush_metrics,
            get_meter,
            record_trace_correlation,
        )

        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                requests.append((self.path, self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/"
        provider = configure_metrics(
            service_name="payload-service",
            service_version="1.2.3",
            otlp_endpoint=endpoint,
            interval_seconds=60,
        )
        meter = get_meter("payload-test")
        meter.create_counter(
            "orders.completed",
            unit="{order}",
            description="Completed orders.",
        ).add(3, {"result": "ok"})
        meter.create_histogram(
            "orders.duration",
            unit="s",
            description="Order processing duration.",
        ).record(0.25, {"result": "ok"})
        meter.create_observable_gauge(
            "orders.queue.depth",
            callbacks=[
                lambda options: [
                    metrics.Observation(2, {"queue": "ready"})
                ]
            ],
            unit="{order}",
            description="Queued orders.",
        )
        span_context = SpanContext(
            trace_id=int("0123456789abcdef0123456789abcdef", 16),
            span_id=int("0123456789abcdef", 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        with trace.use_span(NonRecordingSpan(span_context)):
            assert record_trace_correlation()
        assert force_flush_metrics(timeout_millis=5_000)
        provider.shutdown()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

        assert requests
        assert {path for path, _ in requests} == {"/v1/metrics"}
        payloads = []
        for _, body in requests:
            request = ExportMetricsServiceRequest()
            request.ParseFromString(body)
            payloads.append(request)

        resources = {}
        sums = {}
        histograms = {}
        gauges = {}
        for request in payloads:
            for resource_metrics in request.resource_metrics:
                resources.update(
                    {
                        item.key: item.value.string_value
                        for item in resource_metrics.resource.attributes
                    }
                )
                for scope_metrics in resource_metrics.scope_metrics:
                    for metric in scope_metrics.metrics:
                        if metric.HasField("sum"):
                            sums.setdefault(metric.name, []).extend(
                                metric.sum.data_points
                            )
                        elif metric.HasField("histogram"):
                            histograms.setdefault(metric.name, []).extend(
                                metric.histogram.data_points
                            )
                        elif metric.HasField("gauge"):
                            gauges.setdefault(metric.name, []).extend(
                                metric.gauge.data_points
                            )

        assert resources["service.name"] == "payload-service"
        assert resources["service.version"] == "1.2.3"
        order_points = sums["orders.completed"]
        assert any(point.as_int == 3 for point in order_points)
        assert any(
            point.count == 1 and point.sum == 0.25
            for point in histograms["orders.duration"]
        )
        assert any(
            point.as_int == 2 for point in gauges["orders.queue.depth"]
        )
        correlation_points = sums["tracing_observability.trace_correlation"]
        assert any(
            {
                item.key: item.value.string_value
                for item in point.attributes
            }.get("trace_id") == "0123456789abcdef0123456789abcdef"
            and point.as_int == 1
            for point in correlation_points
        )
        """
    )
    env = os.environ.copy()
    for name in OTLP_ENV_VARS:
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr


class FakeCounter:
    def __init__(self, error=None):
        self.error = error
        self.additions = []

    def add(self, value, attributes):
        if self.error is not None:
            raise self.error
        self.additions.append((value, attributes))


class FakeMeter:
    def __init__(self, counter):
        self.counter = counter
        self.created = []

    def create_counter(self, name, *, unit, description):
        self.created.append((name, unit, description))
        return self.counter


class FakeFlushProvider:
    def __init__(self, result=False, error=None):
        self.result = result
        self.error = error
        self.timeouts = []

    def force_flush(self, timeout_millis):
        self.timeouts.append(timeout_millis)
        if self.error is not None:
            raise self.error
        return self.result


def _span_context():
    return SpanContext(
        trace_id=int("0123456789abcdef0123456789abcdef", 16),
        span_id=int("0123456789abcdef", 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
