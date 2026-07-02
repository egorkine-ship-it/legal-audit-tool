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
STATUS_QUEUED = "queued"    # принята, ждёт свободного слота
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"

# Завершённые (терминальные) статусы — задача больше не займёт слот.
_TERMINAL_STATUSES = (STATUS_DONE, STATUS_ERROR, STATUS_STOPPED)


class Job:
    """Одна фоновая проверка (лёгкий изменяемый объект, без pydantic)."""

    def __init__(self, job_id: str, site_url: str = "", company_name: str = "") -> None:
        self.job_id = job_id
        self.site_url = site_url
        self.company_name = company_name
        # Задача рождается в очереди; воркер стартует только при свободном слоте.
        self.status = STATUS_QUEUED
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
    """Process-global реестр фоновых проверок.

    Ограничение параллелизма: Chromium тяжёлый, несколько одновременных
    проверок «голодят» контейнер (риск 502). Поэтому одновременно выполняется
    не более ``_MAX_CONCURRENT`` задач; остальные ждут в статусе ``queued`` и
    стартуют по FIFO, как только освобождается слот.
    """

    # Максимум одновременно выполняющихся проверок (по умолчанию 1).
    _MAX_CONCURRENT = 1

    _instance: Optional["JobManager"] = None
    _instance_lock = threading.Lock()

    def __init__(self, max_concurrent: Optional[int] = None) -> None:
        self._lock = threading.Lock()
        # dict сохраняет порядок вставки (порядок отправки задач == FIFO очередь).
        self._jobs: "dict[str, Job]" = {}
        # Входные данные ещё не стартовавших задач: job_id -> (scan_input, settings).
        self._pending: "dict[str, tuple]" = {}
        # Предел параллелизма экземпляра (>=1). По умолчанию — класс-атрибут.
        try:
            limit = int(max_concurrent) if max_concurrent is not None else int(self._MAX_CONCURRENT)
        except Exception:
            limit = 1
        self.max_concurrent = limit if limit >= 1 else 1

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
        """Принять проверку в фон. Вернуть job_id сразу (никогда не бросает).

        Задача регистрируется в статусе ``queued`` и стартует немедленно, если
        есть свободный слот; иначе ждёт освобождения (FIFO). Метод не блокирует
        вызывающего в любом случае.
        """
        job_id = uuid.uuid4().hex[:8]
        try:
            job = Job(
                job_id=job_id,
                site_url=str(getattr(scan_input, "site_url", "") or ""),
                company_name=str(getattr(scan_input, "company_name", "") or ""),
            )
            with self._lock:
                self._jobs[job_id] = job
                # Запоминаем вход задачи для отложенного старта из очереди.
                self._pending[job_id] = (scan_input, settings)
                self._prune_locked()
            # Пытаемся занять свободные слоты (в т.ч. этой задачей).
            self._launch_queued()
        except Exception as exc:  # предохранитель: задача остаётся с ошибкой
            try:
                with self._lock:
                    self._pending.pop(job_id, None)
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

    def has_queued(self) -> bool:
        """Есть ли задачи, ждущие свободного слота (статус ``queued``)."""
        try:
            with self._lock:
                return any(j.status == STATUS_QUEUED for j in self._jobs.values())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------
    def _launch_queued(self) -> None:
        """Занять свободные слоты очередными задачами (FIFO). Никогда не бросает.

        Пока число выполняющихся задач меньше предела, берём самую раннюю
        (по порядку отправки) ``queued``-задачу, помечаем её ``running`` и
        запускаем daemon-поток. Всё под локом, кроме собственно старта потока.
        """
        try:
            while True:
                to_start = None  # (job, scan_input, settings)
                with self._lock:
                    running = sum(
                        1 for j in self._jobs.values() if j.status == STATUS_RUNNING
                    )
                    if running >= self.max_concurrent:
                        break
                    # Порядок вставки в dict == FIFO очередь отправки.
                    for job in self._jobs.values():
                        if job.status == STATUS_QUEUED:
                            scan_input, settings = self._pending.pop(
                                job.job_id, (None, None)
                            )
                            job.status = STATUS_RUNNING
                            to_start = (job, scan_input, settings)
                            break
                    if to_start is None:
                        break
                # Старт потока — вне лока (создание потока не должно держать лок).
                job, scan_input, settings = to_start
                try:
                    thread = threading.Thread(
                        target=self._run_job,
                        args=(job, scan_input, settings),
                        daemon=True,
                    )
                    thread.start()
                except Exception as exc:
                    # Поток не создался — задача не «повисает» в running.
                    try:
                        with self._lock:
                            job.error = str(exc)
                            job.status = STATUS_ERROR
                            job.finished_at = datetime.now().isoformat(timespec="seconds")
                    except Exception:
                        pass
                    # Слот освободился (running-счётчик снизился) — пробуем дальше.
        except Exception:
            pass

    def _prune_locked(self) -> None:
        """Удалить старейшие ЗАВЕРШЁННЫЕ задачи сверх лимита (лок уже взят).

        Вытесняем только терминальные (done/error/stopped): выполняющиеся и
        ждущие в очереди задачи трогать нельзя, иначе потеряется ход проверки.
        """
        try:
            overflow = len(self._jobs) - MAX_JOBS
            if overflow <= 0:
                return
            for jid in [
                j.job_id for j in self._jobs.values() if j.status in _TERMINAL_STATUSES
            ]:
                if overflow <= 0:
                    break
                self._jobs.pop(jid, None)
                self._pending.pop(jid, None)  # на всякий случай (у терминальных нет)
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
            # Слот освободился — запускаем следующую задачу из очереди (FIFO).
            self._launch_queued()
