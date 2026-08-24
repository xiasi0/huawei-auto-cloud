"""Reviewed OMP manufacturer endpoint declarations.

This module intentionally contains a finite table, not a provider framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, TYPE_CHECKING

from ..const import SERES_APIG_ORIGIN
from .contracts import CredentialPurpose, OmpOperation, OmpRequestContract

if TYPE_CHECKING:
    from ..models import AccountSession, EnterpriseSession
    from .client import OmpClient

ProfileFetcher = Callable[
    ["OmpClient", "AccountSession", "EnterpriseSession"],
    tuple[dict[str, dict[str, Any]], Any],
]


@dataclass(frozen=True)
class OmpManufacturerEndpoint:
    """One manufacturer's fully reviewed OMP/APIG route declaration.

    Add a new OMP manufacturer here only after its enterprise authorization,
    discovery, profile, and request contracts have real traffic fixtures.
    """

    endpoint_id: str
    enterprise_code: str
    gateway_origin: str
    contracts: Mapping[OmpOperation, OmpRequestContract]
    allowed_spec_ids: frozenset[str]
    profile_fetcher: ProfileFetcher

    def contract(self, operation: OmpOperation) -> OmpRequestContract:
        try:
            return self.contracts[operation]
        except KeyError as error:
            raise ValueError(f"{self.endpoint_id} does not support {operation}") from error

    def operation_for_contract(self, contract_id: str) -> OmpOperation:
        for operation, contract in self.contracts.items():
            if contract.contract_id == contract_id:
                return operation
        raise ValueError(f"{self.endpoint_id} does not own request contract {contract_id}")

    def fetch_profiles(
        self,
        client: "OmpClient",
        account: "AccountSession",
        session: "EnterpriseSession",
    ) -> tuple[dict[str, dict[str, Any]], Any]:
        """Fetch and parse this endpoint's verified vehicle-profile response."""
        return self.profile_fetcher(client, account, session)


_APIG_HEADERS = frozenset({"Authorization", "X-Nonce", "X-Created", "X-Device-Id"})
_VEHICLE_APIG_HEADERS = _APIG_HEADERS | {"X-Vehicle-Id"}


def _fetch_seres_profiles(
    client: "OmpClient",
    account: "AccountSession",
    session: "EnterpriseSession",
) -> tuple[dict[str, dict[str, Any]], Any]:
    """Fetch and parse SERES's verified OMP management-list response."""
    from ..models import Vehicle, vehicle_merge_items

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
            profile = vehicle.profile.as_storage()
            profiles[vehicle.id] = profile
    return profiles, response

SERES_OMP = OmpManufacturerEndpoint(
    endpoint_id="seres_omp",
    enterprise_code="SERES",
    gateway_origin=SERES_APIG_ORIGIN,
    contracts={
        OmpOperation.VEHICLE_LIST: OmpRequestContract(
            contract_id="seres.apig.vehicles.v1",
            evidence_version="2026-08-seres",
            method="GET",
            path_template="/vcam/v1/accounts/vehicles",
            required_header_names=_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.DYNAMIC_INFOS: OmpRequestContract(
            contract_id="seres.apig.dynamic-infos.v2",
            evidence_version="2026-08-seres",
            method="POST",
            path_template="/vctrl/v2/controls/dynamic-infos",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.ENERGY_REPORT: OmpRequestContract(
            contract_id="seres.apig.energy.latest.v1",
            evidence_version="2026-08-seres",
            method="GET",
            path_template="/vdas/v1/report/energy/latest",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.LOCATION: OmpRequestContract(
            contract_id="seres.apig.location.v1",
            evidence_version="2026-08-seres",
            method="GET",
            path_template="/vcam/v1/find-car/location",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.FIRMWARE: OmpRequestContract(
            contract_id="seres.apig.firmware.current.v1",
            evidence_version="2026-08-seres",
            method="GET",
            path_template="/vota/v1/firmware/current-version",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.COMMAND_STATUS: OmpRequestContract(
            contract_id="seres.apig.command-status.v2",
            evidence_version="2026-08-seres",
            method="GET",
            path_template="/vctrl/v2/controls/commands/{command_id}",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.AIR_CONDITIONER: OmpRequestContract(
            contract_id="seres.apig.air-conditioner.v1",
            evidence_version="2026-08-seres",
            method="POST",
            path_template="/vctrl/v1/controls/air-conditioner",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.RAPID_AIR_CONDITIONER: OmpRequestContract(
            contract_id="seres.apig.air-conditioner.rapid.v1",
            evidence_version="2026-08-seres",
            method="POST",
            path_template="/vctrl/v1/controls/air-conditioner/rapid",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.DEFROST: OmpRequestContract(
            contract_id="seres.apig.hvac.v1",
            evidence_version="2026-08-seres",
            method="POST",
            path_template="/vctrl/v1/controls/hvac",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.DEPARTURE_PLAN: OmpRequestContract(
            contract_id="seres.apig.departure-plan.v1",
            evidence_version="2026-08-seres",
            method="POST",
            path_template="/vctrl/v1/controls/departure-plans/now/0",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.SENTRY_MODE: OmpRequestContract(
            contract_id="seres.apig.sentry.v1",
            evidence_version="2026-08-seres",
            method="POST",
            path_template="/vctrl/v1/controls/sentry",
            required_header_names=_VEHICLE_APIG_HEADERS,
            credential_purpose=CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
    },
    allowed_spec_ids=frozenset({"seres_f3", "seres_aito_a15", "seres_x1"}),
    profile_fetcher=_fetch_seres_profiles,
)

ENDPOINTS: Mapping[str, OmpManufacturerEndpoint] = {SERES_OMP.endpoint_id: SERES_OMP}


def endpoint_for_id(endpoint_id: str) -> OmpManufacturerEndpoint:
    try:
        return ENDPOINTS[endpoint_id]
    except KeyError as error:
        raise ValueError(f"unknown manufacturer endpoint {endpoint_id}") from error
