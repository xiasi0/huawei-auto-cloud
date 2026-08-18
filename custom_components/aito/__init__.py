from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from .const import (
    CONF_DEVICE_ID,
    CONF_APIG_AUTHORIZATION,
    CONF_ENCRYPTED_PASSWORD,
    CONF_ENCRYPTED_SESSION_CONTEXT,
    CONF_IVCS_DEVICE_ID,
    CONF_OMP_DEVICE_ID,
    CONF_PHONE,
    CONF_RAW_STATUS_SNAPSHOT_CREATED,
)
from .models import Vehicle, vehicle_device_info
from .storage import decrypt_password, decrypt_session_context

try:
    from homeassistant.exceptions import ConfigEntryAuthFailed
except ModuleNotFoundError:
    class ConfigEntryAuthFailed(RuntimeError):
        pass

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _restore_enterprise_code(client: AitoApiClient, vehicles: list[Vehicle]) -> None:
    """Set the client's enterprise code to the vehicle's (OMP headers and APIG gateway).

    SERES is the default; brands like CHERY/LUXEED must use their own gateway
    (see APIG_BASE_URLS) or the API rejects the token with 401. For accounts
    with vehicles from multiple enterprises the first vehicle wins.
    """
    restored_ec = next(
        (v.profile.enterprise_code for v in vehicles if v.profile.enterprise_code), None
    )
    if restored_ec:
        client.enterprise_code = restored_ec
        _LOGGER.debug("AITO restored enterprise_code=%s (gateway=%s)", restored_ec, client.apig_base_url)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .const import (
        CONF_APIG_AUTHORIZATION,
        CONF_ASSET_KEY,
        PLATFORMS,
        CONF_VEHICLES,
        DOMAIN,
    )
    from .api import AitoApiClient
    from .coordinator import AitoDataCoordinator
    from .devices import vehicle_spec_for
    from .resources import remove_vehicle_resources
    from .storage import AitoAssetStore, AitoDeviceIdentityStore, asset_key_from_login_data

    asset_key = entry.data.get(CONF_ASSET_KEY)
    asset_store = AitoAssetStore(hass, asset_key) if asset_key else None
    assets = await asset_store.async_load() if asset_store else entry.data
    if asset_store and not assets:
        _raise_setup_auth_failed("AITO credential asset is missing")
    phone = assets.get(CONF_PHONE) if isinstance(assets, dict) else None
    if not isinstance(phone, str) or not phone:
        _raise_setup_auth_failed("AITO credential asset is missing its account identity")
    try:
        identity_store = AitoDeviceIdentityStore(hass)
        identity = await identity_store.async_get_or_create(phone)
    except ModuleNotFoundError:
        identity_store = None
        identity = {}
    assets_dirty = False
    for key in (CONF_DEVICE_ID, CONF_OMP_DEVICE_ID, CONF_IVCS_DEVICE_ID):
        identity_value = _identity_value(identity, key)
        if identity_value and assets.get(key) != identity_value:
            assets[key] = identity_value
            assets_dirty = True
    if asset_store and assets:
        target_asset_key = asset_key_from_login_data(assets)
        if target_asset_key != asset_key:
            old_asset_store = asset_store
            asset_store = AitoAssetStore(hass, target_asset_key)
            await asset_store.async_save(assets)
            assets_dirty = False
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_ASSET_KEY: target_asset_key},
            )
            await old_asset_store.async_remove()
            await hass.async_add_executor_job(
                remove_vehicle_resources,
                hass.config.path(".storage", DOMAIN, "resources"),
                str(asset_key),
            )
    if not assets.get(CONF_APIG_AUTHORIZATION):
        _raise_setup_auth_failed("AITO credential asset is incomplete")
    _validate_saved_login_context(
        assets,
        identity,
        require_identity=identity_store is not None,
    )

    vehicle_items = [item for item in assets.get(CONF_VEHICLES, []) if isinstance(item, dict)]
    if not any(Vehicle.from_api(item).id for item in vehicle_items):
        _raise_setup_auth_failed("AITO vehicle list is empty")

    vehicles = [vehicle for item in vehicle_items for vehicle in (Vehicle.from_api(item),) if vehicle.id]
    vehicle_specs = {
        vehicle.id: spec
        for vehicle in vehicles
        for spec in (vehicle_spec_for(vehicle),)
        if spec is not None
    }
    raw_status_snapshots: dict[str, dict[str, Any]] = {}
    if not assets.get(CONF_RAW_STATUS_SNAPSHOT_CREATED):
        raw_status_snapshots = await _async_capture_raw_status_snapshots(hass, assets, identity, vehicles)
        if raw_status_snapshots:
            assets[CONF_RAW_STATUS_SNAPSHOT_CREATED] = True
            assets_dirty = True
    if assets_dirty and asset_store is not None:
        await asset_store.async_save(assets)
    client = AitoApiClient(
        apig_authorization=str(assets[CONF_APIG_AUTHORIZATION]),
        ivcs_device_id=_identity_value(identity, CONF_IVCS_DEVICE_ID) or assets.get(CONF_IVCS_DEVICE_ID),
        omp_cookies=_saved_session_context(assets, identity).get("omp_cookies"),
        apig_verify_ssl=False,
    )
    _restore_enterprise_code(client, vehicles)
    coordinator = None
    if vehicle_specs:
        coordinator = AitoDataCoordinator(
            hass,
            entry,
            client,
            vehicles,
            vehicle_specs,
            assets=assets,
            asset_store=asset_store,
            identity=identity,
            identity_store=identity_store,
        )
        initial_data = {
            vehicle_id: snapshot
            for vehicle_id, snapshot in raw_status_snapshots.items()
            if vehicle_id in vehicle_specs
        }
        if initial_data:
            coordinator.async_set_updated_data(initial_data)
        else:
            await coordinator.async_config_entry_first_refresh()
    _remove_legacy_entities(hass, entry, vehicles, vehicle_specs)
    _register_vehicle_devices(hass, entry, vehicles)
    await _async_extract_car_images(hass, assets, vehicles)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "assets": assets,
        "identity": identity,
        "coordinator": coordinator,
        "raw_status_sensor_loaded": bool(assets.get(CONF_RAW_STATUS_SNAPSHOT_CREATED) or vehicle_specs),
        "raw_status_snapshots": raw_status_snapshots,
        "vehicles": vehicles,
        "vehicle_specs": vehicle_specs,
    }
    if assets.get(CONF_RAW_STATUS_SNAPSHOT_CREATED) or vehicle_specs:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .const import DOMAIN, PLATFORMS

    loaded = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("raw_status_sensor_loaded", False)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS) if loaded else True
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    from .const import CONF_ASSET_KEY, DOMAIN
    from .resources import remove_vehicle_resources
    from .storage import AitoAssetStore

    asset_key = entry.data.get(CONF_ASSET_KEY)
    if asset_key:
        await AitoAssetStore(hass, asset_key).async_remove()
        await hass.async_add_executor_job(
            remove_vehicle_resources,
            hass.config.path(".storage", DOMAIN, "resources"),
            str(asset_key),
        )


def _raise_setup_auth_failed(message: str) -> None:
    _LOGGER.warning("%s; reconfigure the integration", message)
    raise ConfigEntryAuthFailed(message)


def _register_vehicle_devices(hass: HomeAssistant, entry: ConfigEntry, vehicles: list[Vehicle]) -> None:
    """Create device-registry records without creating any HA entities."""
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    for vehicle in vehicles:
        info = vehicle_device_info(vehicle)
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers=info["identifiers"],
            name=info.get("name"),
            manufacturer=info.get("manufacturer"),
            model=info.get("model"),
            sw_version=info.get("sw_version"),
        )


async def _async_extract_car_images(hass: HomeAssistant, assets: Any, vehicles: list[Vehicle]) -> None:
    """Extract each vehicle's body render from its resource archive into /www.

    Writes www/aito/<vehicle_id>.png plus a stable www/aito/car.png alias for
    the first vehicle (served at /local/aito/...), so the Lovelace card can show
    the official picture without a bundled asset.
    """
    import os
    import shutil

    from .const import DOMAIN
    from .resources import AitoResourceError, extract_car_image

    resources = assets.get("vehicle_resources") if isinstance(assets, dict) else None
    if not isinstance(resources, dict):
        return
    account_dir = hass.config.path(".storage", DOMAIN, "resources", str(assets.get(CONF_PHONE) or ""))
    www_dir = hass.config.path("www", DOMAIN)
    default_alias = os.path.join(www_dir, "car.png")
    for index, vehicle in enumerate(vehicles):
        manifest = resources.get(vehicle.id)
        archive_rel = manifest.get("archive") if isinstance(manifest, dict) else None
        if not archive_rel:
            continue
        archive = os.path.join(account_dir, archive_rel)
        destination = os.path.join(www_dir, f"{vehicle.id}.png")
        try:
            if not os.path.isfile(destination):
                written = await hass.async_add_executor_job(extract_car_image, archive, destination)
                if written:
                    _LOGGER.info("AITO extracted car image to %s", destination)
            # The card references a fixed name; keep it pointed at the first vehicle.
            if index == 0 and os.path.isfile(destination) and not os.path.isfile(default_alias):
                await hass.async_add_executor_job(shutil.copyfile, destination, default_alias)
        except (AitoResourceError, OSError):
            _LOGGER.warning("AITO could not extract car image for vehicle %s", vehicle.id, exc_info=True)


def _remove_legacy_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    vehicles: list[Vehicle],
    vehicle_specs: dict[str, Any],
) -> None:
    """Remove entities created by versions that exposed live vehicle state."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    raw_status_unique_ids = {f"{vehicle.id}_raw_vehicle_status" for vehicle in vehicles}
    mapped_unique_ids = {
        f"{vehicle_id}_{sensor.key}"
        for vehicle_id, spec in vehicle_specs.items()
        for sensor in spec.sensors
    }
    switch_unique_ids = {
        f"{vehicle_id}_now_departure_plan"
        for vehicle_id, spec in vehicle_specs.items()
        if spec.supports_now_departure_plan
    }
    switch_unique_ids.update(
        f"{vehicle_id}_sentry_mode"
        for vehicle_id, spec in vehicle_specs.items()
        if spec.supports_sentry_mode
    )
    climate_unique_ids = {
        f"{vehicle_id}_air_conditioner"
        for vehicle_id, spec in vehicle_specs.items()
        if spec.supports_air_conditioner
    }
    location_unique_ids = {
        f"{vehicle_id}_location"
        for vehicle_id, spec in vehicle_specs.items()
        if spec.supports_location
    }
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id not in raw_status_unique_ids | mapped_unique_ids | switch_unique_ids | climate_unique_ids | location_unique_ids:
            registry.async_remove(entity.entity_id)


async def _async_capture_raw_status_snapshots(
    hass: HomeAssistant,
    assets: dict[str, Any],
    identity: dict[str, Any],
    vehicles: list[Vehicle],
) -> dict[str, dict[str, Any]]:
    """Capture APIG status once; the response is intentionally not persisted by us."""
    from .api import AitoApiClient

    authorization = assets.get(CONF_APIG_AUTHORIZATION)
    device_id = _identity_value(identity, CONF_IVCS_DEVICE_ID) or assets.get(CONF_IVCS_DEVICE_ID)
    if not isinstance(authorization, str) or not authorization or not isinstance(device_id, str) or not device_id:
        return {}
    client = AitoApiClient(
        apig_authorization=authorization,
        ivcs_device_id=device_id,
        apig_verify_ssl=False,
    )
    _restore_enterprise_code(client, vehicles)
    snapshots: dict[str, dict[str, Any]] = {}
    for vehicle in vehicles:
        try:
            response = await hass.async_add_executor_job(client.dynamic_infos, vehicle.id)
        except Exception:
            _LOGGER.warning("AITO raw vehicle status snapshot failed", exc_info=True)
            return {}
        if not isinstance(response, dict):
            _LOGGER.warning("AITO raw vehicle status snapshot was not an object")
            return {}
        snapshots[vehicle.id] = response
    return snapshots


def _validate_saved_login_context(
    assets: dict[str, Any],
    identity: dict[str, Any],
    *,
    require_identity: bool,
) -> None:
    required_asset_values = (
        assets.get(CONF_PHONE),
        assets.get(CONF_ENCRYPTED_PASSWORD),
        assets.get(CONF_ENCRYPTED_SESSION_CONTEXT),
    )
    if not all(isinstance(value, str) and value for value in required_asset_values):
        _raise_setup_auth_failed("AITO saved login context is incomplete")
    credential_key = identity.get("credential_key")
    if require_identity and not (isinstance(credential_key, str) and credential_key):
        _raise_setup_auth_failed("AITO saved login context is incomplete")
    if require_identity and not all(
        isinstance(identity.get(key), str) and identity[key]
        for key in (CONF_DEVICE_ID, CONF_OMP_DEVICE_ID, CONF_IVCS_DEVICE_ID, "huawei_user_id")
    ):
        _raise_setup_auth_failed("AITO saved device identity is incomplete")
    if require_identity:
        try:
            decrypt_password(str(assets[CONF_ENCRYPTED_PASSWORD]), str(credential_key))
            session_context = _saved_session_context(assets, identity)
            if not all(isinstance(session_context.get(key), str) and session_context[key] for key in ("tgc", "jsessionid")):
                raise ValueError("saved session context is incomplete")
            if not all(isinstance(session_context.get(key), dict) for key in ("huawei_cookies", "omp_cookies")):
                raise ValueError("saved session cookies are incomplete")
        except Exception:
            _raise_setup_auth_failed("AITO saved login context is invalid")


def _identity_value(identity: dict[str, Any], key: str) -> str | None:
    value = identity.get(key)
    return str(value) if isinstance(value, str) and value else None


def _saved_session_context(assets: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    encrypted_context = assets.get(CONF_ENCRYPTED_SESSION_CONTEXT)
    if not encrypted_context:
        return {}
    credential_key = identity.get("credential_key")
    if not isinstance(encrypted_context, str) or not isinstance(credential_key, str) or not credential_key:
        raise ValueError("saved session context is invalid")
    return decrypt_session_context(encrypted_context, credential_key)
