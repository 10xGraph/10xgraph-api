"""The TaskIQ scheduler: ``10xgraph scheduler`` runs ``tenxgraph_api.jobs.scheduler:scheduler``.

Sources: static ``schedule=`` labels on tasks, plus the Redis source for dynamic schedules when
the result backend is Redis (any broker but ``memory``). Run exactly one scheduler (a TaskIQ rule).
"""

from taskiq import ScheduleSource, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from tenxgraph_api.jobs.config import build_schedule_source
from tenxgraph_api.jobs.worker import broker, worker_config


sources: list[ScheduleSource] = [LabelScheduleSource(broker)]
redis_source = build_schedule_source(worker_config) if worker_config is not None else None
if redis_source is not None:
    sources.append(redis_source)

scheduler = TaskiqScheduler(broker, sources=sources)
