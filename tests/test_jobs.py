"""
Тесты фонового менеджера задач (services/jobs.py), сессионных токенов (auth)
и smoke-тесты Streamlit-приложения (AppTest).

Сканер и БД подменяются через monkeypatch — сети и реальной записи в базу
тесты не требуют.
"""
import base64
import hashlib
import hmac as hmac_mod
import threading
import time
from pathlib import Path

import pytest

import auth
from scanner.models import ScanInput, ScanResult
from services import jobs as jobs_module
from services.jobs import JobManager

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


class _FakeSettings:
    """Минимальные настройки для тестов (без чтения env/БД)."""

    session_secret = "test-secret-0123456789abcdef"  # >=16, надёжный


@pytest.fixture()
def manager():
    """Свежий singleton JobManager на каждый тест."""
    with JobManager._instance_lock:
        JobManager._instance = None
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


def _fake_run_scan_quick(scan_input, settings, progress_cb=None, should_stop=None):
    """Быстрый фейковый скан: три строки прогресса и результат."""
    for i in range(3):
        if progress_cb:
            progress_cb("шаг {}".format(i + 1))
    return ScanResult(scan_id="", site_url=scan_input.site_url)


def _fake_run_scan_slow(scan_input, settings, progress_cb=None, should_stop=None):
    """Медленный фейковый скан (~5 с), честно реагирует на should_stop."""
    for i in range(100):
        if progress_cb:
            progress_cb("медленный шаг {}".format(i + 1))
        if should_stop and should_stop():
            return ScanResult(scan_id="", site_url=scan_input.site_url)
        time.sleep(0.05)
    return ScanResult(scan_id="", site_url=scan_input.site_url)


# ---------------------------------------------------------------------------
# JobManager
# ---------------------------------------------------------------------------
def test_submit_done_and_saved(manager, monkeypatch):
    saved = []
    monkeypatch.setattr(jobs_module, "run_scan", _fake_run_scan_quick)
    monkeypatch.setattr(
        jobs_module.repositories, "save_scan",
        lambda result, settings: (saved.append(result), "scan42")[1],
    )

    job_id = manager.submit(ScanInput(site_url="https://example.ru"), _FakeSettings())
    assert isinstance(job_id, str) and len(job_id) == 8

    assert _wait(lambda: manager.get(job_id) is not None
                 and manager.get(job_id).status == "done")
    job = manager.get(job_id)
    assert job.scan_id == "scan42"
    assert job.finished_at
    assert job.error == ""
    assert any("шаг" in line for line in job.progress)
    assert len(saved) == 1 and saved[0].site_url == "https://example.ru"


def test_stop_leads_to_stopped_status(manager, monkeypatch):
    monkeypatch.setattr(jobs_module, "run_scan", _fake_run_scan_slow)
    monkeypatch.setattr(
        jobs_module.repositories, "save_scan", lambda result, settings: "partial1"
    )

    job_id = manager.submit(ScanInput(site_url="https://slow.example.ru"), _FakeSettings())
    # Дожидаемся, что скан реально начался (появился прогресс).
    assert _wait(lambda: manager.get(job_id) is not None
                 and len(manager.get(job_id).progress) > 0)

    manager.stop(job_id)
    assert manager.get(job_id).stop_requested is True

    assert _wait(lambda: manager.get(job_id).status != "running")
    job = manager.get(job_id)
    assert job.status == "stopped"
    # Частичный результат тоже сохраняется.
    assert job.scan_id == "partial1"


def test_error_status_on_exception(manager, monkeypatch):
    def _boom(scan_input, settings, progress_cb=None, should_stop=None):
        raise RuntimeError("сбой сканера")

    monkeypatch.setattr(jobs_module, "run_scan", _boom)
    job_id = manager.submit(ScanInput(site_url="https://err.example.ru"), _FakeSettings())

    assert _wait(lambda: manager.get(job_id).status != "running")
    job = manager.get(job_id)
    assert job.status == "error"
    assert "сбой сканера" in job.error
    assert job.finished_at


def test_list_jobs_newest_first(manager, monkeypatch):
    monkeypatch.setattr(jobs_module, "run_scan", _fake_run_scan_quick)
    monkeypatch.setattr(jobs_module.repositories, "save_scan", lambda r, s: "x")

    ids = []
    for i in range(3):
        jid = manager.submit(ScanInput(site_url="https://site{}.ru".format(i)), _FakeSettings())
        ids.append(jid)
        assert _wait(lambda: manager.get(jid).status == "done")

    listed = manager.list_jobs()
    assert [j.job_id for j in listed] == list(reversed(ids))
    assert listed[0].site_url == "https://site2.ru"


def test_prune_keeps_max_jobs(manager, monkeypatch):
    monkeypatch.setattr(jobs_module, "run_scan", _fake_run_scan_quick)
    monkeypatch.setattr(jobs_module.repositories, "save_scan", lambda r, s: "x")

    for i in range(jobs_module.MAX_JOBS + 5):
        jid = manager.submit(ScanInput(site_url="https://s{}.ru".format(i)), _FakeSettings())
        assert _wait(lambda: manager.get(jid) is None
                     or manager.get(jid).status == "done")

    assert len(manager.list_jobs()) <= jobs_module.MAX_JOBS
    # Самая свежая задача не вытеснена.
    assert manager.list_jobs()[0].site_url == "https://s{}.ru".format(jobs_module.MAX_JOBS + 4)


def test_parallel_submits_smoke(manager, monkeypatch):
    monkeypatch.setattr(jobs_module, "run_scan", _fake_run_scan_quick)
    monkeypatch.setattr(jobs_module.repositories, "save_scan", lambda r, s: "x")

    results = []
    results_lock = threading.Lock()

    def _submit(i):
        jid = manager.submit(ScanInput(site_url="https://p{}.ru".format(i)), _FakeSettings())
        with results_lock:
            results.append(jid)

    threads = [threading.Thread(target=_submit, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(set(results)) == 5
    assert _wait(lambda: all(
        manager.get(jid) is not None and manager.get(jid).status == "done"
        for jid in results
    ))


def test_manager_never_raises_on_bad_input(manager):
    # get/stop по несуществующему id и submit с None не должны бросать.
    assert manager.get("nope1234") is None
    manager.stop("nope1234")
    job_id = manager.submit(None, None)
    assert isinstance(job_id, str) and len(job_id) == 8
    assert _wait(lambda: manager.get(job_id).status != "running")


# ---------------------------------------------------------------------------
# Сессионные токены (auth)
# ---------------------------------------------------------------------------
def test_session_token_roundtrip():
    settings = _FakeSettings()
    token = auth.issue_session_token("user@example.com", settings)
    assert token and token.count(".") == 2
    assert auth.verify_session_token(token, settings) == "user@example.com"


def test_session_token_wrong_secret_rejected():
    token = auth.issue_session_token("user@example.com", _FakeSettings())

    class _Other:
        session_secret = "another-secret-0123456789abcdef"  # другой, но надёжный

    assert auth.verify_session_token(token, _Other()) is None


def test_session_token_tampered_rejected():
    settings = _FakeSettings()
    token = auth.issue_session_token("user@example.com", settings)
    email_b64, expiry_ts, sig = token.split(".")
    hacker_b64 = base64.urlsafe_b64encode(b"hacker@example.com").decode("ascii").rstrip("=")
    assert auth.verify_session_token(
        "{}.{}.{}".format(hacker_b64, expiry_ts, sig), settings
    ) is None


def test_session_token_expired_rejected():
    settings = _FakeSettings()
    email = "user@example.com"
    expiry_ts = int(time.time()) - 10  # уже истёк
    payload = "{}|{}".format(email, expiry_ts).encode("utf-8")
    sig = hmac_mod.new(b"test-secret", payload, hashlib.sha256).hexdigest()
    email_b64 = base64.urlsafe_b64encode(email.encode("utf-8")).decode("ascii").rstrip("=")
    token = "{}.{}.{}".format(email_b64, expiry_ts, sig)
    assert auth.verify_session_token(token, settings) is None


def test_session_token_garbage_rejected():
    settings = _FakeSettings()
    assert auth.verify_session_token("", settings) is None
    assert auth.verify_session_token("abc", settings) is None
    assert auth.verify_session_token("a.b.c", settings) is None
    assert auth.verify_session_token(None, settings) is None


# ---------------------------------------------------------------------------
# Streamlit AppTest: логин-гейт и меню
# ---------------------------------------------------------------------------
def test_app_login_gate():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception
    # Без входа меню (radio в сайдбаре) не отображается, есть поле Email.
    assert len(at.radio) == 0
    assert any((t.label or "") == "Email" for t in at.text_input)


def test_app_authenticated_shows_menu():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.session_state["authenticated"] = True
    at.session_state["auth_email"] = "admin@example.com"
    at.run()
    assert not at.exception
    assert len(at.radio) >= 1
    options = list(at.radio[0].options)
    assert "Проверка одного сайта" in options
    assert "Дашборд" in options
