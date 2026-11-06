"""Tác vụ chạy lâu (provision, tạo máy, cài SSH key): chạy nền, UI hỏi log theo id."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

MAX_JOBS = 200


@dataclass
class Job:
    id: int
    kind: str
    box: str
    title: str
    status: str = "running"          # running | ok | failed
    lines: List[str] = field(default_factory=list)
    result: Any = None
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None

    def log(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > 2000:
            del self.lines[:500]

    def as_dict(self, since: int = 0) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "box": self.box, "title": self.title,
                "status": self.status, "result": self.result, "started": self.started,
                "finished": self.finished, "lines": self.lines[since:], "nlines": len(self.lines)}


class JobManager:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.jobs: Dict[int, Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, kind: str, box: str, title: str,
              fn: Callable[[Job], Awaitable[Any]]) -> Job:
        job = Job(id=next(self._ids), kind=kind, box=box, title=title)
        self.jobs[job.id] = job
        while len(self.jobs) > MAX_JOBS:
            del self.jobs[min(self.jobs)]

        async def runner() -> None:
            try:
                job.result = await fn(job)
                if job.status == "running":
                    job.status = "ok"
            except Exception as e:  # mọi lỗi đều phải hiện lên UI
                log.warning("job %s (%s) lỗi: %s", job.id, title, e)
                job.log(f"LỖI: {e}")
                job.status = "failed"
            finally:
                job.finished = time.time()

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def running(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "running")

    def get(self, job_id: int) -> Job:
        return self.jobs[job_id]

    def recent(self, box: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        js = [j for j in self.jobs.values() if box is None or j.box == box]
        js.sort(key=lambda j: j.id, reverse=True)
        return [{k: v for k, v in j.as_dict().items() if k != "lines"} for j in js[:limit]]
