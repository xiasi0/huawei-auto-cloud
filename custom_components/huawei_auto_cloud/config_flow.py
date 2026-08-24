"""Config flow for a fresh Huawei Auto Cloud installation."""

from __future__ import annotations

from dataclasses import asdict, replace
import logging
import time
import uuid
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .auth import P256KeyPair, extract_credentials, session_key_status
from .const import CONF_ASSET_KEY, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN, FIRMWARE_REFRESH_SECONDS, MIN_SCAN_INTERVAL_SECONDS, scan_interval_seconds
from .huawei_auth import HuaweiAuthError, HuaweiIosAuthClient
from .models import AccountSession, Vehicle, VehicleRoute, firmware_sw_version, vehicle_resource_manifest
from .omp.auth import create_enterprise_sessions, refresh_enterprise_session
from .omp.client import OmpApiError, OmpClient, safe_response_shape
from .omp.contracts import OmpOperation
from .omp.enterprises import endpoint_for_id
from .routing import RouteRegistry
from .specs import vehicle_spec_for
from .storage import IdentityStore, PhoneAssetStore, encrypt_password

_LOGGER = logging.getLogger(__name__)


class HuaweiAutoCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._phone: str | None = None
        self._password: str | None = None
        self._identity: dict[str, Any] | None = None
        self._identity_store: IdentityStore | None = None
        self._huawei_client: HuaweiIosAuthClient | None = None
        self._reauth_entry: Any | None = None
        self._asset_revision: int = 0
        self._existing_asset: dict[str, Any] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = str(user_input.get("phone", "")).strip()
            password = str(user_input.get("password", ""))
            if phone and password:
                self._phone, self._password = phone, password
                if self._reauth_entry is None:
                    self._identity_store = IdentityStore(self.hass)
                    self._identity = await self._identity_store.async_get_or_create(phone)
                    await self.async_set_unique_id(phone)
                    self._abort_if_unique_id_configured()
                try:
                    await self.hass.async_add_executor_job(self._request_sms)
                    return self.async_show_form(step_id="sms", data_schema=vol.Schema({vol.Required("sms_code"): str}), errors={})
                except HuaweiAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Huawei SMS request failed")
                    errors["base"] = "cannot_connect"
                self._clear_login_state(preserve_reauth=self._reauth_entry is not None)
            else:
                errors["base"] = "invalid_auth"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("phone"): str, vol.Required("password"): str}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Reauthenticate an existing entry without creating another account asset."""
        entry_id = self.context.get("entry_id")
        get_entry = getattr(self.hass.config_entries, "async_get_entry", None)
        self._reauth_entry = get_entry(entry_id) if entry_id and get_entry else None
        asset_key = entry_data.get(CONF_ASSET_KEY)
        if self._reauth_entry is None or not isinstance(asset_key, str) or not asset_key:
            return self.async_abort(reason="reauth_failed")
        self._identity_store = IdentityStore(self.hass)
        self._identity = await self._identity_store.async_get_or_create(asset_key)
        store = PhoneAssetStore(self.hass, asset_key)
        self._asset_revision, self._existing_asset = await store.async_load()
        if not self._existing_asset:
            return self.async_abort(reason="reauth_failed")
        return await self.async_step_user()

    async def async_step_sms(self, user_input: dict[str, Any] | None = None):
        if user_input is None:
            return self.async_show_form(step_id="sms", data_schema=vol.Schema({vol.Required("sms_code"): str}), errors={})
        sms_code = str(user_input.get("sms_code", "")).strip()
        if not sms_code:
            return self.async_show_form(step_id="sms", data_schema=vol.Schema({vol.Required("sms_code"): str}), errors={"base": "invalid_auth"})
        try:
            asset_key, payload = await self.hass.async_add_executor_job(self._login, sms_code)
            assert self._identity is not None
            store = PhoneAssetStore(self.hass, asset_key)
            expected_revision = self._asset_revision if self._reauth_entry is not None else 0
            if not await store.async_save_if_revision(expected_revision, expected_revision + 1, payload):
                if self._reauth_entry is not None:
                    self._asset_revision, self._existing_asset = await store.async_load()
                raise RuntimeError("account asset changed during authentication; retry reauth")
            if self._reauth_entry is not None:
                if self._reauth_entry.data.get(CONF_ASSET_KEY) != asset_key:
                    raise ValueError("reauthentication attempted to replace the account asset")
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data_updates={CONF_ASSET_KEY: asset_key},
                    reason="reauth_successful",
                )
            return self.async_create_entry(
                title="Huawei Auto Cloud",
                data={CONF_ASSET_KEY: asset_key},
                options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL_SECONDS},
            )
        except HuaweiAuthError:
            return self.async_show_form(step_id="user", data_schema=_login_schema(), errors={"base": "invalid_auth"})
        except OmpApiError as error:
            _LOGGER.warning(
                "Huawei Auto Cloud OMP login request failed: status=%s response=%s redirect=%s",
                error.status,
                error.response,
                error.response_headers.get("Location"),
            )
            return self.async_show_form(step_id="user", data_schema=_login_schema(), errors={"base": "cannot_connect"})
        except (ValueError, RuntimeError):
            _LOGGER.exception("Huawei Auto Cloud login failed")
            return self.async_show_form(step_id="user", data_schema=_login_schema(), errors={"base": "cannot_connect"})
        except Exception:
            # A config flow must never let a storage or framework exception
            # escape as Home Assistant's opaque "Unknown error occurred" UI.
            # The traceback remains available locally for diagnosis.
            _LOGGER.exception("Huawei Auto Cloud login failed unexpectedly")
            return self.async_show_form(step_id="user", data_schema=_login_schema(), errors={"base": "cannot_connect"})
        finally:
            if self._identity_store is not None and self._identity is not None:
                try:
                    await self._identity_store.async_save(self._identity)
                except Exception:
                    # Do not let identity persistence hide the preceding login
                    # result or turn a user-visible flow into an unknown error.
                    _LOGGER.exception("Huawei Auto Cloud could not persist device identity")
            self._clear_login_state(preserve_reauth=self._reauth_entry is not None)

    def _request_sms(self) -> None:
        if not self._identity or not self._phone:
            raise RuntimeError("login state is incomplete")
        self._huawei_client = HuaweiIosAuthClient(
            device_id=str(self._identity["device_id"]),
            device_model=str(self._identity["device_model"]),
            native_device_model=str(self._identity["native_device_model"]),
        )
        self._huawei_client.request_sms(self._phone)

    def _login(self, sms_code: str) -> tuple[str, dict[str, Any]]:
        if not self._identity or self._phone is None or self._password is None:
            raise RuntimeError("login state is incomplete")
        identity = self._identity
        huawei = self._huawei_client or HuaweiIosAuthClient(device_id=str(identity["device_id"]))
        login = huawei.login_with_sms_password(self._phone, sms_code, self._password)
        service_token = login.get("TGC")
        if not service_token:
            raise ValueError("Huawei login did not return a service token")
        st_auth = huawei.st_auth(service_token)
        huawei_user_id = login.get("userID") or (st_auth.get("userID") if isinstance(st_auth, dict) else None)
        if not huawei_user_id:
            raise ValueError("Huawei login did not return a user identity")
        known_user = identity.get("huawei_user_id")
        if known_user and str(known_user) != str(huawei_user_id):
            raise ValueError("Huawei account does not match this integration identity")
        identity["huawei_user_id"] = str(huawei_user_id)
        pair = P256KeyPair.from_storage(identity) or P256KeyPair.generate()
        huawei.set_asym_public_key(str(huawei_user_id), service_token, pair)
        identity.update(pair.as_storage())

        client = OmpClient()
        auth_code = huawei.silent_token(service_token)
        user_response = client.user_auth(auth_code, device_id=str(identity["omp_device_id"]), device_model=str(identity["device_model"]), native_device_model=str(identity["native_device_model"]))
        if session_key_status(user_response) == "1":
            untrusted = extract_credentials(user_response)
            untrusted_user = untrusted.get("user_info")
            untrusted_user_id = untrusted_user.get("userId") if isinstance(untrusted_user, dict) else None
            untrusted_xid = untrusted.get("xid")
            if isinstance(untrusted_user_id, str) and untrusted_user_id and isinstance(untrusted_xid, str) and untrusted_xid:
                client.force_login(
                    xid=untrusted_xid,
                    device_id=str(identity["omp_device_id"]),
                    user_id=untrusted_user_id,
                    native_device_model=str(identity["native_device_model"]),
                )
                user_response = client.user_auth(auth_code, device_id=str(identity["omp_device_id"]), device_model=str(identity["device_model"]), native_device_model=str(identity["native_device_model"]))
        credentials = extract_credentials(user_response)
        required = (credentials.get("access_token"), credentials.get("refresh_token"), credentials.get("xid"))
        if not all(isinstance(value, str) and value for value in required):
            _LOGGER.warning(
                "Huawei Auto Cloud OMP user_auth returned an incomplete session: "
                "shape=%s has_access_token=%s has_refresh_token=%s has_xid=%s",
                safe_response_shape(user_response),
                isinstance(credentials.get("access_token"), str) and bool(credentials["access_token"]),
                isinstance(credentials.get("refresh_token"), str) and bool(credentials["refresh_token"]),
                isinstance(credentials.get("xid"), str) and bool(credentials["xid"]),
            )
            raise ValueError("OMP login did not return a complete account session")
        user_info = credentials.get("user_info")
        omp_user_id = user_info.get("userId") if isinstance(user_info, dict) else huawei_user_id
        account = AccountSession(
            account_generation=1,
            access_token=str(credentials["access_token"]),
            refresh_token=str(credentials["refresh_token"]),
            xid=str(credentials["xid"]),
            omp_user_id=str(omp_user_id),
            omp_device_id=str(identity["omp_device_id"]),
            ivcs_device_id=str(identity["ivcs_device_id"]),
            native_device_model=str(identity["native_device_model"]),
            session_context="",
        )
        vehicle_auth = client.vehicle_auth(xid=account.xid, device_id=account.omp_device_id, user_id=account.omp_user_id, native_device_model=account.native_device_model)
        sessions = create_enterprise_sessions(account, vehicle_auth)
        sessions = {session_id: refresh_enterprise_session(client, account, session) for session_id, session in sessions.items()}
        registry = RouteRegistry(account, {}, sessions)
        discovered: list[tuple[str, Vehicle, dict[str, str | None] | None]] = []
        raw_vehicle_lists: dict[str, Any] = {}
        raw_vehicle_profiles: dict[str, Any] = {}
        for session_id, session in sessions.items():
            context = registry.discovery_context(session_id, OmpOperation.VEHICLE_LIST)
            response = client.request(context)
            raw_vehicle_lists[session_id] = response
            vehicle_items = response if isinstance(response, list) else response.get("data", []) if isinstance(response, dict) else []
            profiles, raw_profile_response = endpoint_for_id(session.endpoint_id).fetch_profiles(
                client,
                account,
                session,
            )
            raw_vehicle_profiles[session_id] = raw_profile_response
            for item in vehicle_items:
                if not isinstance(item, dict):
                    continue
                vehicle_id = str(item.get("vehicleIdStr") or item.get("vehicleId") or "")
                if profile := profiles.get(vehicle_id):
                    item = {**item, "profile": profile}
                vehicle = Vehicle.from_api(item)
                if vehicle.id:
                    discovered.append((session_id, vehicle, vehicle_resource_manifest(item)))
        existing_route_ids = _existing_route_ids(self._existing_asset)
        routes: dict[str, VehicleRoute] = {}
        runtime_vehicles: dict[str, Vehicle] = {}
        vehicle_assets: dict[str, dict[str, Any]] = {}
        resource_manifests: dict[str, dict[str, str | None]] = {}
        for session_id, vehicle, resource_manifest in discovered:
            spec = vehicle_spec_for(vehicle)
            session = sessions[session_id]
            if spec is None:
                continue
            route_id = existing_route_ids.get((vehicle.id, session.endpoint_id, session.enterprise_code, spec.key), str(uuid.uuid4()))
            route = VehicleRoute(route_id, vehicle.id, session.endpoint_id, session_id, session.enterprise_code, spec.key)
            routes[route.route_id] = route
            runtime_vehicles[route.route_id] = vehicle
            if resource_manifest:
                resource_manifests[vehicle.id] = resource_manifest
        if not routes:
            raise ValueError("no supported vehicles were found")
        account = AccountSession(**{**asdict(account), "session_context": ""})
        route_registry = RouteRegistry(account, routes, sessions)
        firmware_responses = _attach_firmware_versions(client, route_registry, routes, runtime_vehicles)
        for route_id, vehicle in runtime_vehicles.items():
            vehicle_assets[route_id] = {
                "route": asdict(routes[route_id]),
                "normalized": vehicle.as_storage(),
            }
            if route_id in firmware_responses:
                vehicle_assets[route_id]["firmware_response"] = firmware_responses[route_id]
        payload = {
            "account": {
                "phone": str(self._phone),
                "encrypted_password": encrypt_password(str(self._password), str(identity["credential_key"])),
                "huawei": {
                    "login_response": login,
                    "st_auth_response": st_auth,
                    "session": {
                        "tgc": service_token,
                        "jsessionid": huawei.jsessionid,
                        "cookies": huawei.cookies,
                    },
                },
            },
            "omp": {
                "user_auth_response": user_response,
                "vehicle_auth_response": vehicle_auth,
                "vehicle_list_responses": raw_vehicle_lists,
                "vehicle_profile_responses": raw_vehicle_profiles,
                "session": asdict(account),
                "enterprise_sessions": {session_id: asdict(session) for session_id, session in sessions.items()},
                "cookies": client.omp_cookies,
            },
            "vehicles": vehicle_assets,
            "resources": {
                vehicle_id: {"manifest": manifest}
                for vehicle_id, manifest in resource_manifests.items()
            },
            "runtime": {
                "last_data": {},
                "route_errors": {},
                "firmware_next_check_at": {
                    route_id: time.time() + FIRMWARE_REFRESH_SECONDS
                    for route_id in firmware_responses
                },
            },
        }
        asset_key = str(self._phone)
        return asset_key, payload

    def _clear_login_state(self, *, preserve_reauth: bool = False) -> None:
        self._phone = self._password = None
        self._huawei_client = None
        if not preserve_reauth:
            self._identity = self._identity_store = None
            self._reauth_entry = None
            self._asset_revision = 0
            self._existing_asset = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HuaweiAutoCloudOptionsFlow()


def _attach_firmware_versions(
    client: OmpClient,
    registry: RouteRegistry,
    routes: dict[str, VehicleRoute],
    vehicles: dict[str, Vehicle],
) -> dict[str, Any]:
    """Fetch each published vehicle's static software version once at login."""
    responses: dict[str, Any] = {}
    for route_id in routes:
        try:
            response = client.request(registry.request_context(route_id, OmpOperation.FIRMWARE))
        except Exception:
            # Firmware is static enrichment only. Its failure must never undo
            # a successful Huawei/OMP login or vehicle discovery.
            _LOGGER.warning("Huawei Auto Cloud firmware version lookup failed", exc_info=True)
            continue
        responses[route_id] = response
        version = firmware_sw_version(response)
        if version:
            vehicles[route_id] = replace(vehicles[route_id], sw_version=version)
    return responses


def _existing_route_ids(payload: dict[str, Any] | None) -> dict[tuple[str, str, str, str], str]:
    """Reuse a route identity only for the exact same upstream vehicle binding."""
    vehicles = payload.get("vehicles") if isinstance(payload, dict) else None
    if not isinstance(vehicles, dict):
        return {}
    route_ids: dict[tuple[str, str, str, str], str] = {}
    for route_id, item in vehicles.items():
        route = item.get("route") if isinstance(item, dict) else None
        if not isinstance(route, dict):
            continue
        values = tuple(route.get(key) for key in ("vehicle_id", "endpoint_id", "enterprise_code", "spec_id"))
        if all(isinstance(value, str) and value for value in values):
            route_ids[values] = str(route_id)
    return route_ids


class HuaweiAutoCloudOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=vol.Schema({vol.Required(CONF_SCAN_INTERVAL, default=scan_interval_seconds(self.config_entry.options)): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL_SECONDS))}))


def _login_schema() -> vol.Schema:
    return vol.Schema({vol.Required("phone"): str, vol.Required("password"): str})
