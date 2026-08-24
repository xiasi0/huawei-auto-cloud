"""Verified normal-control switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HuaweiAutoCloudCoordinator
from .models import vehicle_device_info


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator: HuaweiAutoCloudCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for route_id, route in coordinator.routes.items():
        spec = coordinator.vehicle_specs.get(route_id)
        if spec is None:
            continue
        vehicle = coordinator.vehicles[route_id]
        if spec.supports_now_departure_plan:
            entities.append(NowDeparturePlanSwitch(coordinator, route_id, vehicle, route, "now_departure_plan"))
        if spec.supports_sentry_mode:
            entities.append(SentryModeSwitch(coordinator, route_id, vehicle, route, "sentry_mode"))
    async_add_entities(entities)


class _RouteSwitch(CoordinatorEntity[HuaweiAutoCloudCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HuaweiAutoCloudCoordinator, route_id: str, vehicle, route, suffix: str) -> None:
        super().__init__(coordinator)
        self._route_id = route_id
        self._attr_unique_id = f"{route.route_id}_{suffix}"
        self._attr_device_info = vehicle_device_info(vehicle, route)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.is_route_controllable(self._route_id)


class NowDeparturePlanSwitch(_RouteSwitch):
    _attr_translation_key = "now_departure_plan"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data.get(self._route_id, {}) if self.coordinator.data else {}
        plans = data.get("departurePlan", {}).get("departurePlanList", []) if isinstance(data.get("departurePlan"), dict) else []
        plan = next((item for item in plans if isinstance(item, dict) and item.get("planId") in {0, "0"}), None)
        return plan.get("planStatus") in {0, "0"} if isinstance(plan, dict) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_now_departure_plan(self._route_id, enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_now_departure_plan(self._route_id, enabled=False)


class SentryModeSwitch(_RouteSwitch):
    _attr_translation_key = "sentry_mode"

    @property
    def is_on(self) -> bool | None:
        value = self._status
        return value in {1, "1", 2, "2"} if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"sentry_mode_status": self._status} if self._status is not None else {}

    @property
    def _status(self) -> Any:
        data = self.coordinator.data.get(self._route_id, {}) if self.coordinator.data else {}
        status = data.get("vehicleStatus") if isinstance(data, dict) else None
        return status.get("sentryModeStatus") if isinstance(status, dict) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_sentry_mode(self._route_id, enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_sentry_mode(self._route_id, enabled=False)
