"""
Тесты ограничения параллелизма фонового менеджера (services/jobs.py).

Chromium тяжёлый: одновременный запуск нескольких проверок «голодит»
контейнер (риск 502). Поэтому JobManager выполняет не более
``max_concurrent`` задач одновременно, а остальные ждут в статусе ``queued``
и стартуют по FIFO. Здесь мы это проверяем: сканер подменяется фейком,
который замеряет фактический пик параллелизма через общий счётчик.
"""
import threading
import time

import pytest

from scanner.models import ScanInput, ScanResult
from services import jobs as jobs_module
from services.jobs import JobManager


class _FakeSettings:
    """Минимальные настройки (без чтения env/БД)."""

    session_secret = "test-secret-0123456789abcdef"


@pytest.fixture()
def manager_cap1():
    """Свежий singleton JobManager с пределом параллелизма 1."""
    with JobManager._instance_lock:
        JobManager._instance = JobManager(max_concurrent=1)
    yield JobManager.instance()
    with JobManager._instance_lock:
        JobManager._instance = None


def _wait(cond, timeout: float = 10.0) -> bool:
    """Подождать выполнения условия (poll каждые 20 мс)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class _ConcurrencyProbe:
    """Общий счётчик одновременно работающих фейковых сканов + пик."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.completed_order = []  # site_url в порядке завершения

    def enter(self) -> None:
        with self._lock:
            self.current += 1
            if self.current > self.max_seen:
                self.max_seen = self.current

    def leave(self, site_url: str) -> None:
        with self._lock:
            self.current -= 1
            self.completed_order.append(site_url)


def test_cap_limits_concurrency_and_preserves_fifo(manager_cap1, monkeypatch):
    """cap=1: пик параллелизма ровно 1, все 3 задачи -> done, порядок FIFO."""
    probe = _ConcurrencyProbe()

    def _fake_run_scan(scan_input, settings, progress_cb=None, should_stop=None):
        probe.enter()
        try:
            if progress_cb:
                progress_cb("работаю над {}".format(scan_input.site_url))
            time.sleep(0.4)  # окно, в котором ловился бы второй параллельный скан
            return ScanResult(scan_id="", site_url=scan_input.site_url)
        finally:
            probe.leave(scan_input.site_url)

    monkeypatch.setattr(jobs_module, "run_scan", _fake_run_scan)
    monkeypatch.setattr(jobs_module.repositories, "save_scan", lambda r, s: "scan-x")

    urls = ["https://a.ru", "https://b.ru", "https://c.ru"]
    ids = []
    for u in urls:
        jid = manager_cap1.submit(ScanInput(site_url=u), _FakeSettings())
        ids.append(jid)

    # Сразу после submit: одна задача бежит, остальные ждут в очереди.
    assert _wait(lambda: manager_cap1.any_running())
    assert manager_cap1.has_queued() is True

    # Все три доходят до done.
    assert _wait(
        lambda: all(
            manager_cap1.get(j) is not None and manager_cap1.get(j).status == "done"
            for j in ids
        ),
        timeout=15.0,
    )

    # Пик одновременности — ровно 1 (никогда не было двух Chromium сразу).
    assert probe.max_seen == 1
    # Порядок завершения строго FIFO — как отправляли.
    assert probe.completed_order == urls
    # Очередь пуста, ничего не бежит.
    assert manager_cap1.any_running() is False
    assert manager_cap1.has_queued() is False


def test_all_three_saved(manager_cap1, monkeypatch):
    """Каждая из трёх задач сохраняется и получает scan_id (последовательно)."""
    saved = []
    saved_lock = threading.Lock()

    def _fake_run_scan(scan_input, settings, progress_cb=None, should_stop=None):
        time.sleep(0.2)
        return ScanResult(scan_id="", site_url=scan_input.site_url)

    def _save(result, settings):
        with saved_lock:
            saved.append(result.site_url)
        return "id-{}".format(len(saved))

    monkeypatch.setattr(jobs_module, "run_scan", _fake_run_scan)
    monkeypatch.setattr(jobs_module.repositories, "save_scan", _save)

    urls = ["https://x1.ru", "https://x2.ru", "https://x3.ru"]
    ids = [manager_cap1.submit(ScanInput(site_url=u), _FakeSettings()) for u in urls]

    assert _wait(
        lambda: all(
            manager_cap1.get(j) is not None and manager_cap1.get(j).status == "done"
            for j in ids
        ),
        timeout=15.0,
    )
    assert sorted(saved) == sorted(urls)
    for j in ids:
        assert manager_cap1.get(j).scan_id.startswith("id-")
