# Run: celery -A app.worker worker --loglevel=info   (Windows needs --pool=solo)

from __future__ import annotations

import asyncio

from celery import Celery
from celery.signals import worker_init

from app.config import get_settings, setup_logging
from app.database.database import init_db
from app.pipeline import run_job

settings = get_settings()

celery_app = Celery("web_scraper", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=900,
    task_soft_time_limit=840,
    broker_connection_retry_on_startup=True,
)


@worker_init.connect
def _setup(**_kwargs) -> None:
    setup_logging(settings)
    init_db()


@celery_app.task(name="scrape.run_job")
def run_scrape_job(job_id: str) -> str:
    asyncio.run(run_job(job_id))
    return job_id
