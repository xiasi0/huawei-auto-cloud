"""Huawei Auto Cloud integration."""

from __future__ import annotations

import logging
import os
import shutil
from copy import deepcopy
from typing import Any, TYPE_CHECKING

try:
    from homeassistant.exceptions import ConfigEntryAuthFailed
except ModuleNotFoundError:
    class ConfigEntryAuthFailed(RuntimeError):
        """Fallback used only by standalone unit tests without Home Assistant."""

from .const import CONF_ASSET_KEY, DOMAIN, PLATFORMS
from .models import resource_manifests_from_profile_responses, vehicle_device_info
from .resources import VehicleResourceError, cache_vehicle_resources, extract_car_image, remove_vehicle_resources, resource_cache_needs_recovery
from .storage import IdentityStore, PhoneAssetStore

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import HuaweiAutoCloudCoordinator

    asset_key = entry.data.get(CONF_ASSET_KEY)
    if not isinstance(asset_key, str) or not asset_key:
        raise ValueError("Huawei Auto Cloud entry is missing its phone-named asset key")
    await IdentityStore(hass).async_get_or_create(asset_key)
    store = PhoneAssetStore(hass, asset_key)
    revision, payload = await store.async_load()
    if not payload:
        raise ConfigEntryAuthFailed("Huawei Auto Cloud account asset is missing; reauthenticate")
    try:
        coordinator = HuaweiAutoCloudCoordinator(hass, entry, store, revision, payload)
    except (TypeError, ValueError) as error:
        raise ConfigEntryAuthFailed("Huawei Auto Cloud account asset requires reauthentication") from error
    await coordinator.async_config_entry_first_refresh()
    _register_devices(hass, entry, coordinator)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.async_create_task(
        _async_recover_vehicle_resources(hass, asset_key, store, revision, payload, coordinator)
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove phone-named account assets while retaining device identity."""
    asset_key = entry.data.get(CONF_ASSET_KEY)
    if not isinstance(asset_key, str) or not asset_key:
        return
    # PhoneAssetStore deliberately keeps diagnostic fields in plain JSON and
    # does not need the identity key to remove them. Do not create a new device
    # identity merely because a user is deleting an entry after corruption.
    await PhoneAssetStore(hass, asset_key).async_remove()
    await hass.async_add_executor_job(
        remove_vehicle_resources,
        hass.config.path(".storage", DOMAIN, "resources"),
        asset_key,
    )


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_recover_vehicle_resources(
    hass: HomeAssistant,
    asset_key: str,
    store: PhoneAssetStore,
    revision: int,
    payload: dict,
    coordinator: HuaweiAutoCloudCoordinator,
) -> None:
    """Best-effort resource recovery; never block entry setup or login."""
    vehicle_gateway = payload.get("vehicle_gateway")
    vehicles = payload.get("vehicles")
    if not isinstance(vehicle_gateway, dict) or not isinstance(vehicles, dict):
        return
    raw_profiles = vehicle_gateway.get("vehicle_profile_responses")
    raw_management_queries = vehicle_gateway.get("vehicle_management_query_responses")
    raw_discovery_responses: dict[str, Any] = {}
    if isinstance(raw_profiles, dict):
        raw_discovery_responses.update({f"profile:{key}": value for key, value in raw_profiles.items()})
    if isinstance(raw_management_queries, dict):
        raw_discovery_responses.update({f"management_query:{key}": value for key, value in raw_management_queries.items()})
    if not raw_discovery_responses:
        return
    vehicle_ids = {
        route.get("vehicle_id")
        for item in vehicles.values()
        if isinstance(item, dict)
        for route in (item.get("route"),)
        if isinstance(route, dict) and isinstance(route.get("vehicle_id"), str)
    }
    manifests = resource_manifests_from_profile_responses(raw_discovery_responses, vehicle_ids)
    resources = payload.get("resources")
    storage_root = hass.config.path(".storage", DOMAIN, "resources")
    if not manifests or not resource_cache_needs_recovery(storage_root, asset_key, manifests, resources):
        return
    try:
        cached = await hass.async_add_executor_job(
            cache_vehicle_resources,
            storage_root,
            asset_key,
            manifests,
        )
    except VehicleResourceError:
        _LOGGER.warning("Huawei Auto Cloud could not recover vehicle resource cache", exc_info=True)
        return
    updated = deepcopy(payload)
    resources = dict(updated.get("resources", {}))
    for vehicle_id, manifest in manifests.items():
        resources[vehicle_id] = {"manifest": manifest, "cache": cached[vehicle_id]}
    updated["resources"] = resources
    if not await store.async_save_if_revision(revision, revision + 1, updated):
        _LOGGER.debug("Huawei Auto Cloud resource metadata changed concurrently; retry next setup")
        return
    await _async_extract_car_images(hass, asset_key, updated, coordinator)


def _register_devices(hass: HomeAssistant, entry: ConfigEntry, coordinator: HuaweiAutoCloudCoordinator) -> None:
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    for route_id, route in coordinator.routes.items():
        vehicle = coordinator.vehicles[route_id]
        info = vehicle_device_info(vehicle, route)
        registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers=info["identifiers"], name=info.get("name"), manufacturer=info.get("manufacturer"), model=info.get("model"), sw_version=info.get("sw_version"))


async def _async_extract_car_images(hass: HomeAssistant, asset_key: str, payload, coordinator: HuaweiAutoCloudCoordinator) -> None:
    """Extract each cached vehicle resource archive into the local web directory."""
    resources = payload.get("resources") if isinstance(payload, dict) else None
    if not isinstance(resources, dict):
        return
    account_dir = hass.config.path(".storage", DOMAIN, "resources", asset_key)
    web_dir = hass.config.path("www", DOMAIN)
    default_alias = os.path.join(web_dir, "car.png")
    for index, route_id in enumerate(coordinator.routes):
        vehicle_id = coordinator.routes[route_id].vehicle_id
        resource = resources.get(vehicle_id)
        cache = resource.get("cache") if isinstance(resource, dict) else None
        archive_rel = cache.get("archive") if isinstance(cache, dict) else None
        if not isinstance(archive_rel, str) or not archive_rel:
            continue
        archive = os.path.join(account_dir, archive_rel)
        destination = os.path.join(web_dir, f"{vehicle_id}.png")
        try:
            if not os.path.isfile(destination):
                await hass.async_add_executor_job(extract_car_image, archive, destination)
            if index == 0 and os.path.isfile(destination) and not os.path.isfile(default_alias):
                await hass.async_add_executor_job(shutil.copyfile, destination, default_alias)
        except (VehicleResourceError, OSError):
            _LOGGER.warning("Huawei Auto Cloud could not extract a vehicle resource image", exc_info=True)
