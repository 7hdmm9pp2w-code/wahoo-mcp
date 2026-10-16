"""Tests for plan validation, FTP handling and plan header derivation."""

import json
import time

import pytest
from pydantic import ValidationError

from src.models import WorkoutInterval, WorkoutPlan, WorkoutTarget
from src.server import WahooAPIClient, WahooConfig


def _plan(targets, **kwargs) -> WorkoutPlan:
    return WorkoutPlan(
        name="Test",
        intervals=[WorkoutInterval(duration=600, targets=targets)],
        **kwargs,
    )


class TestTargetValidation:
    def test_percent_ftp_is_converted_to_watts(self):
        plan = _plan([WorkoutTarget(target_type="percent_ftp", target_value=0.95)])
        target = plan.to_wahoo_format(ftp=229)["intervals"][0]["targets"][0]
        assert target == {"type": "watts", "low": 218, "high": 218}

    def test_percent_ftp_range(self):
        plan = _plan(
            [WorkoutTarget(target_type="percent_ftp", target_min=0.56, target_max=0.75)]
        )
        target = plan.to_wahoo_format(ftp=229)["intervals"][0]["targets"][0]
        assert target == {"type": "watts", "low": 128, "high": 172}

    def test_percent_ftp_given_as_percent_is_rejected(self):
        with pytest.raises(ValidationError, match="fraction of FTP"):
            WorkoutTarget(target_type="percent_ftp", target_value=95)

    def test_absolute_watts_pass_through_unchanged(self):
        plan = _plan([WorkoutTarget(target_type="watts", target_value=135)])
        target = plan.to_wahoo_format(ftp=229)["intervals"][0]["targets"][0]
        assert target == {"type": "watts", "low": 135, "high": 135}

    def test_target_without_value_is_rejected(self):
        with pytest.raises(ValidationError, match="needs target_value"):
            WorkoutTarget(target_type="watts")

    def test_half_open_range_is_rejected(self):
        with pytest.raises(ValidationError, match="given together"):
            WorkoutTarget(target_type="watts", target_min=100)

    def test_inverted_range_is_rejected(self):
        with pytest.raises(ValidationError, match="must not exceed"):
            WorkoutTarget(target_type="watts", target_min=200, target_max=100)

    def test_zero_duration_is_rejected(self):
        with pytest.raises(ValidationError):
            WorkoutInterval(
                duration=0, targets=[WorkoutTarget(target_type="watts", target_value=1)]
            )

    def test_empty_plan_is_rejected(self):
        with pytest.raises(ValidationError):
            WorkoutPlan(name="Empty", intervals=[])


class TestPlanHeader:
    def test_header_uses_given_ftp(self):
        plan = _plan([WorkoutTarget(target_type="watts", target_value=135)])
        assert plan.to_wahoo_format(ftp=229)["header"]["ftp"] == 229

    def test_header_uses_plan_ftp(self):
        plan = _plan([WorkoutTarget(target_type="watts", target_value=135)], ftp=240)
        assert plan.to_wahoo_format()["header"]["ftp"] == 240

    def test_missing_ftp_raises(self):
        plan = _plan([WorkoutTarget(target_type="watts", target_value=135)])
        with pytest.raises(ValueError, match="FTP is required"):
            plan.to_wahoo_format()

    @pytest.mark.parametrize(
        ("workout_type", "family", "location"),
        [
            ("bike", 0, 0),
            ("bike_outdoor", 0, 1),
            ("run", 1, 1),
            ("run_indoor", 1, 0),
        ],
    )
    def test_header_family_and_location(self, workout_type, family, location):
        plan = _plan(
            [WorkoutTarget(target_type="watts", target_value=135)],
            workout_type=workout_type,
        )
        header = plan.to_wahoo_format(ftp=229)["header"]
        assert header["workout_type_family"] == family
        assert header["workout_type_location"] == location

    def test_unknown_workout_type_is_rejected(self):
        with pytest.raises(ValidationError, match="Unknown workout type"):
            _plan(
                [WorkoutTarget(target_type="watts", target_value=135)],
                workout_type="swim",
            )


def _power_zone(zone_id, family, ftp, updated_at):
    return {
        "id": zone_id,
        "user_id": 1,
        "zone_1": 100,
        "zone_2": 130,
        "zone_3": 160,
        "zone_4": 190,
        "zone_5": 220,
        "zone_6": 260,
        "zone_7": 300,
        "ftp": ftp,
        "zone_count": 7,
        "workout_type_id": 0,
        "workout_type_family_id": family,
        "workout_type_location_id": 0,
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": updated_at,
    }


class TestResolveFtp:
    @pytest.fixture(autouse=True)
    def token_file(self, tmp_path, monkeypatch):
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
        monkeypatch.delenv("WAHOO_FTP", raising=False)

    async def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("WAHOO_FTP", "229")
        async with WahooAPIClient(WahooConfig()) as client:
            assert await client.resolve_ftp() == 229

    async def test_invalid_env_var_raises(self, monkeypatch):
        monkeypatch.setenv("WAHOO_FTP", "abc")
        async with WahooAPIClient(WahooConfig()) as client:
            with pytest.raises(ValueError, match="integer"):
                await client.resolve_ftp()

    async def test_latest_bike_zone_is_used(self, httpx_mock):
        httpx_mock.add_response(
            url="https://api.wahooligan.com/v1/power_zones",
            json={
                "power_zones": [
                    _power_zone(1, 0, 220, "2026-01-01T00:00:00.000Z"),
                    _power_zone(2, 0, 229, "2026-10-01T00:00:00.000Z"),
                    _power_zone(3, 1, 300, "2026-10-10T00:00:00.000Z"),
                ]
            },
        )
        async with WahooAPIClient(WahooConfig()) as client:
            assert await client.resolve_ftp("bike") == 229

    async def test_no_zones_raises(self, httpx_mock):
        httpx_mock.add_response(
            url="https://api.wahooligan.com/v1/power_zones",
            json={"power_zones": []},
        )
        async with WahooAPIClient(WahooConfig()) as client:
            with pytest.raises(ValueError, match="No FTP available"):
                await client.resolve_ftp()
