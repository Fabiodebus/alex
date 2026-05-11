"""Internal cron scheduler.

Backed by APScheduler's ``AsyncIOScheduler``. Feature WOs register their
jobs against this scheduler; the runtime starts/stops it as part of the
FastAPI lifespan.
"""
from __future__ import annotations

from collections.abc import Callable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = structlog.get_logger(__name__)


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            log.info("scheduler.started")

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            log.info("scheduler.stopped")

    def add_interval_job(
        self,
        func: Callable,
        *,
        seconds: int,
        job_id: str,
        replace_existing: bool = True,
    ) -> None:
        self._scheduler.add_job(
            func,
            IntervalTrigger(seconds=seconds),
            id=job_id,
            replace_existing=replace_existing,
        )
        log.info("scheduler.job_registered", job_id=job_id, trigger="interval", seconds=seconds)

    def add_cron_job(
        self,
        func: Callable,
        *,
        cron: str,
        job_id: str,
        replace_existing: bool = True,
    ) -> None:
        self._scheduler.add_job(
            func,
            CronTrigger.from_crontab(cron),
            id=job_id,
            replace_existing=replace_existing,
        )
        log.info("scheduler.job_registered", job_id=job_id, trigger="cron", cron=cron)
