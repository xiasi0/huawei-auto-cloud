from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .models import Vehicle


@dataclass(frozen=True)
class SensorSpec:
    key: str
    path: tuple[str, ...]
    translation_key: str
    source: str = "dynamic"
    device_class: str | None = None
    native_unit_of_measurement: str | None = None
    state_class: str | None = None
    converter: Callable[[Any], Any] | None = None
    value_getter: Callable[[dict[str, Any]], Any] | None = None
    # Keep the previous reading when the converter yields None (e.g. the -1
    # no-data placeholder while the vehicle sleeps) instead of dropping to
    # unknown. Used by tire pressure, SOC, range, temperatures, etc.
    sticky: bool = False


@dataclass(frozen=True)
class VehicleSpec:
    key: str
    enterprise_code: str
    project_code: str
    sensors: tuple[SensorSpec, ...]
    supports_now_departure_plan: bool = False
    supports_sentry_mode: bool = False
    supports_air_conditioner: bool = False
    supports_location: bool = False

    def matches(self, vehicle: Vehicle) -> bool:
        profile = vehicle.profile
        if profile.enterprise_code != self.enterprise_code:
            return False
        # When this spec pins a project_code, both must agree. A spec with an
        # empty project_code matches any vehicle of that enterprise (used for
        # CHERY/LUXEED until the real project code is captured).
        return not self.project_code or profile.project_code == self.project_code


def _absolute_number(value: Any) -> float | int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value != -1:
        return abs(value)
    return None


def _reject_sentinel(value: Any) -> Any:
    """Treat the APIG -1 no-data placeholder as missing.

    Combined with a sticky sensor this makes the entity hold its last real
    reading instead of dropping to unknown. -1 is safe to special-case: the
    tenth-degree temperatures encode a real -1C as -10, never -1.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == -1:
        return None
    return value


def _charge_channel_value(data: dict[str, Any], key: str) -> int | None:
    try:
        status = int(value_at_path(data, ("charge", key)))
    except (TypeError, ValueError):
        return None
    return status if status >= 0 else None


def _charge_channel_status(data: dict[str, Any]) -> int | None:
    statuses = [
        status
        for key in ("acChargeStatus", "dcChargeStatus")
        if (status := _charge_channel_value(data, key)) is not None
    ]
    return max(statuses) if statuses else None


def _charge_current(data: dict[str, Any]) -> float | int | None:
    """Select the active charging channel, as the official app does."""
    channel_status = _charge_channel_status(data)
    if channel_status is None or channel_status == 0:
        return _absolute_number(value_at_path(data, ("charge", "chargeCurrent")))
    if channel_status != 6:
        return 0

    if _charge_channel_value(data, "acChargeStatus") == 6:
        return _absolute_number(value_at_path(data, ("charge", "acChargeCurrent")))
    return _absolute_number(value_at_path(data, ("charge", "dcChargeCurrent")))


def _charge_power_kw(data: dict[str, Any]) -> float | None:
    current = _charge_current(data)
    voltage = _absolute_number(value_at_path(data, ("charge", "chargeVoltage")))
    if current is None or voltage is None:
        return None
    return round(int(round(current)) * int(round(voltage)) / 1000, 1)


def _positive_number(value: Any) -> float | int | None:
    """Drop the -1 placeholder the APIG reports while the vehicle sleeps."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _tenths(value: Any) -> float | None:
    """Convert the APIG tenth-degree temperatures (297) into Celsius (29.7)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value != -1:
        return round(value / 10, 1)
    return None


_CHARGE_STATUS_TEXT = {
    0: "未充电",
    1: "充电中",
    2: "充电完成",
    3: "充电故障",
    4: "充电暂停",
    5: "充电故障",
    6: "充电中",
    7: "充电已停止",
    18: "电池预热中",
    25: "预约充电",
}
_CHANNEL_CHARGE_STATUS_TEXT = {
    0: "未充电",
    1: "已连接、未充电",
    5: "充电故障",
    6: "充电中",
    7: "充电已停止",
    18: "电池预热中",
    25: "预约充电",
}


def _charge_status_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return _CHARGE_STATUS_TEXT.get(status, f"未知({status})")


def _charge_display_status_text(data: dict[str, Any]) -> str | None:
    channel_status = _charge_channel_status(data)
    if channel_status is None or channel_status == 0:
        return _charge_status_text(value_at_path(data, ("charge", "chargeStatus")))
    return _CHANNEL_CHARGE_STATUS_TEXT.get(channel_status, f"未知({channel_status})")


def _epoch_millis(value: Any) -> datetime | None:
    """Convert the APIG millisecond timestamps into an aware datetime."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


def _vehicle_move_status(value: Any) -> int | None:
    """Keep valid official vehicleMoveStatus enum values."""
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if status in {0, 1, 2} else None


DEVICES: tuple[VehicleSpec, ...] = (
    VehicleSpec(
        key="seres_f3",
        enterprise_code="SERES",
        project_code="SERES-F3",
        supports_now_departure_plan=True,
        supports_sentry_mode=True,
        supports_air_conditioner=True,
        supports_location=True,
        sensors=(
            SensorSpec(
                key="battery_soc",
                path=("charge", "soc"),
                translation_key="battery_soc",
                device_class="battery",
                native_unit_of_measurement="%",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="charge_voltage",
                path=("charge", "chargeVoltage"),
                translation_key="charge_voltage",
                device_class="voltage",
                native_unit_of_measurement="V",
                state_class="measurement",
                converter=_absolute_number,
                sticky=True,
            ),
            SensorSpec(
                key="charge_current",
                path=("charge", "chargeCurrent"),
                translation_key="charge_current",
                device_class="current",
                native_unit_of_measurement="A",
                state_class="measurement",
                value_getter=_charge_current,
                sticky=True,
            ),
            SensorSpec(
                key="charge_power",
                path=("charge",),
                translation_key="charge_power",
                device_class="power",
                native_unit_of_measurement="kW",
                state_class="measurement",
                value_getter=_charge_power_kw,
                sticky=True,
            ),
            SensorSpec(
                key="remaining_charge_time",
                path=("charge", "remainChargeTime"),
                translation_key="remaining_charge_time",
                device_class="duration",
                native_unit_of_measurement="min",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="electric_wltc_remaining_mileage",
                path=("charge", "vcuWltcRemainingMileage"),
                translation_key="electric_wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="wltc_remaining_mileage",
                path=("vehicleStatus", "wltcRemainingMileage"),
                translation_key="wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="total_mileage",
                path=("vehicleStatus", "totalMileage"),
                translation_key="total_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="total_increasing",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="fuel_wltc_remaining_mileage",
                path=("fuel", "fuelWltcRemainingMileage"),
                translation_key="fuel_wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="fuel_remaining",
                path=("fuel", "leftPercent"),
                translation_key="fuel_remaining",
                native_unit_of_measurement="%",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="average_power_consumption",
                path=("energyReport", "total", "avgPowerConsum"),
                translation_key="average_power_consumption",
                source="energy_report",
                native_unit_of_measurement="kWh/100km",
                state_class="measurement",
            ),
            SensorSpec(
                key="average_fuel_consumption",
                path=("energyReport", "total", "avgFuelConsum"),
                translation_key="average_fuel_consumption",
                source="energy_report",
                native_unit_of_measurement="L/100km",
                state_class="measurement",
            ),
            SensorSpec(
                key="sum_remaining_mileage",
                path=("vehicleStatus", "sumRemainingMileage"),
                translation_key="sum_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="last_updated_at",
                path=("vehicleStatus", "lastUpdatedAt"),
                translation_key="last_updated_at",
                device_class="timestamp",
                converter=_epoch_millis,
            ),
            SensorSpec(
                key="last_online_at",
                path=("vehicleStatus", "lastOnlineAt"),
                translation_key="last_online_at",
                device_class="timestamp",
                converter=_epoch_millis,
            ),
            SensorSpec(
                key="charge_status",
                path=("charge", "chargeStatus"),
                translation_key="charge_status",
                value_getter=_charge_display_status_text,
            ),
            SensorSpec(
                key="vehicle_move_status",
                path=("vehicleStatus", "vehicleMoveStatus"),
                translation_key="vehicle_move_status",
                converter=_vehicle_move_status,
            ),
            SensorSpec(
                key="inside_temperature",
                path=("hvac", "insideTemp"),
                translation_key="inside_temperature",
                device_class="temperature",
                native_unit_of_measurement="°C",
                state_class="measurement",
                converter=_tenths,
                sticky=True,
            ),
            SensorSpec(
                key="air_conditioner_target_temperature",
                path=("hvac", "remoteTemp"),
                translation_key="air_conditioner_target_temperature",
                device_class="temperature",
                native_unit_of_measurement="°C",
                state_class="measurement",
                converter=_tenths,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_left_front",
                path=("tire", "leftFront", "pressure"),
                translation_key="tire_pressure_left_front",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_right_front",
                path=("tire", "rightFront", "pressure"),
                translation_key="tire_pressure_right_front",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_left_back",
                path=("tire", "leftBack", "pressure"),
                translation_key="tire_pressure_left_back",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_right_back",
                path=("tire", "rightBack", "pressure"),
                translation_key="tire_pressure_right_back",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
        ),
    ),
    VehicleSpec(
        key="seres_aito_a15",
        enterprise_code="SERES",
        project_code="AITO-A15",
        supports_location=True,
        sensors=(
            SensorSpec(
                key="battery_soc",
                path=("charge", "soc"),
                translation_key="battery_soc",
                device_class="battery",
                native_unit_of_measurement="%",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="charge_voltage",
                path=("charge", "chargeVoltage"),
                translation_key="charge_voltage",
                device_class="voltage",
                native_unit_of_measurement="V",
                state_class="measurement",
                converter=_absolute_number,
                sticky=True,
            ),
            SensorSpec(
                key="charge_current",
                path=("charge", "chargeCurrent"),
                translation_key="charge_current",
                device_class="current",
                native_unit_of_measurement="A",
                state_class="measurement",
                value_getter=_charge_current,
                sticky=True,
            ),
            SensorSpec(
                key="charge_power",
                path=("charge",),
                translation_key="charge_power",
                device_class="power",
                native_unit_of_measurement="kW",
                state_class="measurement",
                value_getter=_charge_power_kw,
                sticky=True,
            ),
            SensorSpec(
                key="remaining_charge_time",
                path=("charge", "remainChargeTime"),
                translation_key="remaining_charge_time",
                device_class="duration",
                native_unit_of_measurement="min",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="electric_wltc_remaining_mileage",
                path=("charge", "vcuWltcRemainingMileage"),
                translation_key="electric_wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="wltc_remaining_mileage",
                path=("vehicleStatus", "wltcRemainingMileage"),
                translation_key="wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="total_mileage",
                path=("vehicleStatus", "totalMileage"),
                translation_key="total_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="total_increasing",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="fuel_wltc_remaining_mileage",
                path=("fuel", "fuelWltcRemainingMileage"),
                translation_key="fuel_wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="fuel_remaining",
                path=("fuel", "leftPercent"),
                translation_key="fuel_remaining",
                native_unit_of_measurement="%",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="average_power_consumption",
                path=("energyReport", "total", "avgPowerConsum"),
                translation_key="average_power_consumption",
                source="energy_report",
                native_unit_of_measurement="kWh/100km",
                state_class="measurement",
            ),
            SensorSpec(
                key="average_fuel_consumption",
                path=("energyReport", "total", "avgFuelConsum"),
                translation_key="average_fuel_consumption",
                source="energy_report",
                native_unit_of_measurement="L/100km",
                state_class="measurement",
            ),
            SensorSpec(
                key="sum_remaining_mileage",
                path=("vehicleStatus", "sumRemainingMileage"),
                translation_key="sum_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="last_updated_at",
                path=("vehicleStatus", "lastUpdatedAt"),
                translation_key="last_updated_at",
                device_class="timestamp",
                converter=_epoch_millis,
            ),
            SensorSpec(
                key="last_online_at",
                path=("vehicleStatus", "lastOnlineAt"),
                translation_key="last_online_at",
                device_class="timestamp",
                converter=_epoch_millis,
            ),
            SensorSpec(
                key="charge_status",
                path=("charge", "chargeStatus"),
                translation_key="charge_status",
                value_getter=_charge_display_status_text,
            ),
            SensorSpec(
                key="vehicle_move_status",
                path=("vehicleStatus", "vehicleMoveStatus"),
                translation_key="vehicle_move_status",
                converter=_vehicle_move_status,
            ),
            SensorSpec(
                key="inside_temperature",
                path=("hvac", "insideTemp"),
                translation_key="inside_temperature",
                device_class="temperature",
                native_unit_of_measurement="°C",
                state_class="measurement",
                converter=_tenths,
                sticky=True,
            ),
            SensorSpec(
                key="air_conditioner_target_temperature",
                path=("hvac", "remoteTemp"),
                translation_key="air_conditioner_target_temperature",
                device_class="temperature",
                native_unit_of_measurement="°C",
                state_class="measurement",
                converter=_tenths,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_left_front",
                path=("tire", "leftFront", "pressure"),
                translation_key="tire_pressure_left_front",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_right_front",
                path=("tire", "rightFront", "pressure"),
                translation_key="tire_pressure_right_front",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_left_back",
                path=("tire", "leftBack", "pressure"),
                translation_key="tire_pressure_left_back",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_right_back",
                path=("tire", "rightBack", "pressure"),
                translation_key="tire_pressure_right_back",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
        ),
    ),
    VehicleSpec(
        key="seres_x1",
        enterprise_code="SERES",
        project_code="SERES-X1",
        supports_sentry_mode=True,
        supports_air_conditioner=True,
        supports_location=True,
        sensors=(
            SensorSpec(
                key="battery_soc",
                path=("charge", "soc"),
                translation_key="battery_soc",
                device_class="battery",
                native_unit_of_measurement="%",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="charge_voltage",
                path=("charge", "chargeVoltage"),
                translation_key="charge_voltage",
                device_class="voltage",
                native_unit_of_measurement="V",
                state_class="measurement",
                converter=_absolute_number,
                sticky=True,
            ),
            SensorSpec(
                key="charge_current",
                path=("charge", "chargeCurrent"),
                translation_key="charge_current",
                device_class="current",
                native_unit_of_measurement="A",
                state_class="measurement",
                value_getter=_charge_current,
                sticky=True,
            ),
            SensorSpec(
                key="charge_power",
                path=("charge",),
                translation_key="charge_power",
                device_class="power",
                native_unit_of_measurement="kW",
                state_class="measurement",
                value_getter=_charge_power_kw,
                sticky=True,
            ),
            SensorSpec(
                key="remaining_charge_time",
                path=("charge", "remainChargeTime"),
                translation_key="remaining_charge_time",
                device_class="duration",
                native_unit_of_measurement="min",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="electric_wltc_remaining_mileage",
                path=("charge", "vcuWltcRemainingMileage"),
                translation_key="electric_wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="wltc_remaining_mileage",
                path=("vehicleStatus", "wltcRemainingMileage"),
                translation_key="wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="total_mileage",
                path=("vehicleStatus", "totalMileage"),
                translation_key="total_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="total_increasing",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="fuel_wltc_remaining_mileage",
                path=("fuel", "fuelWltcRemainingMileage"),
                translation_key="fuel_wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="fuel_remaining",
                path=("fuel", "leftPercent"),
                translation_key="fuel_remaining",
                native_unit_of_measurement="%",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="average_power_consumption",
                path=("energyReport", "total", "avgPowerConsum"),
                translation_key="average_power_consumption",
                source="energy_report",
                native_unit_of_measurement="kWh/100km",
                state_class="measurement",
            ),
            SensorSpec(
                key="average_fuel_consumption",
                path=("energyReport", "total", "avgFuelConsum"),
                translation_key="average_fuel_consumption",
                source="energy_report",
                native_unit_of_measurement="L/100km",
                state_class="measurement",
            ),
            SensorSpec(
                key="sum_remaining_mileage",
                path=("vehicleStatus", "sumRemainingMileage"),
                translation_key="sum_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="last_updated_at",
                path=("vehicleStatus", "lastUpdatedAt"),
                translation_key="last_updated_at",
                device_class="timestamp",
                converter=_epoch_millis,
            ),
            SensorSpec(
                key="last_online_at",
                path=("vehicleStatus", "lastOnlineAt"),
                translation_key="last_online_at",
                device_class="timestamp",
                converter=_epoch_millis,
            ),
            SensorSpec(
                key="charge_status",
                path=("charge", "chargeStatus"),
                translation_key="charge_status",
                value_getter=_charge_display_status_text,
            ),
            SensorSpec(
                key="vehicle_move_status",
                path=("vehicleStatus", "vehicleMoveStatus"),
                translation_key="vehicle_move_status",
                converter=_vehicle_move_status,
            ),
            SensorSpec(
                key="inside_temperature",
                path=("hvac", "insideTemp"),
                translation_key="inside_temperature",
                device_class="temperature",
                native_unit_of_measurement="°C",
                state_class="measurement",
                converter=_tenths,
                sticky=True,
            ),
            SensorSpec(
                key="air_conditioner_target_temperature",
                path=("hvac", "remoteTemp"),
                translation_key="air_conditioner_target_temperature",
                device_class="temperature",
                native_unit_of_measurement="°C",
                state_class="measurement",
                converter=_tenths,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_left_front",
                path=("tire", "leftFront", "pressure"),
                translation_key="tire_pressure_left_front",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_right_front",
                path=("tire", "rightFront", "pressure"),
                translation_key="tire_pressure_right_front",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_left_back",
                path=("tire", "leftBack", "pressure"),
                translation_key="tire_pressure_left_back",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_right_back",
                path=("tire", "rightBack", "pressure"),
                translation_key="tire_pressure_right_back",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
        ),
    ),
# LUXEED R7 (智界 R7, made by Chery) — the enterprise code CHERY was
# confirmed from a live login (vehicle_auth returns tokens with
# enterpriseCode=CHERY). The project code is not yet known because OMP
# vehicle/management/list returns an empty list for CHERY, so it is left
# blank to match any CHERY vehicle. Tighten it once the real project code
# is captured (e.g. by capturing the official app's traffic).
    VehicleSpec(
        key="luxeed_r7",
        enterprise_code="CHERY",
        project_code="",
        supports_sentry_mode=True,
        supports_air_conditioner=True,
        supports_location=True,
        sensors=(
            SensorSpec(
                key="battery_soc",
                path=("charge", "soc"),
                translation_key="battery_soc",
                device_class="battery",
                native_unit_of_measurement="%",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="charge_voltage",
                path=("charge", "chargeVoltage"),
                translation_key="charge_voltage",
                device_class="voltage",
                native_unit_of_measurement="V",
                state_class="measurement",
                converter=_absolute_number,
                sticky=True,
            ),
            SensorSpec(
                key="charge_current",
                path=("charge", "chargeCurrent"),
                translation_key="charge_current",
                device_class="current",
                native_unit_of_measurement="A",
                state_class="measurement",
                value_getter=_charge_current,
                sticky=True,
            ),
            SensorSpec(
                key="charge_power",
                path=("charge",),
                translation_key="charge_power",
                device_class="power",
                native_unit_of_measurement="kW",
                state_class="measurement",
                value_getter=_charge_power_kw,
                sticky=True,
            ),
            SensorSpec(
                key="remaining_charge_time",
                path=("charge", "remainChargeTime"),
                translation_key="remaining_charge_time",
                device_class="duration",
                native_unit_of_measurement="min",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="electric_wltc_remaining_mileage",
                path=("charge", "vcuWltcRemainingMileage"),
                translation_key="electric_wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="wltc_remaining_mileage",
                path=("vehicleStatus", "wltcRemainingMileage"),
                translation_key="wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="total_mileage",
                path=("vehicleStatus", "totalMileage"),
                translation_key="total_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="total_increasing",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="fuel_wltc_remaining_mileage",
                path=("fuel", "fuelWltcRemainingMileage"),
                translation_key="fuel_wltc_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="fuel_remaining",
                path=("fuel", "leftPercent"),
                translation_key="fuel_remaining",
                native_unit_of_measurement="%",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="average_power_consumption",
                path=("energyReport", "total", "avgPowerConsum"),
                translation_key="average_power_consumption",
                source="energy_report",
                native_unit_of_measurement="kWh/100km",
                state_class="measurement",
            ),
            SensorSpec(
                key="average_fuel_consumption",
                path=("energyReport", "total", "avgFuelConsum"),
                translation_key="average_fuel_consumption",
                source="energy_report",
                native_unit_of_measurement="L/100km",
                state_class="measurement",
            ),
            SensorSpec(
                key="sum_remaining_mileage",
                path=("vehicleStatus", "sumRemainingMileage"),
                translation_key="sum_remaining_mileage",
                device_class="distance",
                native_unit_of_measurement="km",
                state_class="measurement",
                converter=_reject_sentinel,
                sticky=True,
            ),
            SensorSpec(
                key="last_updated_at",
                path=("vehicleStatus", "lastUpdatedAt"),
                translation_key="last_updated_at",
                device_class="timestamp",
                converter=_epoch_millis,
            ),
            SensorSpec(
                key="last_online_at",
                path=("vehicleStatus", "lastOnlineAt"),
                translation_key="last_online_at",
                device_class="timestamp",
                converter=_epoch_millis,
            ),
            SensorSpec(
                key="charge_status",
                path=("charge", "chargeStatus"),
                translation_key="charge_status",
                value_getter=_charge_display_status_text,
            ),
            SensorSpec(
                key="vehicle_move_status",
                path=("vehicleStatus", "vehicleMoveStatus"),
                translation_key="vehicle_move_status",
                converter=_vehicle_move_status,
            ),
            SensorSpec(
                key="inside_temperature",
                path=("hvac", "insideTemp"),
                translation_key="inside_temperature",
                device_class="temperature",
                native_unit_of_measurement="°C",
                state_class="measurement",
                converter=_tenths,
                sticky=True,
            ),
            SensorSpec(
                key="air_conditioner_target_temperature",
                path=("hvac", "remoteTemp"),
                translation_key="air_conditioner_target_temperature",
                device_class="temperature",
                native_unit_of_measurement="°C",
                state_class="measurement",
                converter=_tenths,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_left_front",
                path=("tire", "leftFront", "pressure"),
                translation_key="tire_pressure_left_front",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_right_front",
                path=("tire", "rightFront", "pressure"),
                translation_key="tire_pressure_right_front",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_left_back",
                path=("tire", "leftBack", "pressure"),
                translation_key="tire_pressure_left_back",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
            SensorSpec(
                key="tire_pressure_right_back",
                path=("tire", "rightBack", "pressure"),
                translation_key="tire_pressure_right_back",
                device_class="pressure",
                native_unit_of_measurement="bar",
                state_class="measurement",
                converter=_positive_number,
                sticky=True,
            ),
        ),
    ),
)


def vehicle_spec_for(vehicle: Vehicle) -> VehicleSpec | None:
    return next((spec for spec in DEVICES if spec.matches(vehicle)), None)


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
    if spec.value_getter is not None:
        return spec.value_getter(data)
    value = value_at_path(data, spec.path)
    return spec.converter(value) if spec.converter is not None else value
