"""Background jobs for the admin service: one at a time, with steps and logs.

controller-admin runs as a single process, so job state lives in memory. A
restart loses the history of finished jobs, nothing else.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from typing import Callable, Optional

MAX_LOG_LINES = 2000
KEEP_JOBS = 50


class Job:
    def __init__(self, title: str, echo: Optional[Callable[[str], None]] = None):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.status = "queued"          # queued | running | done | failed
        self.error: Optional[str] = None
        self.steps: list[dict] = []
        self.lines: list[str] = []
        self.created = time.time()
        self.finished: Optional[float] = None
        self._echo = echo
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]
        if self._echo:
            self._echo(msg)

    @contextmanager
    def step(self, name: str):
        entry = {"name": name, "status": "running", "detail": ""}
        with self._lock:
            self.steps.append(entry)
        self.log(f"▸ {name}")
        try:
            yield entry
        except Exception as e:
            entry["status"] = "failed"
            entry["detail"] = str(e)
            raise
        else:
            entry["status"] = "done"

    def to_dict(self, with_log: bool = True) -> dict:
        with self._lock:
            d = {
                "id": self.id, "title": self.title, "status": self.status,
                "error": self.error, "steps": [dict(s) for s in self.steps],
                "created": self.created, "finished": self.finished,
            }
            if with_log:
                d["log"] = list(self.lines[-400:])
        return d


class _JobLogHandler(logging.Handler):
    """Copy log records emitted on the worker thread into the current job,
    so bulk_spawn/import_flags output shows up in the dashboard."""

    def __init__(self, runner: "JobRunner"):
        super().__init__(level=logging.INFO)
        self.runner = runner

    def emit(self, record: logging.LogRecord) -> None:
        job = self.runner.current
        if job is not None and record.thread == self.runner.thread_id:
            job.log(record.getMessage())


class JobRunner:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.q: "queue.Queue[tuple[Job, Callable[[Job], None]]]" = queue.Queue()
        self.current: Optional[Job] = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="admin-jobs")
        self.thread_id: Optional[int] = None
        logging.getLogger().addHandler(_JobLogHandler(self))
        self.thread.start()

    def submit(self, title: str, fn: Callable[[Job], None]) -> Job:
        job = Job(title)
        self.jobs[job.id] = job
        self.order.append(job.id)
        while len(self.order) > KEEP_JOBS:
            old = self.order.pop(0)
            if self.jobs[old].status in ("done", "failed"):
                self.jobs.pop(old, None)
            else:
                self.order.insert(0, old)
                break
        self.q.put((job, fn))
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def recent(self, n: int = 10) -> list[dict]:
        return [self.jobs[i].to_dict(with_log=False) for i in reversed(self.order[-n:]) if i in self.jobs]

    def busy(self) -> bool:
        return self.current is not None or not self.q.empty()

    def _loop(self) -> None:
        self.thread_id = threading.get_ident()
        while True:
            job, fn = self.q.get()
            self.current = job
            job.status = "running"
            try:
                fn(job)
                job.status = "done"
                job.log("✔ finished")
            except Exception as e:  # report, keep the worker alive
                job.status = "failed"
                job.error = str(e) or e.__class__.__name__
                job.log(f"✘ {job.error}")
                logging.getLogger(__name__).debug(traceback.format_exc())
            finally:
                job.finished = time.time()
                self.current = None


def run_inline(title: str, fn: Callable[[Job], None], echo: Callable[[str], None]) -> Job:
    """Run a job in the current thread (CLI), echoing log lines."""
    job = Job(title, echo=echo)

    class _H(logging.Handler):
        def emit(self, record):
            job.log(record.getMessage())

    h = _H(level=logging.INFO)
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)
    job.status = "running"
    try:
        fn(job)
        job.status = "done"
    except Exception as e:
        job.status = "failed"
        job.error = str(e) or e.__class__.__name__
    finally:
        root.removeHandler(h)
        job.finished = time.time()
    return job
