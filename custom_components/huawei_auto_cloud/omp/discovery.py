"""Fixed vehicle-profile discovery paths for reviewed manufacturer endpoints."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Mapping, TYPE_CHECKING
from urllib.parse import quote

from ..models import OmpDiscoveryContext, Vehicle, VehicleProfile, vehicle_merge_items
from .client import OmpApiError
from .contracts import OmpOperation
from .enterprises import OmpManufacturerEndpoint, VehicleProfileSource

if TYPE_CHECKING:
    from ..models import AccountSession, EnterpriseSession
    from .client import OmpClient

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileFetchResult:
    """Normalized profiles and one raw response collection for persistence."""

    profiles: dict[str, dict[str, Any]]
    raw_response: Any


def fetch_profiles(
    endpoint: OmpManufacturerEndpoint,
    client: "OmpClient",
    account: "AccountSession",
    session: "EnterpriseSession",
    vehicle_items: list[dict[str, Any]],
) -> ProfileFetchResult:
    """Read profiles using the endpoint's fixed, declared source."""
    if endpoint.profile_source is VehicleProfileSource.OMP_MANAGEMENT_LIST:
        return _fetch_omp_management_profiles(client, account, session)
    if endpoint.profile_source is VehicleProfileSource.GATEWAY_VEHICLE_INFO:
        return _fetch_gateway_profiles(endpoint, client, account, session, vehicle_items)
    raise ValueError(f"unknown profile source for {endpoint.endpoint_id}")


def _fetch_omp_management_profiles(
    client: "OmpClient",
    account: "AccountSession",
    session: "EnterpriseSession",
) -> ProfileFetchResult:
    response = client.vehicle_management_list(
        xid=account.xid,
        device_id=account.omp_device_id,
        user_id=account.omp_user_id,
        enterprise_code=session.enterprise_code,
        native_device_model=account.native_device_model,
    )
    profiles: dict[str, dict[str, Any]] = {}
    for item in vehicle_merge_items(response):
        vehicle = Vehicle.from_api(item)
        if vehicle.id:
            profiles[vehicle.id] = vehicle.profile.as_storage()
    return ProfileFetchResult(profiles, response)


def _fetch_gateway_profiles(
    endpoint: OmpManufacturerEndpoint,
    client: "OmpClient",
    account: "AccountSession",
    session: "EnterpriseSession",
    vehicle_items: list[dict[str, Any]],
) -> ProfileFetchResult:
    context = OmpDiscoveryContext(
        endpoint_id=endpoint.endpoint_id,
        enterprise_code=endpoint.enterprise_code,
        gateway_origin=endpoint.gateway_origin,
        authorization=session.authorization,
        ivcs_device_id=account.ivcs_device_id,
        account_generation=account.account_generation,
        session_id=session.session_id,
        session_generation=session.generation,
        contract=endpoint.contract(OmpOperation.VEHICLE_PROFILE),
    )
    profiles: dict[str, dict[str, Any]] = {}
    responses: dict[str, Any] = {}
    for item in vehicle_items:
        vehicle_id = str(item.get("vehicleIdStr") or item.get("vehicleId") or "")
        if not vehicle_id:
            continue
        try:
            response = client.request(context, path_values={"vehicle_id": quote(vehicle_id, safe="")})
        except OmpApiError as error:
            _LOGGER.warning(
                "Huawei Auto Cloud vehicle profile request failed: endpoint=%s status=%s",
                endpoint.endpoint_id,
                error.status,
            )
            continue
        responses[vehicle_id] = response
        profile = _profile_from_gateway_response(response, session.enterprise_code)
        if profile is not None:
            profiles[vehicle_id] = profile
    return ProfileFetchResult(profiles, responses)


def _profile_from_gateway_response(response: Any, enterprise_code: str) -> dict[str, Any] | None:
    """Project only documented profile fields and retain unknown payloads raw."""
    candidate = response
    while isinstance(candidate, Mapping):
        nested = next(
            (candidate.get(key) for key in ("data", "result", "vehicleInfo") if isinstance(candidate.get(key), Mapping)),
            None,
        )
        if nested is None:
            break
        candidate = nested
    if not isinstance(candidate, Mapping):
        return None
    profile = VehicleProfile.from_api(candidate).as_storage()
    if not any(profile.get(key) for key in ("modelCode", "modelName", "projectCode")):
        return None
    profile["enterpriseCode"] = profile.get("enterpriseCode") or enterprise_code
    return profile
