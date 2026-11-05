"""Background work: Cloud Tasks in GCP, an in-process worker locally.

A task is "POST <path> with JSON payload" against the service itself. Cloud Tasks attaches
an OIDC token for the service's own service account, so the handler authenticates it like
any other service call. Locally, handlers are registered by path and run on the event loop
(tests can `await queue.drain()` to wait for completion).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

log = logging.getLogger("eap.tasks")

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class TaskQueue:
    async def enqueue(self, path: str, payload: dict[str, Any], *, task_id: str | None = None, delay_s: int = 0) -> str:
        raise NotImplementedError  # pragma: no cover


class LocalTaskQueue(TaskQueue):
    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self._pending: set[asyncio.Task] = set()
        self._seen: set[str] = set()

    def register(self, path: str, handler: Handler) -> None:
        self.handlers[path] = handler

    async def enqueue(self, path: str, payload: dict[str, Any], *, task_id: str | None = None, delay_s: int = 0) -> str:
        if task_id and task_id in self._seen:
            return task_id
        if task_id:
            self._seen.add(task_id)
        handler = self.handlers.get(path)
        if handler is None:
            raise KeyError(f"no local task handler for {path}")

        async def _run():
            if delay_s:
                await asyncio.sleep(delay_s)
            try:
                await handler(payload)
            except Exception:
                log.exception("local task %s failed", path)

        t = asyncio.get_running_loop().create_task(_run())
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)
        return task_id or str(id(t))

    async def drain(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)


class CloudTasksQueue(TaskQueue):
    def __init__(self, queue: str, base_url: str, service_account_email: str):
        self.queue = queue  # projects/<p>/locations/<l>/queues/<q>
        self.base_url = base_url.rstrip("/")
        self.sa = service_account_email

    async def enqueue(self, path: str, payload: dict[str, Any], *, task_id: str | None = None, delay_s: int = 0) -> str:
        from . import gcp

        return await gcp.cloud_tasks_create(
            self.queue,
            self.base_url + path,
            payload,
            service_account_email=self.sa,
            audience=self.base_url,
            schedule_delay_s=delay_s,
            task_id=task_id,
        )


def queue_from_env(var_prefix: str = "EAP_TASKS_") -> TaskQueue:
    q = os.environ.get(f"{var_prefix}QUEUE")
    if q:
        return CloudTasksQueue(q, os.environ[f"{var_prefix}BASE_URL"], os.environ[f"{var_prefix}SERVICE_ACCOUNT"])
    return LocalTaskQueue()
