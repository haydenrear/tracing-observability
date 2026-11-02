"""A real SDK reader may ignore advisory timeouts; caller deadlines stay bounded."""
import threading
import time

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from tracing_skill_observability import metrics as metrics_module


class SlowReader(MetricReader):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
    def _receive_metrics(self, metrics_data, timeout_millis=10_000, **kwargs):
        self.calls += 1
        self.entered.set()
        try:self.release.wait(timeout=1)
        finally:self.finished.set()
    def shutdown(self, timeout_millis=30_000, **kwargs):pass


def test_real_sdk_reader_cannot_outlive_caller_flush_budget(monkeypatch):
    reader = SlowReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    provider.get_meter('deadline').create_counter('work').add(1)
    monkeypatch.setattr(metrics_module, '_meter_provider', provider)
    try:
        started = time.monotonic()
        result = metrics_module.force_flush_metrics(timeout_millis=20)
        elapsed = time.monotonic() - started
        assert result is False
        assert elapsed < 0.25
        assert reader.entered.is_set()
        # SDK work remains live: do not release lifecycle ownership or start
        # a second overlapping SDK flush merely because the caller timed out.
        assert not metrics_module._meter_provider_lock.acquire(blocking=False)
        assert metrics_module.force_flush_metrics(timeout_millis=20) is False
        assert reader.calls == 1
        reader.release.set()
        assert reader.finished.wait(timeout=1)
        assert metrics_module._meter_provider_lock.acquire(timeout=1)
        metrics_module._meter_provider_lock.release()
        assert metrics_module.force_flush_metrics(timeout_millis=200) is True
        assert reader.calls == 2
    finally:
        reader.release.set()
        assert reader.finished.wait(timeout=1)
        assert metrics_module._meter_provider_lock.acquire(timeout=1)
        metrics_module._meter_provider_lock.release()
        provider.shutdown()


def test_worker_start_failure_releases_lifecycle_lock(monkeypatch):
    class Provider:
        def force_flush(self, timeout_millis):return True
    class NoThread:
        def __init__(self, **kwargs):pass
        def start(self):raise RuntimeError('cannot start worker')
    monkeypatch.setattr(metrics_module, '_meter_provider', Provider())
    monkeypatch.setattr(metrics_module, 'Thread', NoThread, raising=False)
    assert metrics_module.force_flush_metrics(timeout_millis=20) is False
    assert metrics_module._meter_provider_lock.acquire(blocking=False)
    metrics_module._meter_provider_lock.release()
