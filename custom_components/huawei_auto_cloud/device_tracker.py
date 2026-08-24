"""Vehicle location tracker for verified VehicleSpecs."""

from __future__ import annotations

from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HuaweiAutoCloudCoordinator
from .models import vehicle_device_info


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator: HuaweiAutoCloudCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        VehicleLocationTracker(coordinator, route_id, coordinator.vehicles[route_id], route)
        for route_id, route in coordinator.routes.items()
        if (spec := coordinator.vehicle_specs.get(route_id)) and spec.supports_location
    )


class VehicleLocationTracker(CoordinatorEntity[HuaweiAutoCloudCoordinator], TrackerEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "location"

    def __init__(self, coordinator: HuaweiAutoCloudCoordinator, route_id: str, vehicle, route) -> None:
        super().__init__(coordinator)
        self._route_id = route_id
        self._attr_unique_id = f"{route.route_id}_location"
        self._attr_device_info = vehicle_device_info(vehicle, route)

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        return self._coordinates[0] if self._coordinates else None

    @property
    def longitude(self) -> float | None:
        return self._coordinates[1] if self._coordinates else None

    @property
    def _coordinates(self) -> tuple[float, float] | None:
        data = self.coordinator.data.get(self._route_id, {}) if self.coordinator.data else {}
        location = data.get("location") if isinstance(data, dict) else None
        point = location.get("location") if isinstance(location, dict) else None
        if not isinstance(point, dict):
            return None
        latitude, longitude = point.get("latitude"), point.get("longitude")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)) or isinstance(latitude, bool) or isinstance(longitude, bool):
            return None
        return float(latitude), float(longitude)
