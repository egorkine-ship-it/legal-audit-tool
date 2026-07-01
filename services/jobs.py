"""
Менеджер фоновых проверок (JobManager).

Проверки запускаются в daemon-потоках процесса, поэтому переживают
перезагрузку страницы и переключение вкладок в Streamlit: пользователь
после reload «переподключается» к уже идущей проверке.

Особенности:
  * process-global singleton (JobManager.instance());
  * потокобезопасный реестр задач (lock вокруг всех операций);
  * прогресс копится в job.progress (хвост из MAX_PROGRESS_LINES строк);
  * остановка через stop(job_id) -> should_stop() внутри run_scan;
  * ни один публичный метод не бросает исключений наружу.

Playwright-заметка: поток задачи — «чистый» (без активного asyncio event
loop), поэтому sync API Playwright внутри run_scan работает корректно.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import List, Optional

# Точки импорта для monkeypatch в тестах: jobs.run_scan / jobs.repositories.
# Импорт защищён: при недоступности тяжёлых зависимостей модуль всё равно
# импортируется (задача завершится со статусом error).
try:
    from scanner.orchestrator import run_scan
except Exception:  # pragma: no cover - деградация без сканера
    run_scan = None  # type: ignore[assignment]

try:
    from database import repositories
except Exception:  # pragma: no cover - деградация без БД
    repositories = None  # type: ignore[assignment]

# Ограничения реестра.
MAX_JOBS = 30            # максимум задач в памяти
MAX_PROGRESS_LINES = 200  # хвост прогресса на задачу

# Статусы задач.
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"


class Job:
    """Одна фоновая проверка (лёгкий изменяемый объект, без pydantic)."""

    def __init__(self, job_id: str, site_url: str = "", company_name: str = "") -> None:
        self.job_id = job_id
        self.site_url = site_url
        self.company_name = company_name
        self.status = STATUS_RUNNING
        self.progress: List[str] = []
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.finished_at = ""
        self.scan_id = ""
        self.error = ""
        self.stop_requested = False

    def to_dict(self) -> dict:
        """Снимок состояния (для отладки/UI)."""
        return {
            "job_id": self.job_id,
            "site_url": self.site_url,
            "company_name": self.company_name,
            "status": self.status,
            "progress": list(self.progress),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "scan_id": self.scan_id,
            "error": self.error,
            "stop_requested": self.stop_requested,
        }


class JobManager:
    """Process-global реестр фоновых проверок."""

    _instance: Optional["JobManager"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # dict сохраняет порядок вставки (порядок отправки задач).
        self._jobs: "dict[str, Job]" = {}

    @classmethod
    def instance(cls) -> "JobManager":
        """Единственный экземпляр на процесс (потокобезопасно)."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def submit(self, scan_input, settings) -> str:
        """Запустить проверку в фоне. Вернуть job_id (никогда не бросает)."""
        job_id = uuid.uuid4().hex[:8]
        try:
            job = Job(
                job_id=job_id,
                site_url=str(getattr(scan_input, "site_url", "") or ""),
                company_name=str(getattr(scan_input, "company_name", "") or ""),
            )
            with self._lock:
                self._jobs[job_id] = job
                self._prune_locked()
            thread = threading.Thread(
                target=self._run_job,
                args=(job, scan_input, settings),
                name="scan-job-{}".format(job_id),
                daemon=True,
            )
            thread.start()
        except Exception as exc:  # предохранитель: задача остаётся с ошибкой
            try:
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is not None:
                        job.status = STATUS_ERROR
                        job.error = str(exc)
                        job.finished_at = datetime.now().isoformat(timespec="seconds")
            except Exception:
                pass
        return job_id

    def stop(self, job_id: str) -> None:
        """Запросить остановку задачи (сканер завершится на ближайшем шаге)."""
        try:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job.stop_requested = True
        except Exception:
            pass

    def get(self, job_id: str) -> Optional[Job]:
        """Задача по id (или None)."""
        try:
            with self._lock:
                return self._jobs.get(job_id)
        except Exception:
            return None

    def list_jobs(self) -> List[Job]:
        """Все задачи, новые первыми."""
        try:
            with self._lock:
                return list(reversed(list(self._jobs.values())))
        except Exception:
            return []

    def any_running(self) -> bool:
        """Есть ли хоть одна выполняющаяся задача."""
        try:
            with self._lock:
                return any(j.status == STATUS_RUNNING for j in self._jobs.values())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------
    def _prune_locked(self) -> None:
        """Удалить старейшие ЗАВЕРШЁННЫЕ задачи сверх лимита (лок уже взят)."""
        try:
            overflow = len(self._jobs) - MAX_JOBS
            if overflow <= 0:
                return
            for jid in [j.job_id for j in self._jobs.values() if j.status != STATUS_RUNNING]:
                if overflow <= 0:
                    break
                self._jobs.pop(jid, None)
                overflow -= 1
        except Exception:
            pass

    def _append_progress(self, job: Job, message: str) -> None:
        """Добавить строку прогресса, храня только хвост."""
        try:
            with self._lock:
                job.progress.append(str(message))
                if len(job.progress) > MAX_PROGRESS_LINES:
                    del job.progress[: len(job.progress) - MAX_PROGRESS_LINES]
        except Exception:
            pass

    def _run_job(self, job: Job, scan_input, settings) -> None:
        """Тело daemon-потока: run_scan -> save_scan -> финальный статус."""
        try:
            if run_scan is None:
                raise RuntimeError("Сканер недоступен (scanner.orchestrator не импортируется)")

            result = run_scan(
                scan_input,
                settings,
                progress_cb=lambda msg: self._append_progress(job, msg),
                should_stop=lambda: job.stop_requested,
            )

            # Сохраняем результат (в т.ч. частичный после остановки).
            scan_id = ""
            try:
                if repositories is not None:
                    scan_id = repositories.save_scan(result, settings) or ""
            except Exception as exc:
                self._append_progress(job, "Не удалось сохранить в базу: {}".format(exc))
            if not scan_id:
                scan_id = str(getattr(result, "scan_id", "") or "")
            job.scan_id = scan_id

            job.status = STATUS_STOPPED if job.stop_requested else STATUS_DONE
        except Exception as exc:
            job.error = str(exc)
            job.status = STATUS_STOPPED if job.stop_requested else STATUS_ERROR
        finally:
            job.finished_at = datetime.now().isoformat(timespec="seconds")
