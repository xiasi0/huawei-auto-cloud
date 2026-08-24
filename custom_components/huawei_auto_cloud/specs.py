"""Reviewed vehicle specifications and capability declarations.

This module is the only place to add a supported model. It deliberately
contains capability data, not endpoint, session, or HTTP logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable

from .models import Vehicle


class VehicleControl(StrEnum):
    """Normal controls with a reviewed request contract for this vehicle."""

    AIR_CONDITIONER = "air_conditioner"
    SENTRY_MODE = "sentry_mode"
    DEPARTURE_PLAN = "departure_plan"


class ChargeStatus(StrEnum):
    """Stable, translatable meanings for the two observed charge-status fields."""

    NOT_CHARGING = "not_charging"
    CONNECTED_NOT_CHARGING = "connected_not_charging"
    CHARGING = "charging"
    COMPLETE = "complete"
    FAULT = "fault"
    PAUSED = "paused"
    STOPPED = "stopped"
    PREHEATING = "preheating"
    SCHEDULED = "scheduled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SensorSpec:
    """One Home Assistant sensor mapped to a verified response field."""

    key: str
    path: tuple[str, ...]
    translation_key: str
    source: str = "dynamic"
    device_class: str | None = None
    native_unit_of_measurement: str | None = None
    state_class: str | None = None
    converter: Callable[[Any], Any] | None = None
    value_getter: Callable[[dict[str, Any]], Any] | None = None
    sticky: bool = False


@dataclass(frozen=True)
class VehicleSpec:
    """Exact enterprise/project match and capabilities published by this integration."""

    key: str
    enterprise_code: str
    project_code: str
    sensors: tuple[SensorSpec, ...]
    controls: frozenset[VehicleControl] = frozenset()
    supports_location: bool = False

    def matches(self, vehicle: Vehicle) -> bool:
        return (
            vehicle.profile.enterprise_code == self.enterprise_code
            and vehicle.profile.project_code == self.project_code
        )

    def supports(self, control: VehicleControl) -> bool:
        return control in self.controls

    @property
    def supports_now_departure_plan(self) -> bool:
        return self.supports(VehicleControl.DEPARTURE_PLAN)

    @property
    def supports_sentry_mode(self) -> bool:
        return self.supports(VehicleControl.SENTRY_MODE)

    @property
    def supports_air_conditioner(self) -> bool:
        return self.supports(VehicleControl.AIR_CONDITIONER)


def _absolute_number(value: Any) -> float | int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value != -1:
        return abs(value)
    return None


def _reject_sentinel(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == -1:
        return None
    return value


def _positive_number(value: Any) -> float | int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _tenths(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value != -1:
        return round(value / 10, 1)
    return None


def _epoch_millis(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


def _charge_channel_value(data: dict[str, Any], key: str) -> int | None:
    try:
        value = int(value_at_path(data, ("charge", key)))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _charge_current(data: dict[str, Any]) -> float | int | None:
    ac_status = _charge_channel_value(data, "acChargeStatus")
    dc_status = _charge_channel_value(data, "dcChargeStatus")
    status = max((value for value in (ac_status, dc_status) if value is not None), default=None)
    if status is None or status == 0:
        return _absolute_number(value_at_path(data, ("charge", "chargeCurrent")))
    if status != 6:
        return 0
    key = "acChargeCurrent" if ac_status == 6 else "dcChargeCurrent"
    return _absolute_number(value_at_path(data, ("charge", key)))


def _charge_power_kw(data: dict[str, Any]) -> float | None:
    current = _charge_current(data)
    voltage = _absolute_number(value_at_path(data, ("charge", "chargeVoltage")))
    if current is None or voltage is None:
        return None
    return round(int(round(current)) * int(round(voltage)) / 1000, 1)


_CHARGE_STATUS_BY_CODE = {
    0: ChargeStatus.NOT_CHARGING,
    1: ChargeStatus.CHARGING,
    2: ChargeStatus.COMPLETE,
    3: ChargeStatus.FAULT,
    4: ChargeStatus.PAUSED,
    5: ChargeStatus.FAULT,
    6: ChargeStatus.CHARGING,
    7: ChargeStatus.STOPPED,
    18: ChargeStatus.PREHEATING,
    25: ChargeStatus.SCHEDULED,
}
_CHANNEL_CHARGE_STATUS_BY_CODE = {
    0: ChargeStatus.NOT_CHARGING,
    1: ChargeStatus.CONNECTED_NOT_CHARGING,
    5: ChargeStatus.FAULT,
    6: ChargeStatus.CHARGING,
    7: ChargeStatus.STOPPED,
    18: ChargeStatus.PREHEATING,
    25: ChargeStatus.SCHEDULED,
}


def _charge_display_status(data: dict[str, Any]) -> str | None:
    statuses = (_charge_channel_value(data, "acChargeStatus"), _charge_channel_value(data, "dcChargeStatus"))
    status = max((value for value in statuses if value is not None), default=None)
    if status is not None and status != 0:
        return _CHANNEL_CHARGE_STATUS_BY_CODE.get(status, ChargeStatus.UNKNOWN)
    try:
        status = int(value_at_path(data, ("charge", "chargeStatus")))
    except (TypeError, ValueError):
        return None
    return _CHARGE_STATUS_BY_CODE.get(status, ChargeStatus.UNKNOWN)


def _vehicle_move_status(value: Any) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value in {0, 1, 2} else None


# All currently supported SERES projects have this exact verified dynamic shape.
# Keep it shared until a real fixture proves a project-specific difference.
SERES_COMMON_SENSORS: tuple[SensorSpec, ...] = (
    SensorSpec(
        key="battery_soc", path=("charge", "soc"), translation_key="battery_soc",
        device_class="battery", native_unit_of_measurement="%", state_class="measurement",
        converter=_reject_sentinel, sticky=True,
    ),
    SensorSpec(
        key="charge_voltage", path=("charge", "chargeVoltage"), translation_key="charge_voltage",
        device_class="voltage", native_unit_of_measurement="V", state_class="measurement",
        converter=_absolute_number, sticky=True,
    ),
    SensorSpec(
        key="charge_current", path=("charge", "chargeCurrent"), translation_key="charge_current",
        device_class="current", native_unit_of_measurement="A", state_class="measurement",
        value_getter=_charge_current, sticky=True,
    ),
    SensorSpec(
        key="charge_power", path=("charge",), translation_key="charge_power",
        device_class="power", native_unit_of_measurement="kW", state_class="measurement",
        value_getter=_charge_power_kw, sticky=True,
    ),
    SensorSpec(
        key="remaining_charge_time", path=("charge", "remainChargeTime"), translation_key="remaining_charge_time",
        device_class="duration", native_unit_of_measurement="min", state_class="measurement",
        converter=_reject_sentinel, sticky=True,
    ),
    SensorSpec(
        key="electric_wltc_remaining_mileage", path=("charge", "vcuWltcRemainingMileage"), translation_key="electric_wltc_remaining_mileage",
        device_class="distance", native_unit_of_measurement="km", state_class="measurement",
        converter=_reject_sentinel, sticky=True,
    ),
    SensorSpec(
        key="wltc_remaining_mileage", path=("vehicleStatus", "wltcRemainingMileage"), translation_key="wltc_remaining_mileage",
        device_class="distance", native_unit_of_measurement="km", state_class="measurement",
        converter=_reject_sentinel, sticky=True,
    ),
    SensorSpec(
        key="total_mileage", path=("vehicleStatus", "totalMileage"), translation_key="total_mileage",
        device_class="distance", native_unit_of_measurement="km", state_class="total_increasing",
        converter=_reject_sentinel, sticky=True,
    ),
    SensorSpec(
        key="fuel_wltc_remaining_mileage", path=("fuel", "fuelWltcRemainingMileage"), translation_key="fuel_wltc_remaining_mileage",
        device_class="distance", native_unit_of_measurement="km", state_class="measurement",
        converter=_reject_sentinel, sticky=True,
    ),
    SensorSpec(
        key="fuel_remaining", path=("fuel", "leftPercent"), translation_key="fuel_remaining",
        native_unit_of_measurement="%", state_class="measurement", converter=_reject_sentinel,
        sticky=True,
    ),
    SensorSpec(
        key="average_power_consumption", path=("energyReport", "total", "avgPowerConsum"), translation_key="average_power_consumption",
        source="energy_report", native_unit_of_measurement="kWh/100km", state_class="measurement",
    ),
    SensorSpec(
        key="average_fuel_consumption", path=("energyReport", "total", "avgFuelConsum"), translation_key="average_fuel_consumption",
        source="energy_report", native_unit_of_measurement="L/100km", state_class="measurement",
    ),
    SensorSpec(
        key="sum_remaining_mileage", path=("vehicleStatus", "sumRemainingMileage"), translation_key="sum_remaining_mileage",
        device_class="distance", native_unit_of_measurement="km", state_class="measurement",
        converter=_reject_sentinel, sticky=True,
    ),
    SensorSpec(
        key="last_updated_at", path=("vehicleStatus", "lastUpdatedAt"), translation_key="last_updated_at",
        device_class="timestamp", converter=_epoch_millis,
    ),
    SensorSpec(
        key="last_online_at", path=("vehicleStatus", "lastOnlineAt"), translation_key="last_online_at",
        device_class="timestamp", converter=_epoch_millis,
    ),
    SensorSpec(
        key="charge_status", path=("charge", "chargeStatus"), translation_key="charge_status",
        value_getter=_charge_display_status,
    ),
    SensorSpec(
        key="vehicle_move_status", path=("vehicleStatus", "vehicleMoveStatus"), translation_key="vehicle_move_status",
        converter=_vehicle_move_status,
    ),
    SensorSpec(
        key="inside_temperature", path=("hvac", "insideTemp"), translation_key="inside_temperature",
        device_class="temperature", native_unit_of_measurement="°C", state_class="measurement",
        converter=_tenths, sticky=True,
    ),
    SensorSpec(
        key="air_conditioner_target_temperature", path=("hvac", "remoteTemp"), translation_key="air_conditioner_target_temperature",
        device_class="temperature", native_unit_of_measurement="°C", state_class="measurement",
        converter=_tenths, sticky=True,
    ),
    SensorSpec(
        key="tire_pressure_left_front", path=("tire", "leftFront", "pressure"), translation_key="tire_pressure_left_front",
        device_class="pressure", native_unit_of_measurement="bar", state_class="measurement",
        converter=_positive_number, sticky=True,
    ),
    SensorSpec(
        key="tire_pressure_right_front", path=("tire", "rightFront", "pressure"), translation_key="tire_pressure_right_front",
        device_class="pressure", native_unit_of_measurement="bar", state_class="measurement",
        converter=_positive_number, sticky=True,
    ),
    SensorSpec(
        key="tire_pressure_left_back", path=("tire", "leftBack", "pressure"), translation_key="tire_pressure_left_back",
        device_class="pressure", native_unit_of_measurement="bar", state_class="measurement",
        converter=_positive_number, sticky=True,
    ),
    SensorSpec(
        key="tire_pressure_right_back", path=("tire", "rightBack", "pressure"), translation_key="tire_pressure_right_back",
        device_class="pressure", native_unit_of_measurement="bar", state_class="measurement",
        converter=_positive_number, sticky=True,
    ),
)


SPECS: tuple[VehicleSpec, ...] = (
    VehicleSpec(
        key="seres_f3",
        enterprise_code="SERES",
        project_code="SERES-F3",
        sensors=SERES_COMMON_SENSORS,
        controls=frozenset({
            VehicleControl.AIR_CONDITIONER,
            VehicleControl.SENTRY_MODE,
            VehicleControl.DEPARTURE_PLAN,
        }),
        supports_location=True,
    ),
    VehicleSpec(
        key="seres_aito_a15",
        enterprise_code="SERES",
        project_code="AITO-A15",
        sensors=SERES_COMMON_SENSORS,
        supports_location=True,
    ),
    VehicleSpec(
        key="seres_x1",
        enterprise_code="SERES",
        project_code="SERES-X1",
        sensors=SERES_COMMON_SENSORS,
        controls=frozenset({
            VehicleControl.AIR_CONDITIONER,
            VehicleControl.SENTRY_MODE,
        }),
        supports_location=True,
    ),
)


def vehicle_spec_for(vehicle: Vehicle) -> VehicleSpec | None:
    return next((spec for spec in SPECS if spec.matches(vehicle)), None)


def dynamic_sections(spec: VehicleSpec) -> dict[str, int]:
    sections = {sensor.path[0]: 0 for sensor in spec.sensors if sensor.source == "dynamic"}
    if spec.supports_now_departure_plan:
        sections["departurePlan"] = 0
    if spec.supports_sentry_mode:
        sections["vehicleStatus"] = 0
    if spec.supports_air_conditioner:
        sections["hvac"] = 0
    if spec.supports_location:
        sections["location"] = 0
    return sections


def has_energy_report_sensors(spec: VehicleSpec) -> bool:
    return any(sensor.source == "energy_report" for sensor in spec.sensors)


def value_at_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def sensor_value(data: dict[str, Any], spec: SensorSpec) -> Any:
    value = spec.value_getter(data) if spec.value_getter else value_at_path(data, spec.path)
    return spec.converter(value) if spec.converter else value
