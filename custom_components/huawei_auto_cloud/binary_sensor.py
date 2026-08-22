"""Route-level vehicle connection state."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HuaweiAutoCloudCoordinator
from .models import vehicle_device_info


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator: HuaweiAutoCloudCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        VehicleOnlineSensor(coordinator, route_id, coordinator.vehicles[route_id], route)
        for route_id, route in coordinator.routes.items()
    )


class VehicleOnlineSensor(CoordinatorEntity[HuaweiAutoCloudCoordinator], BinarySensorEntity):
    """The authoritative cloud connection state used by the polling policy."""

    _attr_has_entity_name = True
    _attr_translation_key = "vehicle_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: HuaweiAutoCloudCoordinator, route_id: str, vehicle, route) -> None:
        super().__init__(coordinator)
        self._route_id = route_id
        self._attr_unique_id = f"{route.route_id}_online"
        self._attr_device_info = vehicle_device_info(vehicle, route)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.route_online.get(self._route_id) is not None

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.route_online.get(self._route_id)
