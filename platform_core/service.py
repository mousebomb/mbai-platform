"""进程级单例：API 与 CLI 共用同一份 store / worker。"""

from __future__ import annotations

from .settings import settings
from .store import JobStore
from .worker import Worker

settings.ensure_dirs()

store = JobStore(settings.db_path)
worker = Worker(store)
