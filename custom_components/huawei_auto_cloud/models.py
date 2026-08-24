"""Pure data models for route-scoped vehicle access."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from .const import DOMAIN
from .omp.contracts import CredentialPurpose, OmpRequestContract


@dataclass(frozen=True)
class AccountSession:
    """Account credentials shared by all enterprise authorization scopes."""

    account_generation: int
    access_token: str
    refresh_token: str
    xid: str
    omp_user_id: str
    omp_device_id: str
    ivcs_device_id: str
    native_device_model: str
    session_context: str


@dataclass(frozen=True)
class EnterpriseSession:
    """Authorization material for exactly one verified enterprise scope."""

    session_id: str
    endpoint_id: str
    enterprise_code: str
    authorization: str
    generation: int
    expires_at: datetime | None = None


@dataclass(frozen=True)
class VehicleRoute:
    """The persistent identity used by all integration-facing vehicle code."""

    route_id: str
    vehicle_id: str
    endpoint_id: str
    session_id: str
    enterprise_code: str
    spec_id: str


@dataclass(frozen=True)
class OmpRequestContext:
    """Immutable, runtime-only authorization and request routing snapshot."""

    route_id: str
    vehicle_id: str
    endpoint_id: str
    enterprise_code: str
    gateway_origin: str
    authorization: str
    ivcs_device_id: str
    account_generation: int
    session_id: str
    session_generation: int
    contract: OmpRequestContract

    def require(self, purpose: CredentialPurpose) -> None:
        if self.contract.credential_purpose != purpose:
            raise ValueError(f"request contract {self.contract.contract_id} has the wrong credential purpose")
        if not self.authorization:
            raise ValueError("request context is missing enterprise authorization")


@dataclass(frozen=True)
class OmpDiscoveryContext:
    """Runtime-only enterprise context used before a vehicle route exists."""

    endpoint_id: str
    enterprise_code: str
    gateway_origin: str
    authorization: str
    ivcs_device_id: str
    account_generation: int
    session_id: str
    session_generation: int
    contract: OmpRequestContract

    def require(self, purpose: CredentialPurpose) -> None:
        if self.contract.credential_purpose != purpose or not self.authorization:
            raise ValueError("discovery context is not authorized for this request")


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


@dataclass(frozen=True)
class VehicleProfile:
    model_code: str | None = None
    model_name: str | None = None
    enterprise_code: str | None = None
    project_code: str | None = None
    flags: Mapping[str, Any] = field(default_factory=dict)
    features: tuple[VehicleFeature, ...] = ()

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "VehicleProfile":
        stored = data.get("profile")
        source = stored if isinstance(stored, Mapping) else data
        base = source if isinstance(stored, Mapping) else _vehicle_base_info(data)
        features = tuple(
            VehicleFeature.from_api(item)
            for item in source.get("vehicleFeatures", ())
            if isinstance(item, Mapping)
        )
        flags = {
            key: value
            for key, value in base.items()
            if key.startswith("isSupport")
            or key in {"supportVent", "supportHotSeats", "supportVentSeats", "minACTemp", "maxACTemp"}
        }
        return cls(
            model_code=_string_value(base.get("modelCode")),
            model_name=_string_value(base.get("modelName")),
            enterprise_code=_string_value(base.get("enterpriseCode")),
            project_code=_string_value(base.get("projectCode")),
            flags=flags,
            features=features,
        )

    def as_storage(self) -> dict[str, Any]:
        return {
            "modelCode": self.model_code,
            "modelName": self.model_name,
            "enterpriseCode": self.enterprise_code,
            "projectCode": self.project_code,
            **dict(self.flags),
            "vehicleFeatures": [feature.as_storage() for feature in self.features],
        }


@dataclass(frozen=True)
class Vehicle:
    id: str
    name: str
    vin: str | None = None
    model: str | None = None
    sw_version: str | None = None
    profile: VehicleProfile = field(default_factory=VehicleProfile)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Vehicle":
        source = _vehicle_base_info(data)
        vehicle_id = str(source.get("vehicleIdStr") or source.get("vehicleId") or source.get("id") or "")
        profile = VehicleProfile.from_api(data)
        model = profile.model_name or _string_value(source.get("modelName"))
        name = source.get("aliasName") or profile.model_name or source.get("vehicleName") or model or _fallback_name(vehicle_id)
        return cls(
            id=vehicle_id,
            name=str(name),
            vin=_string_value(source.get("vin") or source.get("vinCode")),
            model=model,
            sw_version=firmware_sw_version(data) or firmware_sw_version(source),
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


def vehicle_device_info(vehicle: Vehicle, route: VehicleRoute) -> dict[str, Any]:
    info: dict[str, Any] = {
        "identifiers": {(DOMAIN, route.route_id)},
        "name": vehicle.name,
        "manufacturer": {"SERES": "赛力斯"}.get(route.enterprise_code, route.enterprise_code),
    }
    if vehicle.model:
        info["model"] = vehicle.model
    if vehicle.sw_version:
        info["sw_version"] = vehicle.sw_version
    return info


def vehicle_merge_items(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, Mapping):
        return []
    items = response.get("vehicleMargeInfoList")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def vehicle_resource_manifest(data: Mapping[str, Any]) -> dict[str, str | None] | None:
    """Extract a resource declaration from raw data or a merged vehicle profile.

    During first discovery we merge the APIG vehicle-list item with an OMP
    profile. The raw OMP item keeps ``vehicleResourceInfo`` at the top level,
    while the normalized profile is stored under ``profile``. Prefer the raw
    top-level declaration so that adding the normalized profile never masks a
    real resource archive.
    """
    resource = data.get("vehicleResourceInfo")
    if not isinstance(resource, Mapping):
        profile = data.get("profile")
        resource = profile.get("vehicleResourceInfo") if isinstance(profile, Mapping) else None
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


def resource_manifests_from_profile_responses(
    profile_responses: Mapping[str, Any],
    vehicle_ids: Iterable[str],
) -> dict[str, dict[str, str | None]]:
    """Recover resource manifests from raw OMP profile responses.

    Raw profile responses are retained in the account asset precisely so an
    interrupted first-time resource download can be resumed without another
    login or vehicle-profile request. Only IDs belonging to the saved routes
    are returned.
    """
    known_vehicle_ids = set(vehicle_ids)
    manifests: dict[str, dict[str, str | None]] = {}
    for response in profile_responses.values():
        for item in vehicle_merge_items(response):
            vehicle = Vehicle.from_api(item)
            if vehicle.id not in known_vehicle_ids:
                continue
            if manifest := vehicle_resource_manifest(item):
                manifests[vehicle.id] = manifest
    return manifests


def firmware_sw_version(response: Any) -> str | None:
    if not isinstance(response, Mapping):
        return None
    return _string_value(response.get("swVersion") or response.get("softwareVersion") or response.get("prettyVersion") or response.get("version"))


def _vehicle_base_info(data: Mapping[str, Any]) -> Mapping[str, Any]:
    base = data.get("vehicleBaseInfo")
    return base if isinstance(base, Mapping) else data


def _string_value(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _fallback_name(vehicle_id: str) -> str:
    return f"Vehicle {vehicle_id[-6:]}" if vehicle_id else "Vehicle"
