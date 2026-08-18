from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .const import DOMAIN

_MANUFACTURER_NAMES = {"SERES": "赛力斯", "CHERY": "奇瑞"}


@dataclass(frozen=True)
class Vehicle:
    id: str
    name: str
    vin: str | None = None
    model: str | None = None
    sw_version: str | None = None
    profile: "VehicleProfile" = field(default_factory=lambda: VehicleProfile())

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Vehicle":
        source = _vehicle_base_info(data)
        vehicle_id = str(source.get("vehicleIdStr") or source.get("vehicleId") or source.get("id") or "")
        vin = source.get("vin") or source.get("vinCode")
        profile = VehicleProfile.from_api(data)
        model = profile.model_name or source.get("modelName") or source.get("vehicleModel") or source.get("seriesName")
        plate = source.get("licensePlate") or source.get("plateNo")
        sw_version = firmware_sw_version(data) or firmware_sw_version(source)
        name = (
            source.get("aliasName")
            or profile.model_name
            or source.get("vehicleName")
            or source.get("nickname")
            or model
            or (str(plate) if plate else None)
            or _fallback_name(vehicle_id)
        )
        return cls(
            id=vehicle_id,
            name=str(name),
            vin=str(vin) if vin else None,
            model=str(model) if model else None,
            sw_version=str(sw_version) if sw_version else None,
            profile=profile,
        )

    def as_storage(self) -> dict[str, Any]:
        return {
            "vehicleIdStr": self.id,
            "vehicleName": self.name,
            "vin": self.vin,
            "modelName": self.model,
            "swVersion": self.sw_version,
            "profile": self.profile.as_storage(),
        }


@dataclass(frozen=True)
class VehicleProfile:
    """Non-sensitive static metadata used for device info and capability gating."""

    model_code: str | None = None
    model_name: str | None = None
    enterprise_code: str | None = None
    project_code: str | None = None
    full_material_no: str | None = None
    power_type: str | None = None
    configuration_updated_at: str | None = None
    platform_version: str | None = None
    flags: Mapping[str, Any] = field(default_factory=dict)
    features: tuple["VehicleFeature", ...] = ()

    @property
    def is_resolved(self) -> bool:
        """Whether this is a model profile, not the legacy APIG vehicle stub."""
        return bool(self.model_code and self.project_code)

    def flag_is_true(self, key: str) -> bool:
        return _feature_flag(self.flags.get(key)) is True

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "VehicleProfile":
        stored = data.get("profile")
        if isinstance(stored, Mapping):
            source = stored
            base = stored
        else:
            source = data
            base = _vehicle_base_info(data)

        feature_items = source.get("vehicleFeatures")
        features = tuple(
            VehicleFeature.from_api(item)
            for item in (feature_items if isinstance(feature_items, list) else ())
            if isinstance(item, Mapping)
        )
        flags = {
            key: value
            for key, value in base.items()
            if key.startswith("isSupport")
            or key in {
                "supportVent",
                "supportHotSeats",
                "supportVentSeats",
                "minACTemp",
                "maxACTemp",
                "frontTrunkSupportType",
                "secondSeatSupportHeatType",
            }
        }
        flags.update(_feature_capabilities(features))
        return cls(
            model_code=_string_value(base.get("modelCode")),
            model_name=_string_value(base.get("modelName")),
            enterprise_code=_string_value(base.get("enterpriseCode")),
            project_code=_string_value(base.get("projectCode")),
            full_material_no=_string_value(base.get("fullMaterialNo")),
            power_type=_string_value(base.get("powerType")),
            configuration_updated_at=_string_value(base.get("vehConfigUpdateTime")),
            platform_version=_string_value(base.get("vehPlatformVersion")),
            flags=flags,
            features=features,
        )

    def as_storage(self) -> dict[str, Any]:
        return {
            "modelCode": self.model_code,
            "modelName": self.model_name,
            "enterpriseCode": self.enterprise_code,
            "projectCode": self.project_code,
            "fullMaterialNo": self.full_material_no,
            "powerType": self.power_type,
            "vehConfigUpdateTime": self.configuration_updated_at,
            "vehPlatformVersion": self.platform_version,
            **dict(self.flags or {}),
            "vehicleFeatures": [feature.as_storage() for feature in self.features],
        }


@dataclass(frozen=True)
class VehicleFeature:
    code: str
    value: str | None = None
    match_type: str | None = None
    ota_version: str | None = None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "VehicleFeature":
        return cls(
            code=str(data.get("featureCode") or ""),
            value=_string_value(data.get("featureValue")),
            match_type=_string_value(data.get("matchType")),
            ota_version=_string_value(data.get("carOtaVersion")),
        )

    def as_storage(self) -> dict[str, str | None]:
        return {
            "featureCode": self.code,
            "featureValue": self.value,
            "matchType": self.match_type,
            "carOtaVersion": self.ota_version,
        }


def _feature_capabilities(features: tuple[VehicleFeature, ...]) -> dict[str, Any]:
    """Apply only the feature conversions used by the official static model."""
    boolean_features = {
        "ANION": "isSupportAirClean",
        "FRAGRANCE": "isSupportFragrance",
        "INDOOR_PM25": "isSupportIndoorPm25",
        "WARM_COOLING_BOX": "isSupportWarmCoolingBox",
        "E_MIRROR": "isSupportEMirror",
        "OVERHEAT_PROTECT": "isSupportOverHeatProtect",
        "SLE": "isSupportSle",
        "HI_SCENARIO": "isSupportHiScenario",
    }
    capabilities: dict[str, Any] = {}
    for feature in features:
        if target := boolean_features.get(feature.code):
            capabilities[target] = feature.value == "1"
        elif feature.code == "VENTILATION":
            capabilities["supportVent"] = feature.value
        elif feature.code == "SEAT_HEAT":
            capabilities["secondSeatSupportHeatType"] = feature.value
        elif feature.code == "FRONT_TRUNK":
            capabilities["frontTrunkSupportType"] = {
                "1": "Status_Only",
                "2": "Open_And_Status",
                "3": "Open_Close_Status",
            }.get(feature.value)
    return capabilities


def _feature_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"1", "true", "yes"}:
            return True
        if value in {"0", "false", "no"}:
            return False
    return None


def vehicle_device_info(vehicle: Vehicle) -> dict[str, Any]:
    info: dict[str, Any] = {
        "identifiers": {(DOMAIN, vehicle.id)},
        "name": vehicle.name,
        "manufacturer": _MANUFACTURER_NAMES.get(vehicle.profile.enterprise_code, "赛力斯"),
    }
    if vehicle.model:
        info["model"] = vehicle.model
    if vehicle.sw_version:
        info["sw_version"] = vehicle.sw_version
    return info


def firmware_sw_version(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    version = response.get("swVersion") or response.get("softwareVersion") or response.get("prettyVersion") or response.get("version")
    return str(version) if version else None


def vehicle_merge_items(response: Any) -> list[dict[str, Any]]:
    """Return the official top-level OMP vehicle profile list."""
    if not isinstance(response, Mapping):
        return []
    items = response.get("vehicleMargeInfoList")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def vehicle_resource_manifest(data: Mapping[str, Any]) -> dict[str, str | None] | None:
    resource = data.get("vehicleResourceInfo")
    if not isinstance(resource, Mapping):
        return None
    resource_file = _string_value(resource.get("resourceFile"))
    resource_sign = _string_value(resource.get("vehicleResourceSign"))
    if not resource_file or not resource_sign:
        return None
    return {
        "resourceFile": resource_file,
        "resourceSign": resource_sign,
        "resourceVersion": _string_value(resource.get("resourceVersion")),
        "versionName": _string_value(resource.get("versionName")),
    }


def _vehicle_base_info(data: Mapping[str, Any]) -> Mapping[str, Any]:
    base = data.get("vehicleBaseInfo")
    return base if isinstance(base, Mapping) else data


def _string_value(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _fallback_name(vehicle_id: str) -> str:
    return f"AITO {vehicle_id[-6:]}" if vehicle_id else "AITO"
