"""Background jobs — seeding a model family takes seconds, so it cannot block a
request. One job at a time per tenant, with progress the dashboard can poll.

Deliberately not a queue. A queue is the right answer for the production service
(publishers pushing 14 GB checkpoints) and the wrong answer for a demo that has
to start with `python3 run.py` and nothing else installed.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field


@dataclass
class Job:
    """A unit of background work and everything the UI needs to render it."""

    job_id: str
    kind: str
    tenant_id: str
    state: str = "running"  # running | done | failed
    message: str = ""
    fraction: float = 0.0
    started_unix: float = field(default_factory=lambda: round(time.time(), 3))
    finished_unix: float | None = None
    result: dict | None = None
    error: str | None = None
    steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "tenant_id": self.tenant_id,
            "state": self.state,
            "message": self.message,
            "fraction": round(self.fraction, 4),
            "started_unix": self.started_unix,
            "finished_unix": self.finished_unix,
            "elapsed_seconds": round((self.finished_unix or time.time()) - self.started_unix, 2),
            "result": self.result,
            "error": self.error,
            "steps": self.steps[-40:],
        }


class JobRunner:
    """Runs one job per tenant and keeps the last one for inspection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._counter = 0

    def current(self, tenant_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(tenant_id)

    def busy(self, tenant_id: str) -> bool:
        job = self.current(tenant_id)
        return job is not None and job.state == "running"

    def submit(self, tenant_id: str, kind: str, target) -> Job:
        """Start `target(job)` on a thread. Raises if that tenant is already busy."""
        with self._lock:
            existing = self._jobs.get(tenant_id)
            if existing is not None and existing.state == "running":
                raise JobBusy(f"tenant {tenant_id} already has a running job ({existing.kind})")
            self._counter += 1
            job = Job(job_id=f"job-{self._counter}", kind=kind, tenant_id=tenant_id)
            self._jobs[tenant_id] = job

        def run() -> None:
            try:
                job.result = target(job)
                job.state = "done"
                job.fraction = 1.0
                job.message = "complete"
            except Exception as exc:  # noqa: BLE001 — a job thread must record, never vanish
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = "failed"
                # Full trace to the server log; the API returns only the summary,
                # because a stack trace in an HTTP response is an information leak.
                traceback.print_exc()
            finally:
                job.finished_unix = round(time.time(), 3)

        threading.Thread(target=run, name=f"ccp-{job.job_id}", daemon=True).start()
        return job


class JobBusy(RuntimeError):
    """A job is already running for this tenant."""
