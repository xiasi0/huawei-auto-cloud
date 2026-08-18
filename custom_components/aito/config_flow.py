from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

try:
    from homeassistant.helpers import selector
except (ImportError, ModuleNotFoundError):
    selector = None

from .api import AitoApiClient, AitoApiError
from .auth import (
    P256KeyPair,
    extract_credentials,
    extract_vehicle_enterprise_code,
    extract_vehicle_authorization,
    session_key_status,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_APIG_AUTHORIZATION,
    CONF_ASSET_KEY,
    CONF_DEVICE_ID,
    CONF_ENCRYPTED_PASSWORD,
    CONF_ENCRYPTED_SESSION_CONTEXT,
    CONF_IVCS_DEVICE_ID,
    CONF_OMP_DEVICE_ID,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_RAW_STATUS_SNAPSHOT_CREATED,
    CONF_SCAN_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SMS_CODE,
    CONF_SESSION_KEY,
    CONF_SESSION_KEY_EXPIRE_IN,
    CONF_SERVICE_INFO,
    CONF_SERVICE_LOGIN_STATUS,
    CONF_SERVICE_USER_INFO,
    CONF_USER_INFO,
    CONF_VEHICLE_RESOURCES,
    ENTERPRISE_CODES,
    CONF_VEHICLES,
    CONF_XID,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_NATIVE_DEVICE_MODEL,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MIN_SCAN_INTERVAL_SECONDS,
    scan_interval_seconds,
)
from .huawei_auth import HuaweiAuthError, HuaweiIosAuthClient
from .models import Vehicle, firmware_sw_version, vehicle_merge_items, vehicle_resource_manifest
from .resources import AitoResourceError, cache_vehicle_resources, remove_vehicle_resources
from .storage import (
    AitoAssetStore,
    AitoDeviceIdentityStore,
    asset_key_from_login_data,
    encrypt_password,
    encrypt_session_context,
)

_LOGGER = logging.getLogger(__name__)


class AitoLoginRejected(RuntimeError):
    pass


class AitoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._identity: dict[str, Any] | None = None
        self._identity_store: AitoDeviceIdentityStore | None = None
        self._huawei_login_client: HuaweiIosAuthClient | None = None
        self._phone: str | None = None
        self._password: str | None = None
        self._reauth_entry: Any | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if CONF_PHONE in user_input and CONF_PASSWORD in user_input:
                phone = str(user_input[CONF_PHONE]).strip()
                password = str(user_input[CONF_PASSWORD])
                if not phone or not password.strip():
                    errors["base"] = "invalid_auth"
                    self._clear_transient_login_state()
                else:
                    self._identity_store = AitoDeviceIdentityStore(self.hass)
                    self._identity = await self._identity_store.async_get_or_create(phone)
                    self._phone = phone
                    self._password = password
                    try:
                        await self.hass.async_add_executor_job(self._request_sms)
                        return self.async_show_form(
                            step_id="sms",
                            data_schema=vol.Schema({vol.Required(CONF_SMS_CODE): str}),
                            errors={},
                        )
                    except HuaweiAuthError as error:
                        _LOGGER.warning("AITO Huawei SMS request was rejected: %s", error)
                        errors["base"] = "invalid_auth"
                        self._clear_transient_login_state()
                    except Exception:
                        _LOGGER.exception("AITO SMS request failed")
                        errors["base"] = "cannot_connect"
                        self._clear_transient_login_state()
            else:
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PHONE): str,
                    vol.Required(CONF_PASSWORD): _password_selector(),
                }
            ),
            errors=errors,
            description_placeholders={},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        entry_id = getattr(self, "context", {}).get("entry_id")
        config_entries_manager = getattr(self.hass, "config_entries", None)
        get_entry = getattr(config_entries_manager, "async_get_entry", None)
        self._reauth_entry = get_entry(entry_id) if entry_id and get_entry else None
        return await self.async_step_user()

    async def async_step_sms(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            sms_code = str(user_input.get(CONF_SMS_CODE, "")).strip()
            if not sms_code:
                return self.async_show_form(
                    step_id="sms",
                    data_schema=vol.Schema({vol.Required(CONF_SMS_CODE): str}),
                    errors={"base": "invalid_auth"},
                )
            try:
                data = await self.hass.async_add_executor_job(self._login_with_sms_code, sms_code)
                if not _has_stored_vehicles(data):
                    _LOGGER.warning("AITO login did not return any vehicles")
                    errors["base"] = "no_vehicles"
                else:
                    asset_key = asset_key_from_login_data(data)
                    return await self._finish_login(asset_key, data)
            except HuaweiAuthError as error:
                _LOGGER.warning("AITO Huawei SMS login was rejected: %s", error)
                errors["base"] = "invalid_auth"
            except AitoLoginRejected as error:
                _LOGGER.warning("AITO login was rejected: %s", error)
                errors["base"] = "invalid_auth"
            except AitoApiError as error:
                if error.status in {401, 403}:
                    _LOGGER.warning("AITO login was rejected by upstream auth: %s", error)
                    errors["base"] = "invalid_auth"
                else:
                    _LOGGER.exception("AITO SMS login failed")
                    errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("AITO SMS login failed")
                errors["base"] = "cannot_connect"
            finally:
                if self._identity_store is not None and self._identity is not None:
                    await self._identity_store.async_save(self._identity)
                self._clear_transient_login_state()

            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_PHONE): str,
                            vol.Required(CONF_PASSWORD): _password_selector(),
                        }
                    ),
                    errors=errors,
                    description_placeholders={},
                )

        return self.async_show_form(
            step_id="sms",
            data_schema=vol.Schema({vol.Required(CONF_SMS_CODE): str}),
            errors=errors,
        )

    def _request_sms(self) -> None:
        if not self._identity or not self._phone:
            raise RuntimeError("missing Huawei login state")
        self._huawei_login_client = self._huawei_client(self._identity)
        self._huawei_login_client.request_sms(self._phone)

    def _clear_transient_login_state(self) -> None:
        self._identity = None
        self._identity_store = None
        self._phone = None
        self._password = None
        self._huawei_login_client = None

    def _login_with_sms_code(self, sms_code: str) -> dict[str, Any]:
        if not self._identity or not self._phone or self._password is None:
            raise RuntimeError("missing Huawei login state")
        identity = dict(self._identity)
        huawei_client = self._huawei_login_client or self._huawei_client(identity)
        login = huawei_client.login_with_sms_password(self._phone, sms_code, self._password)
        return self._complete_ios_login(identity, huawei_client, login)

    def _huawei_client(self, identity: dict[str, Any]) -> HuaweiIosAuthClient:
        return HuaweiIosAuthClient(
            device_id=str(identity[CONF_DEVICE_ID]),
            device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
            native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
        )

    def _complete_ios_login(
        self,
        identity: dict[str, Any],
        huawei_client: HuaweiIosAuthClient,
        login: dict[str, str],
    ) -> dict[str, Any]:
        service_token = login.get("TGC")
        if not service_token:
            raise ValueError("Huawei login did not return TGC")
        st_auth = huawei_client.st_auth(service_token)
        user_id = login.get("userID") or (st_auth.get("userID") if isinstance(st_auth, dict) else None)
        if not user_id:
            raise ValueError("Huawei login/stAuth did not return userID")
        known_user_id = identity.get("huawei_user_id")
        if known_user_id and str(known_user_id) != str(user_id):
            raise AitoLoginRejected("Huawei account does not match its saved device identity")
        identity["huawei_user_id"] = str(user_id)
        key_pair = P256KeyPair.from_storage(identity) or P256KeyPair.generate()
        huawei_client.set_asym_public_key(user_id, service_token, key_pair)
        identity.update(key_pair.as_storage())
        self._identity = identity
        auth_code = huawei_client.silent_token(service_token)

        omp_device_id = str(identity[CONF_OMP_DEVICE_ID])
        client = AitoApiClient(
            ivcs_device_id=str(identity[CONF_IVCS_DEVICE_ID]),
            apig_verify_ssl=False,
        )
        auth_response = client.user_auth(
            auth_code,
            device_id=omp_device_id,
            device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
            native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
        )
        auth_response = _ensure_trusted_omp_session(client, auth_code, identity, auth_response)
        credentials = extract_credentials(auth_response)
        xid = credentials.get(CONF_XID)
        if not xid:
            raise AitoLoginRejected("user auth did not return xid")
        user_info = credentials.get(CONF_USER_INFO)
        omp_user_id = user_info.get("userId") if isinstance(user_info, dict) else user_id
        vehicle_response = None
        apig_authorization = None
        # HarmonyOS 智行 manages several brands under one Huawei account; each
        # brand issues its own vehicle tokens, so probe enterprise codes in
        # order. The empty string keeps the legacy behaviour (no filter).
        for ec in ENTERPRISE_CODES:
            try:
                vehicle_response = client.vehicle_auth(
                    xid=str(xid),
                    device_id=omp_device_id,
                    device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
                    native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
                    user_id=str(omp_user_id) if omp_user_id else None,
                    ec=ec,
                )
                apig_authorization = extract_vehicle_authorization(vehicle_response, enterprise_code=ec or "SERES")
            except Exception as error:
                # Any failure (HTTP/network/timeout) means this enterprise
                # code is not usable; keep probing the remaining codes.
                _LOGGER.debug("AITO vehicle_auth ec=%r rejected: %s", ec, error)
            if apig_authorization:
                found_ec = extract_vehicle_enterprise_code(vehicle_response) or ec or client.enterprise_code
                if found_ec:
                    client.enterprise_code = found_ec
                _LOGGER.debug("AITO vehicle_auth OK with enterprise=%s", found_ec)
                break
        if not apig_authorization:
            raise ValueError("vehicle authorization not returned")

        # The native app follows vehicle/auth with vehicle/refresh (with the
        # enterprise-code header) to exchange the initial token for a usable
        # one; the initial token is rejected by APIG with 401 Token invalid.
        try:
            refreshed = client.vehicle_refresh(
                xid=str(xid),
                device_id=omp_device_id,
                device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
                native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
                user_id=str(omp_user_id) if omp_user_id else None,
            )
            refreshed_auth = extract_vehicle_authorization(refreshed)
            if refreshed_auth:
                apig_authorization = refreshed_auth
                _LOGGER.debug("AITO vehicle_refresh OK after auth")
                found_ec2 = extract_vehicle_enterprise_code(refreshed)
                if found_ec2:
                    client.enterprise_code = found_ec2
        except AitoApiError as error:
            _LOGGER.warning("AITO vehicle_refresh after auth failed; continuing with initial token: %s", error)

        client.apig_authorization = apig_authorization
        vehicles = client.apig_vehicles()
        vehicle_items = vehicles if isinstance(vehicles, list) else vehicles.get("data", []) if isinstance(vehicles, dict) else []

        profiles: dict[str, dict[str, Any]] = {}
        resource_manifests: dict[str, dict[str, str | None]] = {}
        if self._reauth_entry is None:
            profiles, resource_manifests = self._vehicle_profiles(
                client,
                xid=str(xid),
                device_id=omp_device_id,
                user_id=str(omp_user_id) if omp_user_id else None,
                identity=identity,
                fallback_items=vehicle_items,
            )
            vehicle_ids = {
                str(item.get("vehicleIdStr") or item.get("vehicleId") or "")
                for item in vehicle_items
                if isinstance(item, dict)
            }
            if not vehicle_ids or not vehicle_ids.issubset(profiles):
                raise AitoLoginRejected("vehicle profile lookup did not cover every vehicle")
        stored_vehicles = []
        for item in vehicle_items:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            vehicle_id = str(item.get("vehicleIdStr") or item.get("vehicleId") or "")
            if vehicle_id in profiles:
                item["profile"] = profiles[vehicle_id]
            stored_vehicle = Vehicle.from_api(item).as_storage()
            if not stored_vehicle.get("vehicleIdStr"):
                continue
            self._attach_current_version(client, stored_vehicle)
            stored_vehicles.append(stored_vehicle)

        credential_key = identity.get("credential_key")
        if not isinstance(credential_key, str) or not credential_key:
            raise ValueError("device identity did not include credential key")
        return {
            CONF_DEVICE_ID: str(identity[CONF_DEVICE_ID]),
            CONF_OMP_DEVICE_ID: omp_device_id,
            CONF_IVCS_DEVICE_ID: str(identity[CONF_IVCS_DEVICE_ID]),
            CONF_PHONE: self._phone,
            CONF_ENCRYPTED_PASSWORD: encrypt_password(self._password or "", credential_key),
            CONF_ENCRYPTED_SESSION_CONTEXT: encrypt_session_context(
                {
                    "tgc": service_token,
                    "jsessionid": huawei_client.jsessionid,
                    "huawei_cookies": huawei_client.cookies,
                    "omp_cookies": client.omp_cookies,
                },
                credential_key,
            ),
            CONF_ACCESS_TOKEN: credentials.get(CONF_ACCESS_TOKEN),
            CONF_REFRESH_TOKEN: credentials.get(CONF_REFRESH_TOKEN),
            CONF_XID: credentials.get(CONF_XID),
            CONF_SESSION_KEY: credentials.get(CONF_SESSION_KEY),
            CONF_SESSION_KEY_EXPIRE_IN: credentials.get(CONF_SESSION_KEY_EXPIRE_IN),
            CONF_USER_INFO: credentials.get(CONF_USER_INFO),
            CONF_SERVICE_INFO: credentials.get(CONF_SERVICE_INFO),
            CONF_SERVICE_USER_INFO: credentials.get(CONF_SERVICE_USER_INFO),
            CONF_SERVICE_LOGIN_STATUS: credentials.get(CONF_SERVICE_LOGIN_STATUS),
            CONF_APIG_AUTHORIZATION: apig_authorization,
            "vehicle_tokens": vehicle_response.get("vehicleTokenInfoList") if isinstance(vehicle_response, dict) else [],
            CONF_VEHICLES: stored_vehicles,
            CONF_VEHICLE_RESOURCES: resource_manifests,
        }

    def _vehicle_profiles(
        self,
        client: AitoApiClient,
        *,
        xid: str,
        device_id: str,
        user_id: str | None,
        identity: dict[str, Any],
        fallback_items: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str | None]]]:
        response = client.vehicle_management_list(
            xid=xid,
            device_id=device_id,
            device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
            native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
            user_id=user_id,
        )

        profiles: dict[str, dict[str, Any]] = {}
        resource_manifests: dict[str, dict[str, str | None]] = {}
        for item in vehicle_merge_items(response):
            vehicle = Vehicle.from_api(item)
            if vehicle.id:
                profiles[vehicle.id] = vehicle.profile.as_storage()
                if resource_manifest := vehicle_resource_manifest(item):
                    resource_manifests[vehicle.id] = resource_manifest
        # SERES returns vehicle profiles from OMP vehicle/management/list;
        # CHERY/LUXEED returns an empty vehicleMargeInfoList there, so fall
        # back to the APIG /vcam/v1/accounts/vehicles entries themselves.
        if not profiles and fallback_items:
            _LOGGER.debug("AITO OMP profile list empty; falling back to APIG vehicle items")
            for item in fallback_items:
                vehicle = Vehicle.from_api(item)
                if not vehicle.id:
                    continue
                stored = vehicle.profile.as_storage()
                if not stored.get("enterpriseCode"):
                    stored["enterpriseCode"] = client.enterprise_code
                if not stored.get("modelName") and vehicle.model:
                    stored["modelName"] = vehicle.model
                profiles[vehicle.id] = stored
        if not profiles:
            raise AitoLoginRejected("vehicle profile lookup returned no vehicles")
        return profiles, resource_manifests

    def _attach_current_version(self, client: AitoApiClient, stored_vehicle: dict[str, Any]) -> None:
        try:
            response = client.firmware_current_version(str(stored_vehicle["vehicleIdStr"]))
        except Exception:
            _LOGGER.debug("AITO firmware version lookup failed during login", exc_info=True)
            return
        version = firmware_sw_version(response)
        if version:
            stored_vehicle["swVersion"] = version

    async def _save_and_verify_assets(self, asset_key: str, data: dict[str, Any]) -> None:
        asset_store = AitoAssetStore(self.hass, asset_key)
        await asset_store.async_save(data)
        saved = await asset_store.async_load()
        if not saved or not _has_stored_vehicles(saved) or not saved.get(CONF_APIG_AUTHORIZATION):
            raise RuntimeError("AITO credential asset save verification failed")

    async def _finish_login(self, asset_key: str, data: dict[str, Any]):
        entry_data = {CONF_ASSET_KEY: asset_key, CONF_DEVICE_ID: data[CONF_DEVICE_ID]}
        if self._reauth_entry is None:
            await self.async_set_unique_id(asset_key)
            self._abort_if_unique_id_configured()
            try:
                data[CONF_VEHICLE_RESOURCES] = await self._async_cache_vehicle_resources(asset_key, data)
                await self._save_and_verify_assets(asset_key, data)
            except Exception:
                await AitoAssetStore(self.hass, asset_key).async_remove()
                await self.hass.async_add_executor_job(
                    remove_vehicle_resources,
                    self.hass.config.path(".storage", DOMAIN, "resources"),
                    asset_key,
                )
                raise
        else:
            await self._async_restore_static_data(asset_key, data)
            await self._save_and_verify_assets(asset_key, data)
        if self._reauth_entry is not None:
            old_asset_key = (
                self._reauth_entry.data.get(CONF_ASSET_KEY)
                if isinstance(self._reauth_entry.data, dict)
                else None
            )
            if old_asset_key and old_asset_key != asset_key:
                try:
                    await AitoAssetStore(self.hass, str(old_asset_key)).async_remove()
                    await self.hass.async_add_executor_job(
                        remove_vehicle_resources,
                        self.hass.config.path(".storage", DOMAIN, "resources"),
                        str(old_asset_key),
                    )
                except Exception:
                    _LOGGER.debug("AITO old credential asset cleanup failed during reauth", exc_info=True)
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates=entry_data,
                reason="reauth_successful",
            )

        return self.async_create_entry(
            title="AITO",
            data=entry_data,
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL_SECONDS},
        )

    async def _async_cache_vehicle_resources(self, asset_key: str, data: dict[str, Any]) -> dict[str, dict[str, str | None]]:
        manifests = data.get(CONF_VEHICLE_RESOURCES)
        if not isinstance(manifests, dict):
            return {}
        try:
            return await self.hass.async_add_executor_job(
                cache_vehicle_resources,
                self.hass.config.path(".storage", DOMAIN, "resources"),
                asset_key,
                manifests,
            )
        except AitoResourceError:
            _LOGGER.exception("AITO vehicle resource download failed")
            raise

    async def _async_restore_static_data(self, asset_key: str, data: dict[str, Any]) -> None:
        existing = await AitoAssetStore(self.hass, asset_key).async_load()
        previous_vehicles = existing.get(CONF_VEHICLES) if isinstance(existing, dict) else None
        if isinstance(previous_vehicles, list):
            profiles = {
                str(item.get("vehicleIdStr") or item.get("vehicleId") or ""): item.get("profile")
                for item in previous_vehicles
                if isinstance(item, dict) and isinstance(item.get("profile"), dict)
            }
            for vehicle in data.get(CONF_VEHICLES, []):
                if not isinstance(vehicle, dict):
                    continue
                vehicle_id = str(vehicle.get("vehicleIdStr") or vehicle.get("vehicleId") or "")
                if profile := profiles.get(vehicle_id):
                    vehicle["profile"] = profile
        resources = existing.get(CONF_VEHICLE_RESOURCES) if isinstance(existing, dict) else None
        data[CONF_VEHICLE_RESOURCES] = resources if isinstance(resources, dict) else {}
        if existing.get(CONF_RAW_STATUS_SNAPSHOT_CREATED) is True:
            data[CONF_RAW_STATUS_SNAPSHOT_CREATED] = True

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AitoOptionsFlow()


class AitoOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=scan_interval_seconds(self.config_entry.options),
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL_SECONDS)),
                }
            ),
        )


def _ensure_trusted_omp_session(
    client: AitoApiClient,
    auth_code: str,
    identity: dict[str, Any],
    response: Any,
) -> Any:
    if session_key_status(response) != "1":
        return response

    credentials = extract_credentials(response)
    user_info = credentials.get(CONF_USER_INFO)
    user_id = user_info.get("userId") if isinstance(user_info, dict) else None
    xid = credentials.get(CONF_XID)
    if not user_id or not xid:
        _LOGGER.warning("AITO OMP session is untrusted and cannot be verified during login")
        return response

    try:
        client.force_login(
            xid=str(xid),
            device_id=str(identity[CONF_OMP_DEVICE_ID]),
            device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
            native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
            user_id=str(user_id),
        )
        refreshed = client.user_auth(
            auth_code,
            device_id=str(identity[CONF_OMP_DEVICE_ID]),
            device_model=str(identity.get("device_model") or DEFAULT_DEVICE_MODEL),
            native_device_model=str(identity.get("native_device_model") or DEFAULT_NATIVE_DEVICE_MODEL),
        )
    except AitoApiError:
        _LOGGER.warning("AITO OMP device verification did not complete during login")
        return response
    if session_key_status(refreshed) == "1":
        _LOGGER.warning("AITO OMP session remains untrusted after device verification")
    return refreshed


def _has_stored_vehicles(data: dict[str, Any]) -> bool:
    vehicles = data.get(CONF_VEHICLES)
    return isinstance(vehicles, list) and any(isinstance(vehicle, dict) and vehicle.get("vehicleIdStr") for vehicle in vehicles)


def _password_selector() -> Any:
    if selector is None:
        return str
    return selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
