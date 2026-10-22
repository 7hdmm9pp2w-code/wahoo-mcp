"""Planner sessions: the compact step format of push_session, turned into a
Wahoo plan plus a scheduled workout."""

import datetime as dt
import math
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    BIKING_INDOOR_WORKOUT_TYPE_ID,
    MAX_REPEAT,
    WorkoutInterval,
    WorkoutPlan,
    WorkoutTarget,
    local_timezone,
)

EXTERNAL_ID_PREFIX = "pbp"


def slugify(text: str) -> str:
    """ASCII slug for IDs: 'Angels Wochenende' -> 'angels-wochenende'."""
    # Drop accents (Ä -> A), but turn other symbols such as × into separators
    text = "".join(
        char if char.isascii() else " "
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def session_id(day: dt.date, name: str) -> str:
    """Stable ID for plan external_id and workout_token."""
    slug = slugify(name)
    if not slug:
        raise ValueError(f"Cannot derive an ID from name {name!r}; pass session_id")
    return f"{EXTERNAL_ID_PREFIX}-{day.isoformat()}-{slug}"


class SessionStep(BaseModel):
    """One step of a planner session, or a repeat block of steps."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    minutes: float | None = Field(None, gt=0)
    seconds: int | None = Field(None, gt=0)
    percent_ftp: float | None = Field(None, description="Fraction of FTP, e.g. 0.65")
    percent_ftp_min: float | None = None
    percent_ftp_max: float | None = None
    watts: int | None = Field(None, gt=0)
    type: str | None = Field(None, description="warmup, active, rest, cooldown, ...")
    repeat: int | None = Field(None, ge=2, le=MAX_REPEAT)
    steps: list["SessionStep"] | None = None

    @model_validator(mode="after")
    def _check(self) -> "SessionStep":
        if self.repeat is not None or self.steps is not None:
            if self.repeat is None or not self.steps:
                raise ValueError("a repeat block needs repeat and steps")
            return self
        if (self.minutes is None) == (self.seconds is None):
            raise ValueError(f"step {self.name or ''!r} needs minutes or seconds")
        given = [
            self.percent_ftp is not None,
            self.percent_ftp_min is not None or self.percent_ftp_max is not None,
            self.watts is not None,
        ]
        if sum(given) > 1:
            raise ValueError(
                f"step {self.name or ''!r}: give only one of percent_ftp, "
                "percent_ftp_min/max or watts"
            )
        return self

    def duration_seconds(self) -> int:
        if self.seconds is not None:
            return self.seconds
        return round(self.minutes * 60)

    def target(self) -> WorkoutTarget | None:
        if self.watts is not None:
            return WorkoutTarget(target_type="watts", target_value=self.watts)
        if self.percent_ftp is not None:
            return WorkoutTarget(
                target_type="percent_ftp", target_value=self.percent_ftp
            )
        if self.percent_ftp_min is not None or self.percent_ftp_max is not None:
            return WorkoutTarget(
                target_type="percent_ftp",
                target_min=self.percent_ftp_min,
                target_max=self.percent_ftp_max,
            )
        return None

    def to_interval(self) -> WorkoutInterval:
        if self.repeat is not None:
            return WorkoutInterval(
                name=self.name,
                interval_type=self.type or "active",
                repeat=self.repeat,
                intervals=[step.to_interval() for step in self.steps],
            )
        target = self.target()
        return WorkoutInterval(
            name=self.name,
            duration=self.duration_seconds(),
            targets=[target] if target else [],
            interval_type=self.type or "active",
        )


class Session(BaseModel):
    """A planner session as accepted by push_session."""

    model_config = ConfigDict(extra="forbid")

    date: dt.date
    time: dt.time = Field(description="Local start time in WAHOO_TIMEZONE")
    name: str = Field(min_length=1)
    steps: list[SessionStep] = Field(min_length=1)
    description: str | None = None
    session_id: str | None = Field(
        None, description="Overrides the derived pbp-<date>-<name> ID"
    )
    workout_type: str = "bike"
    workout_type_id: int = BIKING_INDOOR_WORKOUT_TYPE_ID
    ftp: int | None = Field(None, gt=0)

    def id(self) -> str:
        return self.session_id or session_id(self.date, self.name)

    def starts(self) -> dt.datetime:
        return dt.datetime.combine(self.date, self.time, tzinfo=local_timezone())

    def to_plan(self) -> WorkoutPlan:
        return WorkoutPlan(
            name=self.name,
            description=self.description,
            intervals=[step.to_interval() for step in self.steps],
            workout_type=self.workout_type,
            ftp=self.ftp,
        )

    def minutes(self, plan: WorkoutPlan) -> int:
        return math.ceil(plan.total_duration() / 60)


def describe_watts(plan_json: dict[str, Any]) -> str:
    """Readable step list with the absolute targets sent to Wahoo."""

    def lines(intervals: list[dict], indent: str) -> list[str]:
        out = []
        for interval in intervals:
            name = interval.get("name") or interval["intensity_type"]
            if interval["exit_trigger_type"] == "repeat":
                out.append(f"{indent}{interval['exit_trigger_value'] + 1}x {name}:")
                out.extend(lines(interval["intervals"], indent + "  "))
                continue
            target = interval["targets"][0]
            if target["type"] == "watts":
                low, high = round(target["low"]), round(target["high"])
                value = f"{low} W" if low == high else f"{low}-{high} W"
            else:
                value = f"{target['type']} {target['low']}-{target['high']}"
            seconds = interval["exit_trigger_value"]
            duration = f"{seconds // 60}:{seconds % 60:02d}"
            out.append(f"{indent}- {duration} {name}: {value}")
        return out

    return "\n".join(lines(plan_json["intervals"], ""))
