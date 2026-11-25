"""Request bodies and the schedule record of the /v1/tasks routes."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tenxgraph_api.jobs.schedule_rules import check_cron, check_every
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError


class Schedule(BaseModel):
    """Exactly one of ``cron`` (cron expression, UTC), ``at`` (ISO time, once; a time without
    an offset is UTC, any offset is converted to UTC) or ``every`` (seconds)."""

    model_config = ConfigDict(extra="forbid")

    cron: str | None = None
    at: datetime | None = None
    every: int | None = None

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, value: str | None, info: ValidationInfo) -> str | None:
        # Stored records are read back as written, so one saved before the strict check can
        # still be read, changed and cancelled.
        if value is None or (info.context or {}).get("stored"):
            return value
        try:
            return check_cron(value)
        except ValueError as exc:
            raise ClientInputError(f"schedule.{exc}") from None

    @field_validator("every")
    @classmethod
    def _check_every(cls, value: int | None, info: ValidationInfo) -> int | None:
        if value is None or (info.context or {}).get("stored"):
            return value
        try:
            check_every(value)
        except ValueError as exc:
            raise ClientInputError(f"schedule.{exc}") from None
        return value

    @field_validator("at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

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
    user_id: str | None = None  # the owner's user_id and tenant values, for reach checks
    tenant: dict[str, str] = {}
    schedule: Schedule
    args: list[Any]
    kwargs: dict[str, Any]
    config: dict[str, Any]  # the creator's run config; never returned
    slot_id: str | None = None  # this schedule's slot in the owner's schedule cap
    created_at: float = Field(default_factory=time.time)
