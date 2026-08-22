"""Verified normal HVAC controls."""

from __future__ import annotations

from math import isclose
from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HuaweiAutoCloudCoordinator
from .models import vehicle_device_info

_PRESETS = ("rapid_cool", "rapid_heat", "defrost")


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator: HuaweiAutoCloudCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        AirConditioner(coordinator, route_id, coordinator.vehicles[route_id], route)
        for route_id, route in coordinator.routes.items()
        if (spec := coordinator.vehicle_specs.get(route_id)) and spec.supports_air_conditioner
    )


class AirConditioner(CoordinatorEntity[HuaweiAutoCloudCoordinator], ClimateEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "air_conditioner"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
    _attr_preset_modes = list(_PRESETS)
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 16
    _attr_max_temp = 31
    _attr_target_temperature_step = 0.5

    def __init__(self, coordinator: HuaweiAutoCloudCoordinator, route_id: str, vehicle, route) -> None:
        super().__init__(coordinator)
        self._route_id = route_id
        self._attr_unique_id = f"{route.route_id}_air_conditioner"
        self._attr_device_info = vehicle_device_info(vehicle, route)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.is_route_controllable(self._route_id) and self._hvac is not None

    @property
    def hvac_mode(self) -> HVACMode | None:
        status = self._value("acStatus")
        return HVACMode.AUTO if status in {1, "1"} else HVACMode.OFF if status is not None else None

    @property
    def target_temperature(self) -> float | None:
        return _temperature(self._value("remoteTemp"), require_valid_target=True)

    @property
    def current_temperature(self) -> float | None:
        return _temperature(self._value("insideTemp"))

    @property
    def preset_mode(self) -> str | None:
        active = [preset for preset, field in (("rapid_cool", "maxColdSwitch"), ("rapid_heat", "maxHeatSwitch"), ("defrost", "defrostStatus")) if self._value(field) in {1, "1"}]
        return active[0] if len(active) == 1 else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        elif hvac_mode == HVACMode.AUTO:
            await self.async_turn_on()
        else:
            raise ValueError("unsupported HVAC mode")

    async def async_turn_on(self) -> None:
        await self.coordinator.async_control_air_conditioner(self._route_id, enabled=True, target_temp=_target_tenths(self.target_temperature))

    async def async_turn_off(self) -> None:
        await self.coordinator.async_control_air_conditioner(self._route_id, enabled=False)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_air_conditioner(self._route_id, enabled=True, target_temp=_target_tenths(kwargs.get(ATTR_TEMPERATURE)))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in _PRESETS:
            raise ValueError("unsupported HVAC preset")
        if preset_mode == self.preset_mode:
            return
        if current := self.preset_mode:
            await self._set_preset(current, False)
        await self._set_preset(preset_mode, True)

    async def _set_preset(self, preset: str, enabled: bool) -> None:
        if preset == "rapid_cool":
            await self.coordinator.async_control_air_conditioner_rapid(self._route_id, enabled=enabled, mode=1)
        elif preset == "rapid_heat":
            await self.coordinator.async_control_air_conditioner_rapid(self._route_id, enabled=enabled, mode=2)
        else:
            await self.coordinator.async_control_defrost(self._route_id, enabled=enabled)

    @property
    def _hvac(self) -> dict[str, Any] | None:
        data = self.coordinator.data.get(self._route_id, {}) if self.coordinator.data else {}
        hvac = data.get("hvac") if isinstance(data, dict) else None
        return hvac if isinstance(hvac, dict) else None

    def _value(self, field: str) -> Any:
        return self._hvac.get(field) if self._hvac is not None else None


def _temperature(value: Any, *, require_valid_target: bool = False) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if require_valid_target and (not 160 <= value <= 310 or value % 5):
        return None
    return value / 10


def _target_tenths(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 16 <= value <= 31 or not isclose(value * 2, round(value * 2)):
        raise ValueError("air-conditioner temperature must be 16.0-31.0 C in 0.5 C steps")
    return round(value * 10)
