"""Tests for replacing instead of duplicating plans and workouts, deletes and
rate-limit handling."""

import json
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest

from src.models import (
    CreatePlanRequest,
    CreateWorkoutRequest,
    WorkoutInterval,
    WorkoutPlan,
    WorkoutTarget,
)
from src.server import (
    WahooAPIClient,
    WahooConfig,
    _rate_limit_wait_seconds,
    call_tool,
)

API = "https://api.wahooligan.com"


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
    monkeypatch.setenv("WAHOO_FTP", "229")


def _plan_row(plan_id, external_id="pbp-x", deleted=False):
    return {
        "id": plan_id,
        "user_id": 1,
        "name": "Test",
        "file": {"url": "https://cdn.example.com/plan.json"},
        "workout_type_family_id": 0,
        "external_id": external_id,
        "provider_updated_at": "2026-10-17T15:16:55.000Z",
        "deleted": deleted,
    }


def _plan_response(plan_id):
    return _plan_row(plan_id) | {
        "created_at": "2026-10-17T15:16:56.000Z",
        "updated_at": "2026-10-17T15:16:56.000Z",
    }


def _workout_row(workout_id, token="pbp-x", summary=None):  # noqa: S107
    return {
        "id": workout_id,
        "name": "Test",
        "starts": "2026-10-18T16:00:00.000Z",
        "minutes": 30,
        "workout_type_id": 12,
        "workout_token": token,
        "plan_id": 7,
        "workout_summary": summary,
        "created_at": "2026-10-17T15:17:03.000Z",
        "updated_at": "2026-10-17T15:17:03.000Z",
    }


def _plan_request():
    return CreatePlanRequest(
        plan=WorkoutPlan(
            name="Test",
            intervals=[
                WorkoutInterval(
                    duration=600,
                    targets=[WorkoutTarget(target_type="watts", target_value=135)],
                )
            ],
        ),
        external_id="pbp-x",
        provider_updated_at="2026-10-17T16:00:00Z",
    )


def _workout_request():
    return CreateWorkoutRequest(
        name="Test",
        starts="2026-10-18T18:00:00",
        minutes=30,
        workout_token="pbp-x",
        plan_id=7,
    )


def _form(request: httpx.Request) -> dict:
    return parse_qs(request.content.decode())


class TestPlanUpsert:
    async def test_creates_when_no_plan_exists(self, httpx_mock):
        httpx_mock.add_response(
            method="GET", url=f"{API}/v1/plans?external_id=pbp-x", json=[]
        )
        httpx_mock.add_response(
            method="POST", url=f"{API}/v1/plans", json=_plan_response(7)
        )
        async with WahooAPIClient(WahooConfig()) as client:
            action, plan = await client.upsert_plan(_plan_request())
        assert (action, plan.id) == ("created", 7)
        assert _form(httpx_mock.get_requests()[1])["plan[external_id]"] == ["pbp-x"]

    async def test_replaces_existing_plan_without_external_id(self, httpx_mock):
        httpx_mock.add_response(
            method="GET", url=f"{API}/v1/plans?external_id=pbp-x", json=[_plan_row(7)]
        )
        httpx_mock.add_response(
            method="PUT", url=f"{API}/v1/plans/7", json=_plan_response(7)
        )
        async with WahooAPIClient(WahooConfig()) as client:
            action, plan = await client.upsert_plan(_plan_request())
        assert (action, plan.id) == ("updated", 7)
        form = _form(httpx_mock.get_requests()[1])
        assert "plan[external_id]" not in form
        assert form["plan[file]"][0].startswith("data:application/json;base64,")
        assert form["plan[provider_updated_at]"] == ["2026-10-17T16:00:00Z"]

    async def test_deleted_tombstone_is_ignored(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            url=f"{API}/v1/plans?external_id=pbp-x",
            json=[_plan_row(6, deleted=True)],
        )
        httpx_mock.add_response(
            method="POST", url=f"{API}/v1/plans", json=_plan_response(7)
        )
        async with WahooAPIClient(WahooConfig()) as client:
            action, _ = await client.upsert_plan(_plan_request())
        assert action == "created"

    async def test_other_external_ids_are_ignored(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            url=f"{API}/v1/plans?external_id=pbp-x",
            json=[_plan_row(5, external_id="other")],
        )
        async with WahooAPIClient(WahooConfig()) as client:
            assert await client.find_plan_by_external_id("pbp-x") is None

    async def test_duplicates_raise(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            url=f"{API}/v1/plans?external_id=pbp-x",
            json=[_plan_row(5), _plan_row(6)],
        )
        async with WahooAPIClient(WahooConfig()) as client:
            with pytest.raises(ValueError, match="Several plans"):
                await client.upsert_plan(_plan_request())


WORKOUTS_URL = f"{API}/v1/workouts?page=1&per_page=50"


class TestWorkoutUpsert:
    async def test_creates_when_token_unknown(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            url=WORKOUTS_URL,
            json={"workouts": [_workout_row(1, token="other")]},
        )
        httpx_mock.add_response(
            method="POST", url=f"{API}/v1/workouts", json=_workout_row(42)
        )
        async with WahooAPIClient(WahooConfig()) as client:
            action, workout = await client.upsert_workout(_workout_request())
        assert (action, workout.id) == ("created", 42)

    async def test_updates_workout_with_same_token(self, httpx_mock):
        httpx_mock.add_response(
            method="GET", url=WORKOUTS_URL, json={"workouts": [_workout_row(42)]}
        )
        httpx_mock.add_response(
            method="PUT", url=f"{API}/v1/workouts/42", json=_workout_row(42)
        )
        async with WahooAPIClient(WahooConfig()) as client:
            action, workout = await client.upsert_workout(_workout_request())
        assert (action, workout.id) == ("updated", 42)
        form = _form(httpx_mock.get_requests()[1])
        assert form["workout[starts]"] == ["2026-10-18T16:00:00.000Z"]

    async def test_completed_workout_is_not_overwritten(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            url=WORKOUTS_URL,
            json={"workouts": [_workout_row(42, summary={"id": 1})]},
        )
        async with WahooAPIClient(WahooConfig()) as client:
            with pytest.raises(ValueError, match="already completed"):
                await client.upsert_workout(_workout_request())

    async def test_tool_without_token_does_not_look_up(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url=f"{API}/v1/workouts", json=_workout_row(42)
        )
        with patch("src.server.check_schedule_window"):  # date independent of today
            result = await call_tool(
                "schedule_workout",
                {"name": "Test", "starts": "2026-10-18T18:00:00", "minutes": 30},
            )
        assert "scheduled successfully" in result[0].text
        assert len(httpx_mock.get_requests()) == 1


class TestDeletes:
    async def test_delete_workout(self, httpx_mock):
        httpx_mock.add_response(
            method="GET", url=f"{API}/v1/workouts/42", json=_workout_row(42)
        )
        httpx_mock.add_response(
            method="DELETE", url=f"{API}/v1/workouts/42", status_code=204
        )
        async with WahooAPIClient(WahooConfig()) as client:
            assert await client.delete_workout(42) is True

    async def test_delete_missing_workout_is_not_an_error(self, httpx_mock):
        httpx_mock.add_response(
            method="GET", url=f"{API}/v1/workouts/42", status_code=404
        )
        async with WahooAPIClient(WahooConfig()) as client:
            assert await client.delete_workout(42) is False

    async def test_completed_workout_needs_force(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            url=f"{API}/v1/workouts/42",
            json=_workout_row(42, summary={"id": 1}),
        )
        async with WahooAPIClient(WahooConfig()) as client:
            with pytest.raises(ValueError, match="force"):
                await client.delete_workout(42)

    async def test_delete_plan_404_means_gone(self, httpx_mock):
        httpx_mock.add_response(
            method="DELETE", url=f"{API}/v1/plans/7", status_code=404
        )
        async with WahooAPIClient(WahooConfig()) as client:
            assert await client.delete_plan(7) is False

    async def test_delete_workout_tool_by_token(self, httpx_mock):
        httpx_mock.add_response(
            method="GET", url=WORKOUTS_URL, json={"workouts": [_workout_row(42)]}
        )
        httpx_mock.add_response(
            method="GET", url=f"{API}/v1/workouts/42", json=_workout_row(42)
        )
        httpx_mock.add_response(
            method="DELETE", url=f"{API}/v1/workouts/42", status_code=200
        )
        result = await call_tool("delete_workout", {"workout_token": "pbp-x"})
        assert result[0].text == "Workout 42 deleted."

    async def test_delete_plan_tool_by_external_id(self, httpx_mock):
        httpx_mock.add_response(
            method="GET", url=f"{API}/v1/plans?external_id=pbp-x", json=[_plan_row(7)]
        )
        httpx_mock.add_response(method="DELETE", url=f"{API}/v1/plans/7")
        result = await call_tool("delete_plan", {"external_id": "pbp-x"})
        assert result[0].text.startswith("Plan 7 deleted.")


class TestRateLimit:
    def test_wait_uses_longest_value(self):
        response = httpx.Response(
            429, headers={"Retry-After": "5", "X-RateLimit-Reset": "12, 300"}
        )
        assert _rate_limit_wait_seconds(response) == 300

    def test_epoch_reset_becomes_delay(self):
        response = httpx.Response(
            429, headers={"X-RateLimit-Reset": str(int(time.time()) + 30)}
        )
        assert 25 <= _rate_limit_wait_seconds(response) <= 30

    def test_no_headers(self):
        assert _rate_limit_wait_seconds(httpx.Response(429)) is None

    async def test_short_wait_is_retried(self, httpx_mock):
        httpx_mock.add_response(
            method="DELETE",
            url=f"{API}/v1/plans/7",
            status_code=429,
            headers={"Retry-After": "2"},
        )
        httpx_mock.add_response(method="DELETE", url=f"{API}/v1/plans/7")
        with patch("src.server.asyncio.sleep", new_callable=AsyncMock) as sleep:
            async with WahooAPIClient(WahooConfig()) as client:
                assert await client.delete_plan(7) is True
        sleep.assert_awaited_once_with(2.0)

    async def test_long_wait_raises(self, httpx_mock):
        httpx_mock.add_response(
            method="DELETE",
            url=f"{API}/v1/plans/7",
            status_code=429,
            headers={"Retry-After": "600"},
        )
        async with WahooAPIClient(WahooConfig()) as client:
            with pytest.raises(ValueError, match="rate limit"):
                await client.delete_plan(7)
