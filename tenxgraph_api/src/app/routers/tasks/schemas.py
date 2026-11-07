"""Request bodies and the schedule record of the /v1/tasks routes."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tenxgraph_api.src.app.core.auth.request_config import ClientInputError


class Schedule(BaseModel):
    """Exactly one of ``cron`` (cron expression), ``at`` (ISO time, once) or ``every``
    (seconds)."""

    model_config = ConfigDict(extra="forbid")

    cron: str | None = None
    at: datetime | None = None
    every: int | None = Field(default=None, gt=0)

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, value: str | None) -> str | None:
        if value is None:
            return value
        import pycron  # what TaskIQ's scheduler matches cron with

        try:
            pycron.is_now(value)
        except Exception:
            raise ClientInputError("schedule.cron is not a valid cron expression") from None
        return value

    @model_validator(mode="after")
    def _exactly_one(self) -> Schedule:
        if sum(value is not None for value in (self.cron, self.at, self.every)) != 1:
            raise ClientInputError('schedule needs exactly one of "cron", "at" or "every"')
        return self


class TaskCall(BaseModel):
    """``POST /v1/tasks/{name}``: run now, or schedule when ``schedule`` is set."""

    model_config = ConfigDict(extra="forbid")

    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    schedule: Schedule | None = None


class ScheduleChange(BaseModel):
    """``PATCH /v1/tasks/{schedule_id}``: the new time only."""

    model_config = ConfigDict(extra="forbid")

    schedule: Schedule


class ScheduleRecord(BaseModel):
    """Kept in the cache store as ``jobs:schedule:<id>``: what GET shows and what PATCH
    needs to add the schedule again (TaskIQ sources have no read-by-id)."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    task_name: str
    owner: str
    schedule: Schedule
    args: list[Any]
    kwargs: dict[str, Any]
    config: dict[str, Any]  # the creator's run config; never returned
    created_at: float = Field(default_factory=time.time)
