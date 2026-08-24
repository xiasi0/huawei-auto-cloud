"""Route-isolated Home Assistant data coordinator."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import timedelta
from functools import partial
import logging
import time
from typing import Any, Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DISCOVERED_VEHICLE_SPEC_ID, FIRMWARE_REFRESH_SECONDS, FIRMWARE_RETRY_SECONDS, DOMAIN, UNROUTABLE_OMP_ENDPOINT_ID, UNROUTABLE_OMP_SESSION_ID, scan_interval_seconds
from .models import AccountSession, EnterpriseSession, Vehicle, VehicleRoute
from .omp.auth import OmpAuthorizationError, create_enterprise_sessions, refresh_account_session, refresh_enterprise_session, refresh_enterprise_sessions
from .omp.client import OmpApiError, OmpClient, OmpCommandError
from .omp.contracts import OmpOperation
from .polling import connection_state, sections_for_route
from .routing import RouteRegistry, RouteUnavailable
from .specs import VehicleControl, VehicleSpec, dynamic_sections, has_energy_report_sensors, vehicle_spec_for
from .storage import PhoneAssetStore, canonical_asset_payload

_LOGGER = logging.getLogger(__name__)
_ENERGY_REPORT_REFRESH_SECONDS = 60 * 60
_UNCHANGED_COMMAND_CODES = frozenset({302, "302"})


class HuaweiAutoCloudCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: PhoneAssetStore, revision: int,
                 payload: Mapping[str, Any]) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(seconds=scan_interval_seconds(entry.options)))
        self.entry = entry
        self.store = store
        self.revision = revision
        self.account, self.sessions, self.routes, self.vehicles = _load_payload(payload)
        self.vehicle_specs = {route_id: vehicle_spec_for(self.vehicles[route_id]) for route_id in self.routes}
        self._asset_payload = canonical_asset_payload(payload)
        runtime = payload.get("runtime")
        self.route_errors = dict(runtime.get("route_errors", {})) if isinstance(runtime, Mapping) and isinstance(runtime.get("route_errors"), dict) else {}
        self._last_data = dict(runtime.get("last_data", {})) if isinstance(runtime, Mapping) and isinstance(runtime.get("last_data"), dict) else {}
        disabled_routes = runtime.get("uncontrollable_route_ids", ()) if isinstance(runtime, Mapping) else ()
        self._uncontrollable_routes = {
            route_id for route_id in disabled_routes if isinstance(route_id, str) and route_id in self.routes
        } if isinstance(disabled_routes, (list, tuple, set)) else set()
        omp = payload.get("omp")
        session_data = omp.get("cookies") if isinstance(omp, Mapping) else None
        self.client = OmpClient()
        if isinstance(session_data, Mapping):
            self.client.set_omp_cookies(session_data)
        self._commit_lock = asyncio.Lock()
        self._account_refresh_lock = asyncio.Lock()
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._energy_reports: dict[str, dict[str, Any]] = {}
        self._energy_refresh_at: dict[str, float] = {}
        firmware_checks = runtime.get("firmware_next_check_at", {}) if isinstance(runtime, Mapping) else {}
        self._firmware_refresh_at = {
            route_id: float(next_check)
            for route_id, next_check in firmware_checks.items()
            if route_id in self.routes and isinstance(next_check, (int, float))
        } if isinstance(firmware_checks, Mapping) else {}
        self._raw_status_snapshots: dict[str, dict[str, Any]] = {}
        # The first update after setup is intentionally presence-only, even if
        # the preceding Home Assistant run last saw the vehicle online.
        self._route_online: dict[str, bool | None] = {route_id: None for route_id in self.routes}

    @property
    def registry(self) -> RouteRegistry:
        return RouteRegistry(self.account, self.routes, self.sessions)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for route_id, route in self.routes.items():
            spec = self.vehicle_specs.get(route_id)
            if spec is None:
                continue
            previous = self._last_data.get(route_id)
            try:
                data = await self._async_poll_route(route_id, spec)
                if data is None:
                    if isinstance(previous, dict) and previous:
                        result[route_id] = previous
                    self.route_errors.pop(route_id, None)
                    continue
                if has_energy_report_sensors(spec) and self._route_online[route_id] is True:
                    report = await self._async_energy_report(route_id)
                    if report is not None:
                        data = {**data, "energyReport": report}
                if self._route_online[route_id] is True:
                    await self._async_refresh_firmware_version(route_id)
                result[route_id] = data
                self._last_data[route_id] = data
                self.route_errors.pop(route_id, None)
            except (OmpApiError, OmpAuthorizationError, RouteUnavailable, ValueError) as error:
                self.route_errors[route_id] = type(error).__name__
                if isinstance(previous, dict) and previous:
                    result[route_id] = previous
        await self._async_commit_runtime_state()
        return result

    @property
    def route_online(self) -> Mapping[str, bool | None]:
        """Latest lightweight connection state for the online binary sensor."""
        return self._route_online

    @property
    def raw_status_snapshots(self) -> Mapping[str, dict[str, Any]]:
        """One in-memory raw dynamic frame per route for the diagnostic entity."""
        return self._raw_status_snapshots

    def is_route_controllable(self, route_id: str) -> bool:
        """A route with a vanished enterprise scope must never send a command."""
        route = self.routes.get(route_id)
        return bool(
            route
            and route.endpoint_id != UNROUTABLE_OMP_ENDPOINT_ID
            and route.spec_id != DISCOVERED_VEHICLE_SPEC_ID
            and route_id not in self._uncontrollable_routes
            and route.session_id in self.sessions
        )

    async def _async_poll_route(self, route_id: str, spec: VehicleSpec) -> dict[str, Any] | None:
        """Poll one route once, except for the intentional offline→online catch-up."""
        online = self._route_online[route_id]
        context, raw = await self._async_route_request(
            route_id,
            OmpOperation.DYNAMIC_INFOS,
            payload=sections_for_route(spec, online),
        )
        if not self.registry.is_current(context):
            return None
        data = raw if isinstance(raw, dict) else {}
        observed_online = connection_state(data)

        if online is not True:
            self._route_online[route_id] = observed_online
            if observed_online is not True:
                return None
            # A presence-only request just proved the vehicle online. Fetch the
            # full frame now so entities do not wait for the next 30-second run.
            context, raw = await self._async_route_request(
                route_id,
                OmpOperation.DYNAMIC_INFOS,
                payload=sections_for_route(spec, True),
            )
            if not self.registry.is_current(context):
                return None
            data = raw if isinstance(raw, dict) else {}
            observed_online = connection_state(data)

        if observed_online is False:
            self._route_online[route_id] = False
            return None
        if observed_online is True:
            self._route_online[route_id] = True
            self._raw_status_snapshots.setdefault(route_id, dict(data))
        return data

    async def _async_energy_report(self, route_id: str) -> dict[str, Any] | None:
        now = time.time()
        if now < self._energy_refresh_at.get(route_id, 0):
            return self._energy_reports.get(route_id)
        try:
            context, report = await self._async_route_request(route_id, OmpOperation.ENERGY_REPORT)
            if self.registry.is_current(context) and isinstance(report, dict):
                self._energy_reports[route_id] = report
        except (OmpApiError, RouteUnavailable, ValueError):
            pass
        self._energy_refresh_at[route_id] = now + _ENERGY_REPORT_REFRESH_SECONDS
        return self._energy_reports.get(route_id)

    async def _async_refresh_firmware_version(self, route_id: str) -> None:
        """Refresh static firmware metadata at most once per online vehicle per day."""
        from .omp.enterprises import endpoint_for_id

        if not endpoint_for_id(self.routes[route_id].endpoint_id).supports(OmpOperation.FIRMWARE):
            return
        now = time.time()
        if now < self._firmware_refresh_at.get(route_id, 0):
            return
        try:
            context, response = await self._async_route_request(route_id, OmpOperation.FIRMWARE)
            if not self.registry.is_current(context):
                return
            from .models import firmware_sw_version

            version = firmware_sw_version(response)
            if version and self.vehicles[route_id].sw_version != version:
                self.vehicles = {**self.vehicles, route_id: replace(self.vehicles[route_id], sw_version=version)}
                await self._async_update_device_software_version(route_id, version)
            self._firmware_refresh_at[route_id] = now + FIRMWARE_REFRESH_SECONDS
        except (OmpApiError, RouteUnavailable, ValueError):
            # A firmware lookup must never make an otherwise healthy vehicle
            # unavailable. Retry sooner than the normal daily cadence.
            self._firmware_refresh_at[route_id] = now + FIRMWARE_RETRY_SECONDS

    async def _async_update_device_software_version(self, route_id: str, version: str) -> None:
        """Reflect a confirmed OTA version in Home Assistant's device registry."""
        from homeassistant.helpers import device_registry as dr

        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, route_id)})
        if device is not None and device.sw_version != version:
            registry.async_update_device(device.id, sw_version=version)

    async def async_control_now_departure_plan(self, route_id: str, *, enabled: bool) -> None:
        self._require_control(route_id, VehicleControl.DEPARTURE_PLAN)
        await self._async_command(route_id, OmpOperation.DEPARTURE_PLAN, {"enabled": str(enabled).lower()})

    async def async_control_sentry_mode(self, route_id: str, *, enabled: bool) -> None:
        self._require_control(route_id, VehicleControl.SENTRY_MODE)
        await self._async_command(route_id, OmpOperation.SENTRY_MODE, {"open": str(enabled).lower()})

    async def async_control_air_conditioner(self, route_id: str, *, enabled: bool, target_temp: int | None = None) -> None:
        self._require_control(route_id, VehicleControl.AIR_CONDITIONER)
        if enabled and target_temp is None:
            raise ValueError("air-conditioner requires a target temperature when enabling")
        query = {"enabled": str(enabled).lower()}
        if target_temp is not None:
            query["targetTemp"] = str(target_temp)
        await self._async_command(route_id, OmpOperation.AIR_CONDITIONER, query)

    async def async_control_air_conditioner_rapid(self, route_id: str, *, enabled: bool, mode: int) -> None:
        self._require_control(route_id, VehicleControl.AIR_CONDITIONER)
        if mode not in {1, 2}:
            raise ValueError("rapid air-conditioner mode must be 1 or 2")
        await self._async_command(route_id, OmpOperation.RAPID_AIR_CONDITIONER, {"enabled": str(enabled).lower(), "mode": str(mode)})

    async def async_control_defrost(self, route_id: str, *, enabled: bool) -> None:
        self._require_control(route_id, VehicleControl.AIR_CONDITIONER)
        await self._async_command(route_id, OmpOperation.DEFROST, {"enabled": str(enabled).lower()})

    def _require_control(self, route_id: str, control: VehicleControl) -> None:
        spec = self.vehicle_specs.get(route_id)
        if spec is None or not spec.supports(control) or not self.is_route_controllable(route_id):
            raise RouteUnavailable(f"route does not support {control.value}")

    async def _async_command(self, route_id: str, operation: OmpOperation, query: Mapping[str, str]) -> None:
        route = self.routes.get(route_id)
        if route is None:
            raise RouteUnavailable("unknown route")
        scope_lock = self._scope_locks.setdefault(route.session_id, asyncio.Lock())
        # Keep the route's authorization and account generation stable from
        # admission through dispatch. A request already sent to the cloud
        # cannot be withdrawn, so this is intentionally a pre-dispatch fence.
        async with scope_lock, self._account_refresh_lock:
            context = self.registry.request_context(route_id, operation)
            status = self.registry.request_context(route_id, OmpOperation.COMMAND_STATUS)
            try:
                await self.hass.async_add_executor_job(
                    partial(self.client.command, context, query=query, command_status_context=status)
                )
            except OmpCommandError as error:
                if error.result_code not in _UNCHANGED_COMMAND_CODES:
                    raise
            if not self.registry.is_current(context):
                raise RouteUnavailable("route changed while the command was in flight")
        await self.async_request_refresh()

    async def _async_route_request(self, route_id: str, operation: OmpOperation, *, payload: dict[str, Any] | None = None) -> tuple[Any, Any]:
        route = self.routes.get(route_id)
        if route is None:
            raise RouteUnavailable("unknown route")
        scope_lock = self._scope_locks.setdefault(route.session_id, asyncio.Lock())
        try:
            async with scope_lock, self._account_refresh_lock:
                context = self.registry.request_context(route_id, operation)
                response = await self.hass.async_add_executor_job(
                    partial(self.client.request, context, payload=payload)
                )
            return context, response
        except OmpApiError as error:
            if not _is_enterprise_token_failure(error):
                raise
            await self.async_refresh_session(route.session_id)
            async with scope_lock, self._account_refresh_lock:
                context = self.registry.request_context(route_id, operation)
                response = await self.hass.async_add_executor_job(
                    partial(self.client.request, context, payload=payload)
                )
            return context, response

    async def async_refresh_session(self, session_id: str) -> None:
        """Single-flight refresh for one authorization scope."""
        lock = self._scope_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            current = self.sessions.get(session_id)
            if current is None:
                raise RouteUnavailable("authorization scope is unavailable")
            account_generation = self.account.account_generation
            try:
                refreshed = await self.hass.async_add_executor_job(refresh_enterprise_session, self.client, self.account, current)
            except (OmpApiError, OmpAuthorizationError):
                await self._async_refresh_account_and_sessions(account_generation)
                refreshed = self.sessions.get(session_id)
                if refreshed is None:
                    raise RouteUnavailable("account refresh did not rebuild this enterprise scope")
            if (
                self.account.account_generation != account_generation
                or self.sessions.get(session_id) is not current
                or current.generation != refreshed.generation - 1
            ):
                # Account refresh already replaced this scope. Discard the old
                # in-flight refresh result instead of overwriting new tokens.
                return
            self.sessions = {**self.sessions, session_id: refreshed}
            await self._async_commit_runtime_state()

    async def _async_refresh_account_and_sessions(self, expected_generation: int) -> None:
        """Refresh shared account credentials once, then replace every scope.

        Callers that observed an older generation simply consume the result of
        the in-flight refresh instead of logging in again.
        """
        async with self._account_refresh_lock:
            if self.account.account_generation != expected_generation:
                return
            try:
                refreshed_account = await self.hass.async_add_executor_job(
                    refresh_account_session,
                    self.client,
                    self.account,
                )
            except OmpApiError as error:
                if error.status not in {401, 403}:
                    raise
                raise ConfigEntryAuthFailed("Huawei Auto Cloud account authentication expired") from error
            except OmpAuthorizationError as error:
                raise ConfigEntryAuthFailed("Huawei Auto Cloud account authentication expired") from error
            try:
                response = await self.hass.async_add_executor_job(
                    partial(
                        self.client.vehicle_auth,
                        xid=refreshed_account.xid,
                        device_id=refreshed_account.omp_device_id,
                        user_id=refreshed_account.omp_user_id,
                        native_device_model=refreshed_account.native_device_model,
                    )
                )
            except OmpApiError as error:
                if error.status in {401, 403}:
                    raise ConfigEntryAuthFailed("Huawei Auto Cloud account authentication expired") from error
                raise
            try:
                discovered = create_enterprise_sessions(refreshed_account, response)
            except OmpAuthorizationError:
                discovered = {}
            refreshed_scopes, _ = await self.hass.async_add_executor_job(
                refresh_enterprise_sessions,
                self.client,
                refreshed_account,
                discovered,
            )
            by_endpoint = {session.endpoint_id: session for session in refreshed_scopes.values()}
            replacement: dict[str, EnterpriseSession] = {}
            for session_id, old_session in self.sessions.items():
                new_session = by_endpoint.get(old_session.endpoint_id)
                if new_session is None:
                    self._uncontrollable_routes.update(
                        route_id for route_id, route in self.routes.items() if route.session_id == session_id
                    )
                    continue
                replacement[session_id] = replace(new_session, session_id=session_id, generation=old_session.generation + 1)
            self.account = refreshed_account
            self.sessions = replacement

    async def _async_commit_runtime_state(self) -> None:
        async with self._commit_lock:
            payload = canonical_asset_payload(self._asset_payload)
            omp = dict(payload["omp"])
            omp["session"] = asdict(self.account)
            omp["enterprise_sessions"] = {session_id: asdict(session) for session_id, session in self.sessions.items()}
            omp["cookies"] = self.client.omp_cookies
            payload["omp"] = omp
            vehicles = dict(payload["vehicles"])
            for route_id, route in self.routes.items():
                vehicle_asset = dict(vehicles[route_id])
                vehicle_asset["route"] = asdict(route)
                vehicle_asset["normalized"] = self.vehicles[route_id].as_storage()
                vehicles[route_id] = vehicle_asset
            payload["vehicles"] = vehicles
            payload["runtime"] = {
                "last_data": self._last_data,
                "route_errors": self.route_errors,
                "firmware_next_check_at": self._firmware_refresh_at,
                "uncontrollable_route_ids": sorted(self._uncontrollable_routes),
            }
            self._asset_payload = payload
            next_revision = self.revision + 1
            if not await self.store.async_save_if_revision(self.revision, next_revision, payload):
                raise RuntimeError("account asset changed concurrently; reload or reauth before retrying")
            self.revision = next_revision


def _load_payload(payload: Mapping[str, Any]) -> tuple[AccountSession, dict[str, EnterpriseSession], dict[str, VehicleRoute], dict[str, Vehicle]]:
    omp_raw = payload.get("omp")
    vehicles_raw = payload.get("vehicles")
    if not isinstance(omp_raw, Mapping) or not isinstance(vehicles_raw, Mapping):
        raise ValueError("account asset is incomplete")
    account_raw = omp_raw.get("session")
    sessions_raw = omp_raw.get("enterprise_sessions")
    if not isinstance(account_raw, Mapping) or not isinstance(sessions_raw, Mapping):
        raise ValueError("account asset is incomplete")
    account = AccountSession(**dict(account_raw))
    sessions = {str(session_id): EnterpriseSession(**dict(value)) for session_id, value in sessions_raw.items() if isinstance(value, Mapping)}
    routes = {
        str(route_id): VehicleRoute(**dict(value["route"]))
        for route_id, value in vehicles_raw.items()
        if isinstance(value, Mapping) and isinstance(value.get("route"), Mapping)
    }
    vehicles = {
        str(route_id): Vehicle.from_api(value["normalized"])
        for route_id, value in vehicles_raw.items()
        if isinstance(value, Mapping) and isinstance(value.get("normalized"), Mapping)
    }
    unroutable_routes = {
        route_id
        for route_id, route in routes.items()
        if route.endpoint_id == UNROUTABLE_OMP_ENDPOINT_ID and route.session_id == UNROUTABLE_OMP_SESSION_ID
    }
    if not routes or set(routes) != set(vehicles) or (not sessions and not unroutable_routes):
        raise ValueError("account asset has inconsistent routes")
    return account, sessions, routes, vehicles




def _is_enterprise_token_failure(error: OmpApiError) -> bool:
    response = error.response
    return error.status in {401, 404} and (
        not isinstance(response, Mapping)
        or str(response.get("code")) in {"100011", "100012", "100015", "100002"}
        or any(response.get(key) in {"Token invalid", "Token already been cancelled", "not login"} for key in ("msg", "message"))
    )
