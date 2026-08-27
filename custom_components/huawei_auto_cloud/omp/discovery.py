"""Shared IVCS discovery with binding-selected list and profile sources."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Mapping, TYPE_CHECKING
from urllib.parse import quote

from ..models import VehicleDiscoveryContext, Vehicle, VehicleProfile, vehicle_merge_items, vehicle_resource_manifest
from ..vehicle_gateway import VehicleGatewayApiError, VehicleGatewayClient
from .client import OmpApiError
from .contracts import VehicleOperation
from .enterprises import DiscoverySource, IvcsBinding
from ..routing import vehicle_authorization

if TYPE_CHECKING:
    from ..models import AccountSession, VehicleGatewaySession
    from .client import OmpClient

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileFetchResult:
    """Normalized profiles and one raw response collection for persistence."""

    profiles: dict[str, dict[str, Any]]
    raw_response: Any
    resource_manifests: dict[str, dict[str, str | None]] = field(default_factory=dict)
    management_query_responses: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VehicleListResult:
    """Vehicle list items and the raw response used to obtain them."""

    items: list[dict[str, Any]]
    raw_response: Any


def fetch_vehicle_list(
    binding: IvcsBinding,
    omp_client: "OmpClient",
    gateway_client: VehicleGatewayClient,
    account: "AccountSession",
    session: "VehicleGatewaySession",
) -> VehicleListResult:
    """Fetch a vehicle list using the binding's declared discovery source."""
    source = binding.discovery_plan.vehicle_list_source
    if source is DiscoverySource.OMP_MANAGEMENT_LIST:
        response = omp_client.vehicle_management_list(
            xid=account.xid,
            device_id=account.omp_device_id,
            user_id=account.omp_user_id,
            enterprise_code=session.enterprise_code,
            native_device_model=account.native_device_model,
        )
        return VehicleListResult(vehicle_merge_items(response), response)
    if source is not DiscoverySource.IVCS_VCAM:
        raise ValueError(f"unsupported vehicle-list source for {binding.binding_id}")
    context = _gateway_discovery_context(binding, account, session, VehicleOperation.VEHICLE_LIST)
    response = gateway_client.request(context)
    items = response if isinstance(response, list) else response.get("data", []) if isinstance(response, Mapping) else []
    return VehicleListResult(
        [item for item in items if isinstance(item, dict)] if isinstance(items, list) else [],
        response,
    )


def fetch_profiles(
    binding: IvcsBinding,
    omp_client: "OmpClient",
    gateway_client: VehicleGatewayClient,
    account: "AccountSession",
    session: "VehicleGatewaySession",
    vehicle_items: list[dict[str, Any]],
    *,
    list_response: Any | None = None,
) -> ProfileFetchResult:
    """Read profiles using the binding's declared discovery source."""
    source = binding.discovery_plan.vehicle_profile_source
    if source is DiscoverySource.OMP_MANAGEMENT_LIST:
        return _fetch_omp_management_profiles(omp_client, account, session, list_response)
    if source is DiscoverySource.IVCS_VEHICLE_INFO:
        return _fetch_gateway_profiles(binding, omp_client, gateway_client, account, session, vehicle_items)
    raise ValueError(f"unsupported vehicle-profile source for {binding.binding_id}")


def _fetch_omp_management_profiles(
    client: OmpClient,
    account: "AccountSession",
    session: "VehicleGatewaySession",
    list_response: Any | None = None,
) -> ProfileFetchResult:
    response = list_response
    if response is None:
        response = client.vehicle_management_list(
            xid=account.xid,
            device_id=account.omp_device_id,
            user_id=account.omp_user_id,
            enterprise_code=session.enterprise_code,
            native_device_model=account.native_device_model,
        )
    profiles: dict[str, dict[str, Any]] = {}
    resource_manifests: dict[str, dict[str, str | None]] = {}
    for item in vehicle_merge_items(response):
        vehicle = Vehicle.from_api(item)
        if vehicle.id:
            profiles[vehicle.id] = vehicle.profile.as_storage()
            if manifest := vehicle_resource_manifest(item):
                resource_manifests[vehicle.id] = manifest
    return ProfileFetchResult(profiles, response, resource_manifests)


def _fetch_gateway_profiles(
    binding: IvcsBinding,
    omp_client: OmpClient,
    client: VehicleGatewayClient,
    account: "AccountSession",
    session: "VehicleGatewaySession",
    vehicle_items: list[dict[str, Any]],
) -> ProfileFetchResult:
    profiles: dict[str, dict[str, Any]] = {}
    responses: dict[str, Any] = {}
    resource_manifests: dict[str, dict[str, str | None]] = {}
    management_query_responses: dict[str, Any] = {}
    for item in vehicle_items:
        vehicle_id = str(item.get("vehicleIdStr") or item.get("vehicleId") or "")
        if not vehicle_id:
            continue
        try:
            context = _gateway_discovery_context(binding, account, session, VehicleOperation.VEHICLE_PROFILE)
            response = client.request(context, path_values={"vehicle_id": quote(vehicle_id, safe="")})
        except VehicleGatewayApiError as error:
            _LOGGER.warning(
                "Huawei Auto Cloud vehicle profile request failed: "
                "binding=%s contract=%s method=%s path=%s status=%s code=%s msg=%s",
                binding.binding_id,
                error.contract_id,
                error.method,
                error.path_template,
                error.status,
                error.response.get("code") if isinstance(error.response, Mapping) else None,
                error.response.get("msg") if isinstance(error.response, Mapping) else None,
            )
        else:
            responses[vehicle_id] = response
            if manifest := vehicle_resource_manifest(response):
                resource_manifests[vehicle_id] = manifest
            profile = _profile_from_gateway_response(response, session.enterprise_code)
            if profile is not None:
                profiles[vehicle_id] = profile

        try:
            response = omp_client.vehicle_management_query(
                vehicle_id=vehicle_id,
                xid=account.xid,
                device_id=account.omp_device_id,
                user_id=account.omp_user_id,
                enterprise_code=session.enterprise_code,
                native_device_model=account.native_device_model,
            )
        except (OmpApiError, OSError) as error:
            response = error.response if isinstance(error, OmpApiError) else None
            _LOGGER.warning(
                "Huawei Auto Cloud vehicle management query failed: "
                "binding=%s method=POST path=/xcar/omp/xbs/vehicle/management/query status=%s code=%s msg=%s error=%s",
                binding.binding_id,
                error.status if isinstance(error, OmpApiError) else None,
                response.get("code") if isinstance(response, Mapping) else None,
                response.get("msg") if isinstance(response, Mapping) else None,
                type(error).__name__,
            )
        else:
            management_query_responses[vehicle_id] = response
            if manifest := vehicle_resource_manifest(response):
                resource_manifests[vehicle_id] = manifest
    return ProfileFetchResult(profiles, responses, resource_manifests, management_query_responses)


def _gateway_discovery_context(
    binding: IvcsBinding,
    account: "AccountSession",
    session: "VehicleGatewaySession",
    operation: VehicleOperation,
) -> VehicleDiscoveryContext:
    return VehicleDiscoveryContext(
        binding_id=binding.binding_id,
        enterprise_code=binding.enterprise_code,
        gateway_origin=binding.gateway_origin,
        authorization=vehicle_authorization(session),
        ivcs_device_id=account.ivcs_device_id,
        account_generation=account.account_generation,
        session_id=session.session_id,
        session_generation=session.generation,
        contract=binding.contract(operation),
    )


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
