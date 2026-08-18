from __future__ import annotations

from datetime import timedelta
from functools import partial
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

try:
    from homeassistant.exceptions import ConfigEntryAuthFailed
except ModuleNotFoundError:
    class ConfigEntryAuthFailed(RuntimeError):
        pass

from .api import AitoApiClient, AitoApiError, AitoCommandError
from .auth import P256KeyPair, extract_credentials, extract_vehicle_authorization, extract_vehicle_enterprise_code, session_key_status
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_APIG_AUTHORIZATION,
    CONF_DEVICE_ID,
    CONF_ENCRYPTED_PASSWORD,
    CONF_ENCRYPTED_SESSION_CONTEXT,
    CONF_IVCS_DEVICE_ID,
    CONF_OMP_DEVICE_ID,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    CONF_SERVICE_INFO,
    CONF_SERVICE_LOGIN_STATUS,
    CONF_SERVICE_USER_INFO,
    CONF_SESSION_KEY,
    CONF_SESSION_KEY_EXPIRE_IN,
    CONF_USER_INFO,
    CONF_XID,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_NATIVE_DEVICE_MODEL,
    DOMAIN,
    scan_interval_seconds,
)
from .devices import VehicleSpec, dynamic_sections, has_energy_report_sensors
from .huawei_auth import HuaweiAuthError, HuaweiIosAuthClient
from .models import Vehicle
from .storage import decrypt_password, decrypt_session_context, encrypt_session_context

_LOGGER = logging.getLogger(__name__)

_ENERGY_REPORT_REFRESH_SECONDS = 60 * 60

# The vehicle answers a command that would not change its state with this code.
_COMMAND_STATE_UNCHANGED_CODES = frozenset({302, "302"})


class AitoDataCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AitoApiClient,
        vehicles: list[Vehicle],
        vehicle_specs: dict[str, VehicleSpec],
        assets: dict[str, Any] | None = None,
        asset_store: Any | None = None,
        identity: dict[str, Any] | None = None,
        identity_store: Any | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval_seconds(entry.options)),
        )
        self.entry = entry
        self.client = client
        self.vehicles = vehicles
        self.vehicle_specs = vehicle_specs
        self.assets = assets if assets is not None else {}
        self.asset_store = asset_store
        self.identity = identity if identity is not None else {}
        self.identity_store = identity_store
        self._identity_dirty = False
        self._energy_reports: dict[str, dict[str, Any]] = {}
        self._energy_report_refresh_at: dict[str, float] = {}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for vehicle in self.vehicles:
            spec = self.vehicle_specs.get(vehicle.id)
            if spec is None:
                continue
            raw = await self._async_dynamic_infos(vehicle.id, dynamic_sections(spec))
            data = raw if isinstance(raw, dict) else {}
            # While the vehicle sleeps (connectStatus=0) it stops reporting live
            # data and the API returns placeholder values (cabin temp 20.0C, A/C
            # temp 6553.5C, etc. — plausible-looking but fake). Keep the previous
            # frame so they don't overwrite real values. Do NOT substitute an
            # empty frame when there is no previous data: that would make the
            # departure-plan / sentry / A/C entities unavailable and block the
            # controls, exactly when the user needs to send a wake/command.
            previous = (self.data or {}).get(vehicle.id)
            if _is_vehicle_offline(data) and isinstance(previous, dict) and previous:
                data = previous
            if has_energy_report_sensors(spec):
                report = await self._async_latest_energy_report(vehicle.id)
                if report is not None:
                    self._energy_reports[vehicle.id] = report
                if cached_report := self._energy_reports.get(vehicle.id):
                    data = {**data, "energyReport": cached_report}
            result[vehicle.id] = data
        return result

    async def _async_dynamic_infos(self, vehicle_id: str, sections: dict[str, int]) -> Any:
        return await self._async_apig_request(self.client.dynamic_infos, vehicle_id, sections)

    async def async_control_now_departure_plan(self, vehicle_id: str, *, enabled: bool) -> None:
        await self._async_run_vehicle_command(
            partial(self.client.control_now_departure_plan, vehicle_id, enabled=enabled),
            "now departure plan",
        )

    async def async_control_sentry_mode(self, vehicle_id: str, *, enabled: bool) -> None:
        await self._async_run_vehicle_command(
            partial(self.client.control_sentry_mode, vehicle_id, enabled=enabled),
            "sentry mode",
        )

    async def _async_run_vehicle_command(self, request, description: str) -> None:
        """Run a vehicle command, treating "state unchanged" as success.

        The vehicle rejects a command that would not change anything (turning off
        sentry mode while it is already off) with resultCode 302. That is not a
        failure worth raising at the user, so log it and let the refresh below
        report whatever the vehicle actually thinks its state is.
        """
        try:
            await self._async_apig_request(request, retry_after_refresh=False)
        except AitoCommandError as error:
            if error.result_code not in _COMMAND_STATE_UNCHANGED_CODES:
                raise
            _LOGGER.info(
                "AITO %s command reported resultCode %r (state already applied); refreshing instead",
                description,
                error.result_code,
            )
        await self.async_request_refresh()

    async def async_control_air_conditioner(
        self,
        vehicle_id: str,
        *,
        enabled: bool,
        target_temp: int | None = None,
    ) -> None:
        await self._async_control_command(
            partial(
                self.client.control_air_conditioner,
                vehicle_id,
                enabled=enabled,
                target_temp=target_temp,
            )
        )

    async def async_control_air_conditioner_rapid(
        self,
        vehicle_id: str,
        *,
        enabled: bool,
        mode: int,
    ) -> None:
        await self._async_control_command(
            partial(
                self.client.control_air_conditioner_rapid,
                vehicle_id,
                enabled=enabled,
                mode=mode,
            )
        )

    async def async_control_defrost(self, vehicle_id: str, *, enabled: bool) -> None:
        await self._async_control_command(
            partial(self.client.control_defrost, vehicle_id, enabled=enabled)
        )

    async def _async_control_command(self, request) -> None:
        await self._async_apig_request(request, retry_after_refresh=False)
        await self.async_request_refresh()

    async def _async_latest_energy_report(self, vehicle_id: str) -> dict[str, Any] | None:
        now = time.monotonic()
        if now < self._energy_report_refresh_at.get(vehicle_id, 0):
            return None
        try:
            report = await self._async_apig_request(self.client.latest_energy_report, vehicle_id)
        except AitoApiError:
            _LOGGER.warning("AITO energy report request failed for vehicle %s", vehicle_id, exc_info=True)
            self._energy_report_refresh_at[vehicle_id] = now + _ENERGY_REPORT_REFRESH_SECONDS
            return None
        self._energy_report_refresh_at[vehicle_id] = now + _ENERGY_REPORT_REFRESH_SECONDS
        return report if isinstance(report, dict) else None

    async def _async_apig_request(self, request, *args: Any, retry_after_refresh: bool = True) -> Any:
        try:
            return await self.hass.async_add_executor_job(request, *args)
        except AitoApiError as error:
            if not _is_cancelled_apig_token(error):
                raise
            await self._async_refresh_apig_authorization()
            if not retry_after_refresh:
                raise
            try:
                return await self.hass.async_add_executor_job(request, *args)
            except AitoApiError as retry_error:
                if _is_auth_failure(retry_error) or _is_cancelled_apig_token(retry_error):
                    raise ConfigEntryAuthFailed("AITO APIG authorization refresh failed") from retry_error
                raise

    async def _async_refresh_apig_authorization(self) -> None:
        assets = await self.hass.async_add_executor_job(self._refresh_apig_authorization)
        self._sync_session_context()
        if getattr(self, "_identity_dirty", False) and self.identity_store is not None:
            await self.identity_store.async_save(self.identity)
            self._identity_dirty = False
        if self.asset_store is not None:
            await self.asset_store.async_save(assets)

    def _refresh_apig_authorization(self) -> dict[str, Any]:
        xid = self.assets.get(CONF_XID)
        omp_device_id = self._identity_value(CONF_OMP_DEVICE_ID)
        if not xid or not omp_device_id:
            raise ConfigEntryAuthFailed("missing xid or OMP device identity for AITO vehicle token refresh")
        self._sync_device_ids()

        user_info = self.assets.get(CONF_USER_INFO)
        user_id = user_info.get("userId") if isinstance(user_info, dict) else None

        if self.assets.get(CONF_ACCESS_TOKEN) and self.assets.get(CONF_REFRESH_TOKEN):
            try:
                credentials = self._refresh_user_session(str(xid), str(omp_device_id), str(user_id) if user_id else None)
                xid = credentials.get(CONF_XID) or xid
                user_info = self.assets.get(CONF_USER_INFO)
                user_id = user_info.get("userId") if isinstance(user_info, dict) else user_id
            except ConfigEntryAuthFailed:
                return self._password_relogin()

        try:
            response = self._request_vehicle_refresh(str(xid), str(omp_device_id), str(user_id) if user_id else None)
        except ConfigEntryAuthFailed:
            return self._password_relogin()
        authorization = _vehicle_refresh_authorization(response)
        if not authorization and _needs_user_session_refresh(response):
            try:
                credentials = self._refresh_user_session(str(xid), str(omp_device_id), str(user_id) if user_id else None)
            except ConfigEntryAuthFailed:
                return self._password_relogin()
            xid = credentials.get(CONF_XID) or xid
            user_info = self.assets.get(CONF_USER_INFO)
            user_id = user_info.get("userId") if isinstance(user_info, dict) else user_id
            try:
                response = self._request_vehicle_refresh(str(xid), str(omp_device_id), str(user_id) if user_id else None)
            except ConfigEntryAuthFailed:
                return self._password_relogin()
            authorization = _vehicle_refresh_authorization(response)
        if not authorization:
            raise ConfigEntryAuthFailed("vehicle refresh did not return accessToken")

        self.client.apig_authorization = str(authorization)
        self.assets[CONF_APIG_AUTHORIZATION] = str(authorization)
        return self.assets

    def _request_vehicle_refresh(self, xid: str, device_id: str, user_id: str | None) -> Any:
        try:
            return self.client.vehicle_refresh(
                xid=xid,
                device_id=device_id,
                user_id=user_id,
            )
        except AitoApiError as error:
            if _is_auth_failure(error):
                raise ConfigEntryAuthFailed("AITO vehicle token refresh failed") from error
            raise

    def _refresh_user_session(self, xid: str, device_id: str, user_id: str | None) -> dict[str, Any]:
        access_token = self.assets.get(CONF_ACCESS_TOKEN)
        refresh_token = self.assets.get(CONF_REFRESH_TOKEN)
        if not access_token or not refresh_token:
            raise ConfigEntryAuthFailed("missing user token for AITO session refresh")

        try:
            response = self.client.refresh_user_token(
                str(access_token),
                str(refresh_token),
                device_id=device_id,
                xid=xid,
                user_id=user_id,
            )
        except AitoApiError as error:
            if error.status in {401, 403}:
                raise ConfigEntryAuthFailed("user session refresh failed") from error
            raise
        if session_key_status(response) == "1":
            raise ConfigEntryAuthFailed("user session was kicked during refresh")
        credentials = extract_credentials(response)
        if not credentials.get(CONF_XID):
            raise ConfigEntryAuthFailed("user refresh did not return xid")

        for key in (
            CONF_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN,
            CONF_XID,
            CONF_SESSION_KEY,
            CONF_SESSION_KEY_EXPIRE_IN,
            CONF_USER_INFO,
            CONF_SERVICE_INFO,
            CONF_SERVICE_USER_INFO,
            CONF_SERVICE_LOGIN_STATUS,
        ):
            if key in credentials:
                self.assets[key] = credentials[key]
        return credentials

    def _password_relogin(self) -> dict[str, Any]:
        identity = self.identity if isinstance(getattr(self, "identity", None), dict) else {}
        phone = self.assets.get(CONF_PHONE)
        encrypted_password = self.assets.get(CONF_ENCRYPTED_PASSWORD)
        credential_key = identity.get("credential_key")
        huawei_device_id = self._identity_value(CONF_DEVICE_ID)
        omp_device_id = self._identity_value(CONF_OMP_DEVICE_ID)
        if not all(
            isinstance(value, str) and value
            for value in (phone, encrypted_password, credential_key, huawei_device_id, omp_device_id)
        ):
            raise ConfigEntryAuthFailed("missing saved password context for AITO relogin")
        self._sync_device_ids()

        try:
            password = decrypt_password(str(encrypted_password), str(credential_key))
        except Exception as error:
            raise ConfigEntryAuthFailed("saved AITO password cannot be decrypted") from error
        session_context = self._session_context()
        huawei_client = HuaweiIosAuthClient(
            device_id=str(huawei_device_id),
            device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
            native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
            jsessionid=_session_jsessionid(session_context),
            cookies=_session_huawei_cookies(session_context),
        )
        try:
            login = huawei_client.login_by_password(str(phone), password)
            service_token = login.get("TGC")
            if not service_token:
                raise ConfigEntryAuthFailed("Huawei password relogin did not return TGC")
            st_auth = huawei_client.st_auth(service_token)
            huawei_user_id = login.get("userID") or (st_auth.get("userID") if isinstance(st_auth, dict) else None)
            if not huawei_user_id:
                raise ConfigEntryAuthFailed("Huawei password relogin did not return userID")
            known_user_id = identity.get("huawei_user_id")
            if known_user_id and str(known_user_id) != str(huawei_user_id):
                raise ConfigEntryAuthFailed("Huawei account does not match its saved device identity")
            if identity.get("huawei_user_id") != str(huawei_user_id):
                identity["huawei_user_id"] = str(huawei_user_id)
                self._identity_dirty = True
            key_pair = P256KeyPair.from_storage(identity)
            identity_changed = key_pair is None
            key_pair = key_pair or P256KeyPair.generate()
            huawei_client.set_asym_public_key(huawei_user_id, service_token, key_pair)
            key_storage = key_pair.as_storage()
            if any(identity.get(key) != value for key, value in key_storage.items()):
                identity.update(key_storage)
                self.identity = identity
                identity_changed = True
            if identity_changed:
                self._identity_dirty = True
            auth_code = huawei_client.silent_token(service_token)
        except HuaweiAuthError as error:
            raise ConfigEntryAuthFailed(f"Huawei password relogin failed: {error}") from error

        try:
            user_session = self.client.user_auth(
                auth_code,
                device_id=str(omp_device_id),
                device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
                native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
            )
        except AitoApiError as error:
            if _is_auth_failure(error):
                raise ConfigEntryAuthFailed("AITO relogin user auth failed") from error
            raise
        user_session = self._ensure_trusted_omp_session(auth_code, user_session)
        credentials = extract_credentials(user_session)
        xid = credentials.get(CONF_XID)
        if not xid:
            raise ConfigEntryAuthFailed("AITO relogin user auth did not return xid")
        for key in (
            CONF_ACCESS_TOKEN,
            CONF_REFRESH_TOKEN,
            CONF_XID,
            CONF_SESSION_KEY,
            CONF_SESSION_KEY_EXPIRE_IN,
            CONF_USER_INFO,
            CONF_SERVICE_INFO,
            CONF_SERVICE_USER_INFO,
            CONF_SERVICE_LOGIN_STATUS,
        ):
            if key in credentials:
                self.assets[key] = credentials[key]

        user_info = credentials.get(CONF_USER_INFO)
        user_id = user_info.get("userId") if isinstance(user_info, dict) else None
        try:
            vehicle_session = self.client.vehicle_auth(
                xid=str(xid),
                device_id=str(omp_device_id),
                device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
                native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
                user_id=str(user_id) if user_id else None,
                ec=self.client.enterprise_code,
            )
        except AitoApiError as error:
            if _is_auth_failure(error):
                raise ConfigEntryAuthFailed("AITO relogin vehicle auth failed") from error
            raise
        authorization = extract_vehicle_authorization(vehicle_session)
        if not authorization:
            raise ConfigEntryAuthFailed("AITO relogin vehicle auth did not return accessToken")
        found_ec = extract_vehicle_enterprise_code(vehicle_session)
        if found_ec:
            self.client.enterprise_code = found_ec
        self.client.apig_authorization = str(authorization)
        self.assets[CONF_APIG_AUTHORIZATION] = str(authorization)
        self._sync_session_context(
            service_token=str(service_token),
            jsessionid=huawei_client.jsessionid,
            huawei_cookies=huawei_client.cookies,
        )
        if isinstance(vehicle_session, dict) and isinstance(vehicle_session.get("vehicleTokenInfoList"), list):
            self.assets["vehicle_tokens"] = vehicle_session["vehicleTokenInfoList"]
        return self.assets

    def _identity_value(self, key: str) -> str | None:
        for source in (
            self.identity if isinstance(getattr(self, "identity", None), dict) else {},
            self.assets if isinstance(getattr(self, "assets", None), dict) else {},
            self.entry.data if isinstance(getattr(getattr(self, "entry", None), "data", None), dict) else {},
        ):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _sync_device_ids(self) -> None:
        for key in (CONF_DEVICE_ID, CONF_OMP_DEVICE_ID, CONF_IVCS_DEVICE_ID):
            value = self._identity_value(key)
            if value:
                self.assets[key] = value

    def _session_context(self) -> dict[str, Any]:
        encrypted_context = self.assets.get(CONF_ENCRYPTED_SESSION_CONTEXT)
        credential_key = self.identity.get("credential_key") if isinstance(self.identity, dict) else None
        if not encrypted_context:
            return {}
        if not isinstance(encrypted_context, str) or not isinstance(credential_key, str) or not credential_key:
            raise ConfigEntryAuthFailed("saved AITO session context is invalid")
        try:
            return decrypt_session_context(encrypted_context, credential_key)
        except Exception as error:
            raise ConfigEntryAuthFailed("saved AITO session context cannot be decrypted") from error

    def _sync_session_context(
        self,
        *,
        service_token: str | None = None,
        jsessionid: str | None = None,
        huawei_cookies: dict[str, str] | None = None,
    ) -> None:
        credential_key = self.identity.get("credential_key") if isinstance(self.identity, dict) else None
        if not isinstance(credential_key, str) or not credential_key:
            raise ConfigEntryAuthFailed("missing key for saved AITO session context")
        context = self._session_context()
        if service_token:
            context["tgc"] = service_token
        if jsessionid:
            context["jsessionid"] = jsessionid
        if huawei_cookies:
            context["huawei_cookies"] = huawei_cookies
        if self.client.omp_cookies:
            context["omp_cookies"] = self.client.omp_cookies
        self.assets[CONF_ENCRYPTED_SESSION_CONTEXT] = encrypt_session_context(context, credential_key)

    def _ensure_trusted_omp_session(self, auth_code: str, response: Any) -> Any:
        if session_key_status(response) != "1":
            return response

        credentials = extract_credentials(response)
        user_info = credentials.get(CONF_USER_INFO)
        user_id = user_info.get("userId") if isinstance(user_info, dict) else None
        xid = credentials.get(CONF_XID)
        omp_device_id = self._identity_value(CONF_OMP_DEVICE_ID)
        if not user_id or not xid or not omp_device_id:
            _LOGGER.warning("AITO OMP session is untrusted and cannot be verified during relogin")
            return response

        try:
            self.client.force_login(
                xid=str(xid),
                device_id=str(omp_device_id),
                device_model=str(self.identity.get("device_model") or DEFAULT_DEVICE_MODEL),
                native_device_model=str(self.identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
                user_id=str(user_id),
            )
            refreshed = self.client.user_auth(
                auth_code,
                device_id=str(omp_device_id),
                device_model=str(self.identity.get("device_model") or DEFAULT_DEVICE_MODEL),
                native_device_model=str(self.identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
            )
        except AitoApiError as error:
            _LOGGER.warning("AITO OMP device verification did not complete during relogin: HTTP %s", error.status)
            return response
        if session_key_status(refreshed) == "1":
            _LOGGER.warning("AITO OMP session remains untrusted after device verification")
        return refreshed


def _session_jsessionid(context: dict[str, Any]) -> str | None:
    value = context.get("jsessionid")
    return value if isinstance(value, str) and value else None


def _session_huawei_cookies(context: dict[str, Any]) -> dict[str, str]:
    cookies = context.get("huawei_cookies")
    if not isinstance(cookies, dict):
        return {}
    return {
        str(name): str(value)
        for name, value in cookies.items()
        if isinstance(name, str) and isinstance(value, str) and name and value
    }


def _is_vehicle_offline(data: dict[str, Any]) -> bool:
    """The vehicle is asleep/offline when vehicleStatus.connectStatus is 0.

    While offline it stops reporting live data and the API returns placeholder
    values, so callers should keep the last known data instead.
    """
    vehicle_status = data.get("vehicleStatus") if isinstance(data, dict) else None
    return isinstance(vehicle_status, dict) and vehicle_status.get("connectStatus") == 0


def _is_cancelled_apig_token(error: AitoApiError) -> bool:
    response = error.response
    if error.status in {401, 404} and response in (None, ""):
        return True
    if not isinstance(response, dict):
        return False
    return error.status in {401, 404} and (
        str(response.get("code")) in {"100011", "100012", "100015", "100002"}
        or any(
            response.get(key) in {"Token invalid", "Token already been cancelled", "not login", "not logged in"}
            for key in ("msg", "message", "error", "error_description")
        )
    )


def _is_auth_failure(error: AitoApiError) -> bool:
    return error.status in {401, 403}


def _vehicle_refresh_authorization(response: Any) -> str | None:
    if isinstance(response, dict) and response.get("accessToken"):
        return str(response["accessToken"])
    return extract_vehicle_authorization(response)


def _needs_user_session_refresh(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    return (
        str(response.get("code")) in {"401", "100011"}
        or str(response.get("resultCode")) in {"1000019", "3001002"}
        or any(
            response.get(key) in {"xid is expired", "not login", "not logged in"}
            for key in ("msg", "message", "error", "error_description")
        )
    )
