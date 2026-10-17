"""Tests for repeat blocks and the push_session planner format."""

import base64
import json
import time
from datetime import date, datetime
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest
from pydantic import ValidationError

from src.models import WorkoutInterval, WorkoutPlan, WorkoutTarget
from src.server import call_tool
from src.session import Session, SessionStep, session_id, slugify

API = "https://api.wahooligan.com"


def _watts(value):
    return [WorkoutTarget(target_type="watts", target_value=value)]


class TestRepeatBlocks:
    def _plan(self):
        return WorkoutPlan(
            name="4x8",
            intervals=[
                WorkoutInterval(duration=600, targets=_watts(120)),
                WorkoutInterval(
                    repeat=4,
                    intervals=[
                        WorkoutInterval(duration=480, targets=_watts(206)),
                        WorkoutInterval(
                            duration=240, targets=_watts(115), interval_type="rest"
                        ),
                    ],
                ),
            ],
        )

    def test_repeat_value_counts_passes_after_the_first(self):
        block = self._plan().to_wahoo_format(ftp=229)["intervals"][1]
        assert block["exit_trigger_type"] == "repeat"
        assert block["exit_trigger_value"] == 3
        assert "targets" not in block
        assert [child["exit_trigger_value"] for child in block["intervals"]] == [
            480,
            240,
        ]

    def test_total_duration_counts_every_pass(self):
        assert self._plan().total_duration() == 600 + 4 * (480 + 240)

    def test_repeat_without_intervals_is_rejected(self):
        with pytest.raises(ValidationError, match="nested intervals"):
            WorkoutInterval(repeat=3)

    def test_repeat_with_duration_is_rejected(self):
        with pytest.raises(ValidationError, match="no duration"):
            WorkoutInterval(
                repeat=3,
                duration=60,
                intervals=[WorkoutInterval(duration=60, targets=_watts(100))],
            )

    def test_nested_intervals_need_repeat(self):
        with pytest.raises(ValidationError, match="need repeat"):
            WorkoutInterval(intervals=[WorkoutInterval(duration=60)])

    def test_implausible_repeat_is_rejected(self):
        with pytest.raises(ValidationError):
            WorkoutInterval(repeat=40, intervals=[WorkoutInterval(duration=60)])


class TestIds:
    def test_slugify_handles_umlauts_and_spaces(self):
        assert slugify("Ängels Über-Unter 4×8") == "angels-uber-unter-4-8"

    def test_session_id(self):
        assert session_id(date(2026, 10, 18), "Angels") == "pbp-2026-10-18-angels"

    def test_name_without_letters_needs_explicit_id(self):
        with pytest.raises(ValueError, match="session_id"):
            session_id(date(2026, 10, 18), "!!!")


class TestSessionSteps:
    def test_minutes_or_seconds_required(self):
        with pytest.raises(ValidationError, match="minutes or seconds"):
            SessionStep(percent_ftp=0.6)

    def test_only_one_target_kind(self):
        with pytest.raises(ValidationError, match="only one"):
            SessionStep(minutes=5, percent_ftp=0.6, watts=140)

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            SessionStep(minutes=5, percent=0.6)

    def test_session_to_plan(self):
        session = Session(
            date="2026-10-18",
            time="18:00",
            name="Angels",
            steps=[
                {
                    "name": "Einfahren",
                    "minutes": 10,
                    "percent_ftp": 0.55,
                    "type": "warmup",
                },
                {
                    "repeat": 3,
                    "steps": [
                        {"minutes": 8, "percent_ftp": 0.9},
                        {
                            "minutes": 4,
                            "percent_ftp_min": 0.45,
                            "percent_ftp_max": 0.55,
                        },
                    ],
                },
                {"minutes": 5, "type": "cooldown"},
            ],
        )
        plan = session.to_plan()
        intervals = plan.to_wahoo_format(ftp=229)["intervals"]
        assert intervals[0]["targets"] == [{"type": "watts", "low": 126, "high": 126}]
        assert intervals[1]["exit_trigger_value"] == 2
        assert intervals[1]["intervals"][0]["targets"][0]["low"] == 206
        assert intervals[1]["intervals"][1]["targets"][0] == {
            "type": "watts",
            "low": 103,
            "high": 126,
        }
        assert intervals[2]["targets"] == [{"type": "rpe", "low": 1, "high": 10}]
        assert session.minutes(plan) == 10 + 3 * 12 + 5
        assert session.starts() == datetime.fromisoformat("2026-10-18T18:00:00+02:00")


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "t",
                "refresh_token": "r",
                "expires_at": time.time() + 7200,
                "token_type": "Bearer",
            }
        )
    )
    monkeypatch.setenv("WAHOO_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("WAHOO_FTP", "229")


SESSION_ARGS = {
    "date": "2026-10-18",
    "time": "18:00",
    "name": "Angels",
    "steps": [
        {"name": "Einfahren", "minutes": 10, "percent_ftp": 0.55},
        {"name": "Z2", "minutes": 20, "percent_ftp": 0.6},
    ],
}

PLAN_RESPONSE = {
    "id": 7,
    "user_id": 1,
    "name": "Angels",
    "file": {"url": "https://cdn.example.com/p.json"},
    "workout_type_family_id": 0,
    "external_id": "pbp-2026-10-18-angels",
    "provider_updated_at": "2026-10-17T16:00:00.000Z",
    "created_at": "2026-10-17T16:00:00.000Z",
    "updated_at": "2026-10-17T16:00:00.000Z",
}

WORKOUT_RESPONSE = {
    "id": 42,
    "name": "Angels",
    "starts": "2026-10-18T16:00:00.000Z",
    "minutes": 30,
    "workout_type_id": 12,
    "workout_token": "pbp-2026-10-18-angels",
    "plan_id": 7,
    "created_at": "2026-10-17T16:00:00.000Z",
    "updated_at": "2026-10-17T16:00:00.000Z",
}


@pytest.mark.usefixtures("token_file")
class TestPushSessionTool:
    async def test_first_push_creates_plan_and_workout(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            url=f"{API}/v1/plans?external_id=pbp-2026-10-18-angels",
            json=[],
        )
        httpx_mock.add_response(
            method="POST", url=f"{API}/v1/plans", json=PLAN_RESPONSE
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{API}/v1/workouts?page=1&per_page=50",
            json={"workouts": []},
        )
        httpx_mock.add_response(
            method="POST", url=f"{API}/v1/workouts", json=WORKOUT_RESPONSE
        )

        with patch("src.server.check_schedule_window"):
            result = await call_tool("push_session", SESSION_ARGS)

        text = result[0].text
        assert "Plan 7 created, workout 42 scheduled" in text
        assert "- 10:00 Einfahren: 126 W" in text
        assert "- 20:00 Z2: 137 W" in text

        plan_post, workout_post = (
            parse_qs(httpx_mock.get_requests()[i].content.decode()) for i in (1, 3)
        )
        plan_json = json.loads(
            base64.b64decode(plan_post["plan[file]"][0].split(",", 1)[1])
        )
        assert plan_json["header"]["ftp"] == 229
        assert plan_post["plan[external_id]"] == ["pbp-2026-10-18-angels"]
        assert workout_post["workout[workout_token]"] == ["pbp-2026-10-18-angels"]
        assert workout_post["workout[plan_id]"] == ["7"]
        assert workout_post["workout[minutes]"] == ["30"]
        assert workout_post["workout[starts]"] == ["2026-10-18T16:00:00.000Z"]

    async def test_outside_window_fails_before_any_request(self, httpx_mock):
        result = await call_tool("push_session", SESSION_ARGS | {"date": "2099-01-01"})
        assert "sync window" in result[0].text
        assert httpx_mock.get_requests() == []

    async def test_invalid_step_fails_before_any_request(self, httpx_mock):
        args = SESSION_ARGS | {"steps": [{"minutes": 5, "percent_ftp": 95}]}
        with patch("src.server.check_schedule_window"):
            result = await call_tool("push_session", args)
        assert "fraction of FTP" in result[0].text
        assert httpx_mock.get_requests() == []
