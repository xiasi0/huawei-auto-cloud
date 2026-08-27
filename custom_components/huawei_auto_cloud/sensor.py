"""Sensors declared by a route's verified VehicleSpec."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HuaweiAutoCloudCoordinator
from .models import vehicle_device_info
from .specs import SensorSpec, sensor_value


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator: HuaweiAutoCloudCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for route_id, route in coordinator.routes.items():
        spec = coordinator.vehicle_specs.get(route_id)
        if spec is None:
            continue
        vehicle = coordinator.vehicles[route_id]
        entities.append(VehicleModelSensor(vehicle, route))
        entities.extend(MappedSensor(coordinator, route_id, sensor) for sensor in spec.sensors)
    async_add_entities(entities)


class VehicleModelSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "model"

    def __init__(self, vehicle, route) -> None:
        self._attr_unique_id = f"{route.route_id}_model"
        self._attr_device_info = vehicle_device_info(vehicle, route)
        self._attr_native_value = vehicle.model or vehicle.name


class MappedSensor(CoordinatorEntity[HuaweiAutoCloudCoordinator], RestoreSensor):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HuaweiAutoCloudCoordinator, route_id: str, spec: SensorSpec) -> None:
        super().__init__(coordinator)
        self._route_id, self._spec = route_id, spec
        route, vehicle = coordinator.routes[route_id], coordinator.vehicles[route_id]
        self._attr_unique_id = f"{route.route_id}_{spec.key}"
        self._attr_device_info = vehicle_device_info(vehicle, route)
        self._attr_translation_key = spec.translation_key
        self._last_value: Any = None
        if spec.device_class:
            self._attr_device_class = SensorDeviceClass(spec.device_class)
        if spec.native_unit_of_measurement:
            self._attr_native_unit_of_measurement = spec.native_unit_of_measurement
        if spec.state_class:
            self._attr_state_class = SensorStateClass(spec.state_class)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._spec.sticky and (last := await self.async_get_last_sensor_data()) is not None:
            self._last_value = last.native_value

    @property
    def native_value(self):
        data = self.coordinator.data.get(self._route_id, {}) if self.coordinator.data else {}
        value = sensor_value(data, self._spec)
        if value is not None:
            self._last_value = value
        return self._last_value if value is None and self._spec.sticky else value
