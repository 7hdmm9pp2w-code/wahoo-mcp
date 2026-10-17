#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pydantic import BaseModel, Field

from .models import (
    WORKOUT_TYPE_HEADERS,
    CreatePlanRequest,
    CreatePlanResponse,
    CreateWorkoutRequest,
    Plan,
    PowerZone,
    Route,
    Workout,
    WorkoutInterval,
    WorkoutPlan,
    WorkoutTarget,
    check_schedule_window,
)
from .session import Session, describe_watts
from .token_store import TokenStore

# Type aliases for cleaner function signatures
ToolResponse = list[TextContent]
Arguments = dict[str, Any]

# Load environment variables
load_dotenv()

# Set up logging
logger = logging.getLogger(__name__)


class WahooConfig(BaseModel):
    base_url: str = Field(
        default="https://api.wahooligan.com", description="Base URL for Wahoo API"
    )


# Longest wait for a 429 before giving up instead of blocking the tool call.
MAX_RATE_LIMIT_WAIT_SECONDS = 60

# Workouts fetched to find one by workout_token (newest start times first).
WORKOUT_LOOKUP_PAGE = 50


def _rate_limit_wait_seconds(response: httpx.Response) -> float | None:
    """Seconds to wait after a 429, from Retry-After or X-RateLimit-Reset.

    Wahoo may send several comma-separated values (one per limit window);
    the longest wait wins. Epoch timestamps are converted to a delay.
    """
    waits = []
    for header in ("Retry-After", "X-RateLimit-Reset"):
        for part in response.headers.get(header, "").split(","):
            try:
                value = float(part.strip())
            except ValueError:
                continue
            if value > EPOCH_THRESHOLD:
                value -= time.time()
            waits.append(max(value, 0.0))
    return max(waits) if waits else None


# Header values above this are epoch timestamps, not delays in seconds.
EPOCH_THRESHOLD = 1_000_000_000


class WahooAPIClient:
    def __init__(self, config: WahooConfig):
        self.config = config
        token_file = os.getenv("WAHOO_TOKEN_FILE")
        if not token_file:
            raise ValueError("WAHOO_TOKEN_FILE environment variable is required")
        self.token_store = TokenStore(token_file)
        self.token_data = self.token_store.get_current()
        if not self.token_data:
            raise ValueError(f"No valid tokens found in {token_file}")
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=self._get_headers(),
        )

    def _get_headers(self) -> dict[str, str]:
        """Get current headers with valid access token"""
        return {
            "Authorization": f"Bearer {self.token_data.access_token}",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> bool:
        """Refresh the access token using refresh token"""
        if not self.token_data or not self.token_data.refresh_token:
            logger.error("No refresh token available")
            return False

        client_id = os.getenv("WAHOO_CLIENT_ID")
        if not client_id:
            logger.error("WAHOO_CLIENT_ID not set, cannot refresh token")
            return False

        try:
            # Prepare refresh token request
            data = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.token_data.refresh_token,
            }

            # Check if we should use client_secret (confidential client) or
            # code_verifier (public client)
            client_secret = os.getenv("WAHOO_CLIENT_SECRET")
            if client_secret:
                # Confidential client: use client_secret
                data["client_secret"] = client_secret
                logger.info(
                    "Using client_secret for token refresh (confidential client)"
                )
            elif self.token_data.code_verifier:
                # Public client: use PKCE code_verifier
                data["code_verifier"] = self.token_data.code_verifier
                logger.info(
                    "Using code_verifier for token refresh (public client with PKCE)"
                )
            else:
                logger.warning(
                    "No client_secret or code_verifier available for token refresh"
                )

            async with httpx.AsyncClient() as refresh_client:
                response = await refresh_client.post(
                    f"{self.config.base_url}/oauth/token",
                    data=data,
                )

                if response.status_code == HTTPStatus.OK:
                    token_response = response.json()
                    self.token_data = self.token_store.update_from_response(
                        token_response
                    )
                    # Update client headers with new token
                    self.client.headers.update(self._get_headers())
                    logger.info("Successfully refreshed access token")
                    return True
                else:
                    logger.error(
                        f"Failed to refresh token: {response.status_code} - "
                        f"{response.text}"
                    )
                    return False

        except Exception as e:
            logger.error(f"Error refreshing token: {e}")
            return False

    async def _ensure_valid_token(self) -> bool:
        """Ensure we have a valid access token, refreshing if necessary"""
        if not self.token_data:
            return False

        if self.token_data.is_expired():
            logger.info("Access token expired, attempting to refresh")
            return await self._refresh_access_token()

        return True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()

    async def list_workouts(
        self,
        page: int = 1,
        per_page: int = 30,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Workout]:
        await self._ensure_valid_token()

        params = {"page": page, "per_page": per_page}
        if start_date:
            params["created_after"] = start_date
        if end_date:
            params["created_before"] = end_date

        response = await self.client.get("/v1/workouts", params=params)

        # Handle 401 by refreshing token and retrying once
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if await self._refresh_access_token():
                response = await self.client.get("/v1/workouts", params=params)
            else:
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status()
        data = response.json()
        workouts_data = data.get("workouts", [])

        # Convert each workout dict to Workout object
        workouts = []
        for workout_dict in workouts_data:
            try:
                workout = Workout(**workout_dict)
                workouts.append(workout)
            except Exception as e:
                logger.warning(
                    f"Failed to parse workout {workout_dict.get('id', 'unknown')}: {e}"
                )
                # Continue with other workouts instead of failing completely
                continue

        return workouts

    async def get_workout(self, workout_id: int) -> Workout:
        await self._ensure_valid_token()

        response = await self.client.get(f"/v1/workouts/{workout_id}")

        # Handle 401 by refreshing token and retrying once
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if await self._refresh_access_token():
                response = await self.client.get(f"/v1/workouts/{workout_id}")
            else:
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status()
        workout_dict = response.json()

        try:
            return Workout(**workout_dict)
        except Exception as e:
            logger.error(f"Failed to parse workout {workout_id}: {e}")
            raise ValueError(f"Invalid workout data received from API: {e}") from e

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict | None = None,
        params: dict | None = None,
        allowed_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        """Send a request, refreshing the token once on 401 and waiting once
        on 429. Form data is sent form-encoded as the Wahoo API expects."""
        await self._ensure_valid_token()

        def send() -> Any:
            headers = {"Authorization": f"Bearer {self.token_data.access_token}"}
            if data is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            return self.client.request(
                method, url, data=data, params=params, headers=headers
            )

        response = await send()
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if not await self._refresh_access_token():
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )
            response = await send()

        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            wait = _rate_limit_wait_seconds(response)
            if wait is None or wait > MAX_RATE_LIMIT_WAIT_SECONDS:
                raise ValueError(
                    "Wahoo rate limit reached"
                    + (f"; retry in {wait:.0f} s" if wait else "")
                    + " (sandbox: 25 requests per 5 min, 100 per hour, 250 per day)"
                )
            logger.info(f"Rate limited, waiting {wait:.0f} s before retrying")
            await asyncio.sleep(wait)
            response = await send()

        if response.status_code not in allowed_statuses:
            response.raise_for_status()
        return response

    async def create_workout(self, workout_request: CreateWorkoutRequest) -> Workout:
        """Schedule a workout, optionally with a plan attached"""
        response = await self._request(
            "POST", "/v1/workouts", data=workout_request.to_form_data()
        )
        try:
            return Workout(**response.json())
        except Exception as e:
            logger.error(f"Failed to parse created workout: {e}")
            raise ValueError(f"Invalid workout data received from API: {e}") from e

    async def update_workout(
        self, workout_id: int, workout_request: CreateWorkoutRequest
    ) -> Workout:
        """Replace name, start, duration and plan of a scheduled workout"""
        response = await self._request(
            "PUT", f"/v1/workouts/{workout_id}", data=workout_request.to_form_data()
        )
        return Workout(**response.json())

    async def find_workout_by_token(self, workout_token: str) -> Workout | None:
        """Find a workout by workout_token among the most recent workouts.

        The API sorts by start time descending, so scheduled (future) workouts
        come first and one page covers them.
        """
        response = await self._request(
            "GET", "/v1/workouts", params={"page": 1, "per_page": WORKOUT_LOOKUP_PAGE}
        )
        matches = [
            Workout(**workout)
            for workout in response.json().get("workouts", [])
            if workout.get("workout_token") == workout_token
        ]
        if len(matches) > 1:
            ids = ", ".join(str(workout.id) for workout in matches)
            raise ValueError(
                f"Several workouts share workout_token '{workout_token}' ({ids}); "
                "delete the duplicates first"
            )
        return matches[0] if matches else None

    async def upsert_workout(
        self, workout_request: CreateWorkoutRequest
    ) -> tuple[str, Workout]:
        """Update the workout with the same workout_token, or create it."""
        existing = await self.find_workout_by_token(workout_request.workout_token)
        if existing is None:
            return "created", await self.create_workout(workout_request)
        if existing.workout_summary:
            raise ValueError(
                f"Workout {existing.id} ('{existing.name}') was already completed "
                "and is not overwritten; use a new workout_token"
            )
        return "updated", await self.update_workout(existing.id, workout_request)

    async def delete_workout(self, workout_id: int, force: bool = False) -> bool:
        """Delete a workout. Returns False if it was already gone.

        Completed workouts (with a summary) are only deleted with force=True.
        """
        response = await self._request(
            "GET",
            f"/v1/workouts/{workout_id}",
            allowed_statuses=(HTTPStatus.NOT_FOUND,),
        )
        if response.status_code == HTTPStatus.NOT_FOUND:
            return False
        if response.json().get("workout_summary") and not force:
            raise ValueError(
                f"Workout {workout_id} was already completed; pass force to delete it"
            )
        response = await self._request(
            "DELETE",
            f"/v1/workouts/{workout_id}",
            allowed_statuses=(HTTPStatus.NOT_FOUND,),
        )
        return response.status_code != HTTPStatus.NOT_FOUND

    async def list_routes(self, external_id: str | None = None) -> list[Route]:
        await self._ensure_valid_token()

        params = {}
        if external_id:
            params["external_id"] = external_id

        response = await self.client.get("/v1/routes", params=params)

        # Handle 401 by refreshing token and retrying once
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if await self._refresh_access_token():
                response = await self.client.get("/v1/routes", params=params)
            else:
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status()
        data = response.json()
        # Handle both dict and list response formats
        if isinstance(data, dict):
            routes_data = data.get("routes", [])
        elif isinstance(data, list):
            routes_data = data
        else:
            routes_data = []

        # Convert each route dict to Route object
        routes = []
        for route_dict in routes_data:
            try:
                route = Route(**route_dict)
                routes.append(route)
            except Exception as e:
                logger.warning(
                    f"Failed to parse route {route_dict.get('id', 'unknown')}: {e}"
                )
                # Continue with other routes instead of failing completely
                continue

        return routes

    async def get_route(self, route_id: int) -> Route:
        await self._ensure_valid_token()

        response = await self.client.get(f"/v1/routes/{route_id}")

        # Handle 401 by refreshing token and retrying once
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if await self._refresh_access_token():
                response = await self.client.get(f"/v1/routes/{route_id}")
            else:
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status()
        route_dict = response.json()

        try:
            return Route(**route_dict)
        except Exception as e:
            logger.error(f"Failed to parse route {route_id}: {e}")
            raise ValueError(f"Invalid route data received from API: {e}") from e

    async def list_plans(self, external_id: str | None = None) -> list[Plan]:
        await self._ensure_valid_token()

        params = {}
        if external_id:
            params["external_id"] = external_id

        response = await self.client.get("/v1/plans", params=params)

        # Handle 401 by refreshing token and retrying once
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if await self._refresh_access_token():
                response = await self.client.get("/v1/plans", params=params)
            else:
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status()
        data = response.json()
        # Handle both dict and list response formats
        if isinstance(data, dict):
            plans_data = data.get("plans", [])
        elif isinstance(data, list):
            plans_data = data
        else:
            plans_data = []

        # Convert each plan dict to Plan object
        plans = []
        for plan_dict in plans_data:
            try:
                plan = Plan(**plan_dict)
                plans.append(plan)
            except Exception as e:
                logger.warning(
                    f"Failed to parse plan {plan_dict.get('id', 'unknown')}: {e}"
                )
                # Continue with other plans instead of failing completely
                continue

        return plans

    async def get_plan(self, plan_id: int) -> Plan:
        await self._ensure_valid_token()

        response = await self.client.get(f"/v1/plans/{plan_id}")

        # Handle 401 by refreshing token and retrying once
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if await self._refresh_access_token():
                response = await self.client.get(f"/v1/plans/{plan_id}")
            else:
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status()
        plan_dict = response.json()

        try:
            return Plan(**plan_dict)
        except Exception as e:
            logger.error(f"Failed to parse plan {plan_id}: {e}")
            raise ValueError(f"Invalid plan data received from API: {e}") from e

    async def resolve_ftp(self, workout_type: str = "bike") -> int:
        """Resolve the FTP: WAHOO_FTP env var, else the user's power zones."""
        env_ftp = os.getenv("WAHOO_FTP")
        if env_ftp:
            try:
                ftp = int(env_ftp)
            except ValueError as e:
                raise ValueError(
                    f"WAHOO_FTP must be an integer, got {env_ftp!r}"
                ) from e
            if ftp <= 0:
                raise ValueError(f"WAHOO_FTP must be positive, got {ftp}")
            return ftp

        family, _ = WORKOUT_TYPE_HEADERS[workout_type]
        zones = [
            zone
            for zone in await self.list_power_zones()
            if zone.workout_type_family_id == family and zone.ftp > 0
        ]
        if not zones:
            raise ValueError(
                "No FTP available: set WAHOO_FTP, pass ftp, or configure power "
                "zones in Wahoo"
            )
        return max(zones, key=lambda zone: zone.updated_at).ftp

    async def _plan_form(
        self, plan_request: CreatePlanRequest, include_external_id: bool
    ) -> dict[str, str]:
        """Form data for POST/PUT /v1/plans with the base64 plan file."""
        plan = plan_request.plan
        ftp = plan.ftp or await self.resolve_ftp(plan.workout_type)
        plan_json = json.dumps(plan.to_wahoo_format(ftp), separators=(",", ":"))
        plan_base64 = base64.b64encode(plan_json.encode("utf-8")).decode("utf-8")

        form_data = {
            "plan[file]": f"data:application/json;base64,{plan_base64}",
            "plan[provider_updated_at]": plan_request.provider_updated_at,
        }
        # PUT does not accept external_id; it identifies the plan on create only
        if include_external_id:
            form_data["plan[external_id]"] = plan_request.external_id
        if plan_request.filename:
            form_data["plan[filename]"] = plan_request.filename
        return form_data

    @staticmethod
    def _parse_plan_response(response: httpx.Response) -> CreatePlanResponse:
        try:
            return CreatePlanResponse(**response.json())
        except Exception as e:
            logger.error(f"Failed to parse plan response: {e}")
            raise ValueError(f"Invalid plan data received from API: {e}") from e

    async def create_plan(self, plan_request: CreatePlanRequest) -> CreatePlanResponse:
        """Create a new plan in the user's library"""
        form_data = await self._plan_form(plan_request, include_external_id=True)
        response = await self._request("POST", "/v1/plans", data=form_data)
        return self._parse_plan_response(response)

    async def update_plan(
        self, plan_id: int, plan_request: CreatePlanRequest
    ) -> CreatePlanResponse:
        """Replace the file of a plan this app created"""
        form_data = await self._plan_form(plan_request, include_external_id=False)
        response = await self._request("PUT", f"/v1/plans/{plan_id}", data=form_data)
        return self._parse_plan_response(response)

    async def find_plan_by_external_id(self, external_id: str) -> Plan | None:
        """Find the live plan with this external_id.

        Deleted plans stay visible in the lookup with deleted=true and are
        skipped.
        """
        live = [
            plan
            for plan in await self.list_plans(external_id=external_id)
            if plan.external_id == external_id and not plan.deleted
        ]
        if len(live) > 1:
            ids = ", ".join(str(plan.id) for plan in live)
            raise ValueError(
                f"Several plans share external_id '{external_id}' ({ids}); "
                "delete the duplicates first"
            )
        return live[0] if live else None

    async def upsert_plan(
        self, plan_request: CreatePlanRequest
    ) -> tuple[str, CreatePlanResponse]:
        """Replace the plan with the same external_id, or create it."""
        existing = await self.find_plan_by_external_id(plan_request.external_id)
        if existing is None:
            return "created", await self.create_plan(plan_request)
        return "updated", await self.update_plan(existing.id, plan_request)

    async def delete_plan(self, plan_id: int) -> bool:
        """Delete a plan. Returns False if it was already gone.

        Workouts linked to it are kept but lose the plan.
        """
        response = await self._request(
            "DELETE", f"/v1/plans/{plan_id}", allowed_statuses=(HTTPStatus.NOT_FOUND,)
        )
        return response.status_code != HTTPStatus.NOT_FOUND

    async def list_power_zones(self) -> list[PowerZone]:
        await self._ensure_valid_token()

        response = await self.client.get("/v1/power_zones")

        # Handle 401 by refreshing token and retrying once
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if await self._refresh_access_token():
                response = await self.client.get("/v1/power_zones")
            else:
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status()
        data = response.json()
        # Handle both dict and list response formats
        if isinstance(data, dict):
            power_zones_data = data.get("power_zones", [])
        elif isinstance(data, list):
            power_zones_data = data
        else:
            power_zones_data = []

        # Convert each power zone dict to PowerZone object
        power_zones = []
        for power_zone_dict in power_zones_data:
            try:
                power_zone = PowerZone(**power_zone_dict)
                power_zones.append(power_zone)
            except Exception as e:
                logger.warning(
                    f"Failed to parse power zone "
                    f"{power_zone_dict.get('id', 'unknown')}: {e}"
                )
                # Continue with other power zones instead of failing completely
                continue

        return power_zones

    async def get_power_zone(self, power_zone_id: int) -> PowerZone:
        await self._ensure_valid_token()

        response = await self.client.get(f"/v1/power_zones/{power_zone_id}")

        # Handle 401 by refreshing token and retrying once
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            logger.info("Got 401 Unauthorized, attempting to refresh token")
            if await self._refresh_access_token():
                response = await self.client.get(f"/v1/power_zones/{power_zone_id}")
            else:
                raise httpx.HTTPStatusError(
                    "Authentication failed and token refresh was unsuccessful",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status()
        power_zone_dict = response.json()

        try:
            return PowerZone(**power_zone_dict)
        except Exception as e:
            logger.error(f"Failed to parse power zone {power_zone_id}: {e}")
            raise ValueError(f"Invalid power zone data received from API: {e}") from e


def load_json_schema(schema: str) -> dict:
    """Load a JSON schema from a file."""
    return json.loads((Path(__file__).parent / "schemas" / schema).read_text())


app = Server("wahoo-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_workouts",
            description="List workouts from Wahoo Cloud API",
            inputSchema=load_json_schema("list_workouts.json"),
        ),
        Tool(
            name="get_workout",
            description="Get detailed information about a specific workout",
            inputSchema=load_json_schema("get_workout.json"),
        ),
        Tool(
            name="list_routes",
            description="List routes from Wahoo Cloud API",
            inputSchema=load_json_schema("list_routes.json"),
        ),
        Tool(
            name="get_route",
            description="Get detailed information about a specific route",
            inputSchema=load_json_schema("get_route.json"),
        ),
        Tool(
            name="list_plans",
            description="List plans from Wahoo Cloud API",
            inputSchema=load_json_schema("list_plans.json"),
        ),
        Tool(
            name="get_plan",
            description="Get detailed information about a specific plan",
            inputSchema=load_json_schema("get_plan.json"),
        ),
        Tool(
            name="create_plan",
            description=(
                "Create a plan in the user's library, or replace the plan with "
                "the same external_id. Linked workouts then get the new version."
            ),
            inputSchema=load_json_schema("create_plan.json"),
        ),
        Tool(
            name="schedule_workout",
            description=(
                "Schedule a workout on the user's Wahoo calendar, optionally "
                "attaching a plan so it appears on ELEMNT and KICKR. The start "
                "must be today through 6 days ahead, otherwise Wahoo never "
                "syncs it to the devices. With a workout_token from an earlier "
                "call, that workout is updated instead of duplicated."
            ),
            inputSchema=load_json_schema("schedule_workout.json"),
        ),
        Tool(
            name="push_session",
            description=(
                "Push one planner session to Wahoo in one step: converts % FTP "
                "to watts, creates or replaces the plan and schedules or updates "
                "the workout. The ID pbp-<date>-<name> makes a repeated push "
                "replace instead of duplicate. Date must be today through 6 "
                "days ahead."
            ),
            inputSchema=load_json_schema("push_session.json"),
        ),
        Tool(
            name="delete_workout",
            description=(
                "Delete a scheduled workout by workout_id or workout_token. "
                "Completed workouts are only deleted with force."
            ),
            inputSchema=load_json_schema("delete_workout.json"),
        ),
        Tool(
            name="delete_plan",
            description="Delete a plan by plan_id or external_id",
            inputSchema=load_json_schema("delete_plan.json"),
        ),
        Tool(
            name="list_power_zones",
            description="List power zones from Wahoo Cloud API",
            inputSchema=load_json_schema("list_power_zones.json"),
        ),
        Tool(
            name="get_power_zone",
            description="Get detailed information about a specific power zone",
            inputSchema=load_json_schema("get_power_zone.json"),
        ),
    ]


async def _handle_list_workouts(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle list_workouts tool request."""
    workouts = await client.list_workouts(
        page=arguments.get("page", 1),
        per_page=arguments.get("per_page", 30),
        start_date=arguments.get("start_date"),
        end_date=arguments.get("end_date"),
    )

    if not workouts:
        return [TextContent(type="text", text="No workouts found.")]

    formatted_workouts = "\n\n".join(workout.format_summary() for workout in workouts)
    result = f"Found {len(workouts)} workouts:\n\n{formatted_workouts}"
    return [TextContent(type="text", text=result)]


async def _handle_get_workout(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle get_workout tool request."""
    workout_id = arguments["workout_id"]
    workout = await client.get_workout(workout_id)
    return [TextContent(type="text", text=workout.format_details())]


async def _handle_list_routes(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle list_routes tool request."""
    routes = await client.list_routes(external_id=arguments.get("external_id"))

    if not routes:
        return [TextContent(type="text", text="No routes found.")]

    formatted_routes = "\n\n".join(route.format_summary() for route in routes)
    result = f"Found {len(routes)} routes:\n\n{formatted_routes}"
    return [TextContent(type="text", text=result)]


async def _handle_get_route(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle get_route tool request."""
    route_id = arguments["route_id"]
    route = await client.get_route(route_id)
    return [TextContent(type="text", text=route.format_details())]


async def _handle_list_plans(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle list_plans tool request."""
    plans = await client.list_plans(external_id=arguments.get("external_id"))

    if not plans:
        return [TextContent(type="text", text="No plans found.")]

    formatted_plans = "\n\n".join(plan.format_summary() for plan in plans)
    result = f"Found {len(plans)} plans:\n\n{formatted_plans}"
    return [TextContent(type="text", text=result)]


async def _handle_get_plan(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle get_plan tool request."""
    plan_id = arguments["plan_id"]
    plan = await client.get_plan(plan_id)
    return [TextContent(type="text", text=plan.format_details())]


def _build_workout_intervals(intervals_data: list) -> list[WorkoutInterval]:
    """Build list of WorkoutInterval objects from raw data."""
    intervals = []
    for interval_data in intervals_data:
        targets = [
            WorkoutTarget(**target_data)
            for target_data in interval_data.get("targets", [])
        ]
        nested = interval_data.get("intervals")

        interval = WorkoutInterval(
            duration=interval_data.get("duration"),
            targets=targets,
            name=interval_data.get("name"),
            interval_type=interval_data.get(
                "interval_type", "active" if nested else "work"
            ),
            repeat=interval_data.get("repeat"),
            intervals=_build_workout_intervals(nested) if nested else None,
        )
        intervals.append(interval)
    return intervals


def _format_plan_result(created_plan, action: str = "created") -> str:
    """Format the plan creation result message."""
    verb = "replaced" if action == "updated" else "created"
    result = f"""Plan {verb} successfully!

Plan Details:
- ID: {created_plan.id}
- Name: {created_plan.name}
- External ID: {created_plan.external_id}
- User ID: {created_plan.user_id}
- Created: {created_plan.created_at}
- Updated: {created_plan.updated_at}
- File URL: {created_plan.file.url}"""

    if created_plan.description:
        result += f"\n- Description: {created_plan.description}"

    return result


async def _handle_create_plan(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle create_plan tool request."""
    plan_data = arguments["plan"]

    # Build intervals from data
    intervals = _build_workout_intervals(plan_data["intervals"])

    # Create WorkoutPlan with only essential fields to avoid API validation errors
    plan = WorkoutPlan(
        name=plan_data["name"],
        description=plan_data.get("description"),
        intervals=intervals,
        workout_type=plan_data.get("workout_type", "bike"),
        ftp=plan_data.get("ftp"),
        # Skip optional fields that cause Wahoo API validation errors:
        # - estimated_duration: calculated automatically from intervals
        # - estimated_tss: not accepted by API
        # - author: not accepted by API
    )

    plan_request = CreatePlanRequest(
        plan=plan,
        filename=arguments.get("filename"),
        external_id=arguments["external_id"],
        provider_updated_at=arguments["provider_updated_at"],
    )
    action, created_plan = await client.upsert_plan(plan_request)

    result = _format_plan_result(created_plan, action)
    return [TextContent(type="text", text=result)]


async def _handle_schedule_workout(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle schedule_workout tool request."""
    workout_token = arguments.get("workout_token")
    workout_request = CreateWorkoutRequest(
        name=arguments["name"],
        starts=arguments["starts"],
        minutes=arguments["minutes"],
        workout_token=workout_token or f"wahoo-mcp-{uuid.uuid4()}",
        workout_type_id=arguments.get("workout_type_id", 12),
        plan_id=arguments.get("plan_id"),
        route_id=arguments.get("route_id"),
    )
    if not arguments.get("allow_outside_window", False):
        check_schedule_window(workout_request.starts)

    # Only a caller-chosen token can match an earlier push
    if workout_token:
        action, workout = await client.upsert_workout(workout_request)
    else:
        action, workout = "created", await client.create_workout(workout_request)

    verb = "updated" if action == "updated" else "scheduled"
    return [
        TextContent(
            type="text",
            text=f"Workout {verb} successfully!\n\n{workout.format_summary()}",
        )
    ]


async def _handle_push_session(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle push_session: build the plan, replace or create it, schedule it."""
    arguments = dict(arguments)
    allow_outside_window = arguments.pop("allow_outside_window", False)
    session = Session(**arguments)
    starts = session.starts()
    # Check everything that can fail locally before the first API request
    if not allow_outside_window:
        check_schedule_window(starts)
    plan = session.to_plan()
    sid = session.id()

    ftp = plan.ftp or await client.resolve_ftp(plan.workout_type)
    plan = plan.model_copy(update={"ftp": ftp})
    plan_json = plan.to_wahoo_format(ftp)

    plan_action, created_plan = await client.upsert_plan(
        CreatePlanRequest(
            plan=plan,
            filename=f"{sid}.json",
            external_id=sid,
            provider_updated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    workout_action, workout = await client.upsert_workout(
        CreateWorkoutRequest(
            name=session.name,
            starts=starts,
            minutes=session.minutes(plan),
            workout_token=sid,
            workout_type_id=session.workout_type_id,
            plan_id=created_plan.id,
        )
    )

    plan_verb = "replaced" if plan_action == "updated" else "created"
    workout_verb = "updated" if workout_action == "updated" else "scheduled"
    text = f"""Session pushed: {session.name}
- Start: {starts:%Y-%m-%d %H:%M} ({starts.tzinfo}), {session.minutes(plan)} min
- ID: {sid}
- Plan {created_plan.id} {plan_verb}, workout {workout.id} {workout_verb}
- FTP: {ftp} W

Steps sent to Wahoo:
{describe_watts(plan_json)}"""
    return [TextContent(type="text", text=text)]


async def _handle_delete_workout(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle delete_workout tool request."""
    workout_id = arguments.get("workout_id")
    if workout_id is None:
        token = arguments.get("workout_token")
        if not token:
            raise ValueError("Pass workout_id or workout_token")
        workout = await client.find_workout_by_token(token)
        if workout is None:
            return [TextContent(type="text", text=f"No workout with token '{token}'.")]
        workout_id = workout.id

    if not await client.delete_workout(workout_id, force=arguments.get("force", False)):
        return [
            TextContent(type="text", text=f"Workout {workout_id} was already gone.")
        ]
    return [TextContent(type="text", text=f"Workout {workout_id} deleted.")]


async def _handle_delete_plan(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle delete_plan tool request."""
    plan_id = arguments.get("plan_id")
    if plan_id is None:
        external_id = arguments.get("external_id")
        if not external_id:
            raise ValueError("Pass plan_id or external_id")
        plan = await client.find_plan_by_external_id(external_id)
        if plan is None:
            return [
                TextContent(
                    type="text", text=f"No plan with external_id '{external_id}'."
                )
            ]
        plan_id = plan.id

    if not await client.delete_plan(plan_id):
        return [TextContent(type="text", text=f"Plan {plan_id} was already gone.")]
    return [
        TextContent(
            type="text",
            text=(
                f"Plan {plan_id} deleted. Workouts that used it are kept without plan. "
                "Wahoo keeps the external_id reserved for the deleted plan, so "
                "re-create it under a new external_id."
            ),
        )
    ]


async def _handle_list_power_zones(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle list_power_zones tool request."""
    power_zones = await client.list_power_zones()

    if not power_zones:
        return [TextContent(type="text", text="No power zones found.")]

    formatted_zones = "\n\n".join(pz.format_summary() for pz in power_zones)
    result = f"Found {len(power_zones)} power zones:\n\n{formatted_zones}"
    return [TextContent(type="text", text=result)]


async def _handle_get_power_zone(
    client: WahooAPIClient, arguments: Arguments
) -> ToolResponse:
    """Handle get_power_zone tool request."""
    power_zone_id = arguments["power_zone_id"]
    power_zone = await client.get_power_zone(power_zone_id)
    return [TextContent(type="text", text=power_zone.format_details())]


# Tool handler mapping
TOOL_HANDLERS = {
    "list_workouts": _handle_list_workouts,
    "get_workout": _handle_get_workout,
    "list_routes": _handle_list_routes,
    "get_route": _handle_get_route,
    "list_plans": _handle_list_plans,
    "get_plan": _handle_get_plan,
    "create_plan": _handle_create_plan,
    "schedule_workout": _handle_schedule_workout,
    "push_session": _handle_push_session,
    "delete_workout": _handle_delete_workout,
    "delete_plan": _handle_delete_plan,
    "list_power_zones": _handle_list_power_zones,
    "get_power_zone": _handle_get_power_zone,
}


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Main tool dispatcher."""
    config = WahooConfig()

    # Check if tool exists
    if name not in TOOL_HANDLERS:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    try:
        async with WahooAPIClient(config) as client:
            handler = TOOL_HANDLERS[name]
            return await handler(client, arguments)
    except httpx.HTTPStatusError as e:
        return [
            TextContent(
                type="text",
                text=f"HTTP Error {e.response.status_code}: {e.response.text}",
            )
        ]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
