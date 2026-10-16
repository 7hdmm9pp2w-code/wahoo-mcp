"""Tests for scheduling workouts (POST /v1/workouts)."""

import json
import time
from datetime import date, datetime
from unittest.mock import patch
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.models import CreateWorkoutRequest, check_schedule_window
from src.server import WahooAPIClient, WahooConfig, call_tool

BERLIN = ZoneInfo("Europe/Berlin")

CREATED_WORKOUT = {
    "id": 42,
    "name": "Test Z2 30 min",
    "starts": "2026-10-17T16:00:00.000Z",
    "minutes": 30,
    "workout_type_id": 12,
    "workout_token": "pbp-test",
    "plan_id": 7,
    "plan_ids": [7],
    "route_id": None,
    "workout_summary": None,
    "created_at": "2026-10-16T18:00:00.000Z",
    "updated_at": "2026-10-16T18:00:00.000Z",
}


@pytest.fixture(autouse=True)
def token_file(tmp_path, monkeypatch):
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "test_token",
                "refresh_token": "test_refresh_token",
                "expires_at": time.time() + 7200,
                "token_type": "Bearer",
            }
        )
    )
    monkeypatch.setenv("WAHOO_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("WAHOO_TIMEZONE", raising=False)


def _request(**kwargs) -> CreateWorkoutRequest:
    defaults = {
        "name": "Test Z2 30 min",
        "starts": "2026-10-17T18:00:00",
        "minutes": 30,
        "workout_token": "pbp-test",
        "plan_id": 7,
    }
    return CreateWorkoutRequest(**(defaults | kwargs))


class TestCreateWorkoutRequest:
    def test_naive_start_is_berlin_time_sent_as_utc(self):
        form = _request().to_form_data()
        assert form == {
            "workout[name]": "Test Z2 30 min",
            "workout[workout_token]": "pbp-test",
            "workout[workout_type_id]": "12",
            "workout[starts]": "2026-10-17T16:00:00.000Z",
            "workout[minutes]": "30",
            "workout[plan_id]": "7",
        }

    def test_explicit_offset_is_kept(self):
        form = _request(starts="2026-10-17T18:00:00+00:00").to_form_data()
        assert form["workout[starts]"] == "2026-10-17T18:00:00.000Z"

    def test_unknown_workout_type_is_rejected(self):
        with pytest.raises(ValidationError, match="Unknown workout_type_id"):
            _request(workout_type_id=999)

    def test_zero_minutes_is_rejected(self):
        with pytest.raises(ValidationError):
            _request(minutes=0)


class TestScheduleWindow:
    today = date(2026, 10, 16)

    @pytest.mark.parametrize("day", [16, 17, 22])
    def test_inside_window(self, day):
        check_schedule_window(datetime(2026, 10, day, 18, tzinfo=BERLIN), self.today)

    @pytest.mark.parametrize("day", [15, 23])
    def test_outside_window(self, day):
        with pytest.raises(ValueError, match="sync window"):
            check_schedule_window(
                datetime(2026, 10, day, 18, tzinfo=BERLIN), self.today
            )

    def test_window_uses_local_day(self):
        # 23:30 UTC on the 22nd is already the 23rd in Berlin.
        with pytest.raises(ValueError, match="sync window"):
            check_schedule_window(
                datetime(2026, 10, 22, 23, 30, tzinfo=ZoneInfo("UTC")), self.today
            )


class TestCreateWorkout:
    async def test_posts_form_data(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://api.wahooligan.com/v1/workouts",
            json=CREATED_WORKOUT,
            status_code=201,
        )
        async with WahooAPIClient(WahooConfig()) as client:
            workout = await client.create_workout(_request())

        assert workout.id == 42
        assert workout.plan_ids == [7]
        request = httpx_mock.get_requests()[0]
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        form = parse_qs(request.content.decode())
        assert form["workout[plan_id]"] == ["7"]
        assert form["workout[starts]"] == ["2026-10-17T16:00:00.000Z"]

    async def test_retries_once_after_401(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://api.wahooligan.com/v1/workouts", status_code=401
        )
        httpx_mock.add_response(
            method="POST",
            url="https://api.wahooligan.com/v1/workouts",
            json=CREATED_WORKOUT,
            status_code=201,
        )
        async with WahooAPIClient(WahooConfig()) as client:
            with patch.object(client, "_refresh_access_token", return_value=True):
                workout = await client.create_workout(_request())
        assert workout.id == 42
        assert len(httpx_mock.get_requests()) == 2


class TestScheduleWorkoutTool:
    async def test_rejects_date_outside_window(self, httpx_mock):
        result = await call_tool(
            "schedule_workout",
            {"name": "Later", "starts": "2099-01-01T18:00:00", "minutes": 30},
        )
        assert "sync window" in result[0].text
        assert httpx_mock.get_requests() == []

    async def test_override_allows_date_outside_window(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://api.wahooligan.com/v1/workouts",
            json=CREATED_WORKOUT,
            status_code=201,
        )
        result = await call_tool(
            "schedule_workout",
            {
                "name": "Later",
                "starts": "2099-01-01T18:00:00",
                "minutes": 30,
                "allow_outside_window": True,
            },
        )
        assert "Workout scheduled successfully" in result[0].text
        form = parse_qs(httpx_mock.get_requests()[0].content.decode())
        assert form["workout[workout_token]"][0].startswith("wahoo-mcp-")
