"""Reviewed OMP manufacturer endpoint declarations.

This module intentionally contains a finite table, not a provider framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from ..const import SERES_APIG_ORIGIN
from .contracts import CredentialPurpose, OmpOperation, OmpRequestContract

class VehicleProfileSource(StrEnum):
    """The two reviewed sources of static vehicle profile information."""

    OMP_MANAGEMENT_LIST = "omp_management_list"
    GATEWAY_VEHICLE_INFO = "gateway_vehicle_info"


@dataclass(frozen=True)
class OmpManufacturerEndpoint:
    """One manufacturer's fully reviewed OMP/APIG route declaration.

    Each declaration is bound to an explicit enterprise authorization record;
    no endpoint is selected by probing enterprise codes or by fallback.
    """

    endpoint_id: str
    enterprise_code: str
    gateway_origin: str
    contracts: Mapping[OmpOperation, OmpRequestContract]
    allowed_spec_ids: frozenset[str]
    profile_source: VehicleProfileSource

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

    def supports(self, operation: OmpOperation) -> bool:
        return operation in self.contracts

_APIG_HEADERS = frozenset({"Authorization", "X-Nonce", "X-Created", "X-Device-Id"})
_VEHICLE_APIG_HEADERS = _APIG_HEADERS | {"X-Vehicle-Id"}


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
    profile_source=VehicleProfileSource.OMP_MANAGEMENT_LIST,
)


def _normal_gateway_contracts(endpoint_id: str) -> Mapping[OmpOperation, OmpRequestContract]:
    """Static HIMA VCAM/VCTRL routes used for non-protected controls only."""
    evidence = "2026-08-hima-static"
    return {
        OmpOperation.VEHICLE_LIST: OmpRequestContract(
            f"{endpoint_id}.vehicles.v1", evidence, "GET", "/vcam/v1/accounts/vehicles",
            _APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.VEHICLE_PROFILE: OmpRequestContract(
            f"{endpoint_id}.vehicle-info.v1", evidence, "GET", "/vcam/v1/accounts/vehicle-infos/{{vehicle_id}}",
            _APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.DYNAMIC_INFOS: OmpRequestContract(
            f"{endpoint_id}.dynamic-infos.v2", evidence, "POST", "/vctrl/v2/controls/dynamic-infos",
            _VEHICLE_APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.COMMAND_STATUS: OmpRequestContract(
            f"{endpoint_id}.command-status.v2", evidence, "GET", "/vctrl/v2/controls/commands/{{command_id}}",
            _VEHICLE_APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.AIR_CONDITIONER: OmpRequestContract(
            f"{endpoint_id}.air-conditioner.v1", evidence, "POST", "/vctrl/v1/controls/air-conditioner",
            _VEHICLE_APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.RAPID_AIR_CONDITIONER: OmpRequestContract(
            f"{endpoint_id}.air-conditioner.rapid.v1", evidence, "POST", "/vctrl/v1/controls/air-conditioner/rapid",
            _VEHICLE_APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.DEFROST: OmpRequestContract(
            f"{endpoint_id}.hvac.v1", evidence, "POST", "/vctrl/v1/controls/hvac",
            _VEHICLE_APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.DEPARTURE_PLAN: OmpRequestContract(
            f"{endpoint_id}.departure-plan.v1", evidence, "POST", "/vctrl/v1/controls/departure-plans/now/0",
            _VEHICLE_APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
        OmpOperation.SENTRY_MODE: OmpRequestContract(
            f"{endpoint_id}.sentry.v1", evidence, "POST", "/vctrl/v1/controls/sentry",
            _VEHICLE_APIG_HEADERS, CredentialPurpose.VEHICLE_AUTHORIZATION,
        ),
    }


CHERY_OMP = OmpManufacturerEndpoint(
    endpoint_id="chery_omp",
    enterprise_code="CHERY",
    gateway_origin="https://apir.chssatsp.icvcs.com",
    contracts=_normal_gateway_contracts("chery.apir"),
    allowed_spec_ids=frozenset(),
    profile_source=VehicleProfileSource.GATEWAY_VEHICLE_INFO,
)

BAIC_OMP = OmpManufacturerEndpoint(
    endpoint_id="baic_omp",
    enterprise_code="BAIC",
    gateway_origin="https://apir.bjevssa.icvcs.com",
    contracts=_normal_gateway_contracts("baic.apir"),
    allowed_spec_ids=frozenset(),
    profile_source=VehicleProfileSource.GATEWAY_VEHICLE_INFO,
)

JAC_OMP = OmpManufacturerEndpoint(
    endpoint_id="jac_omp",
    enterprise_code="JAC",
    gateway_origin="https://apir.jacssa.icvcs.com",
    contracts=_normal_gateway_contracts("jac.apir"),
    allowed_spec_ids=frozenset(),
    profile_source=VehicleProfileSource.GATEWAY_VEHICLE_INFO,
)

SAIC_OMP = OmpManufacturerEndpoint(
    endpoint_id="saic_omp",
    enterprise_code="SAIC",
    gateway_origin="https://api-app.srih.icvcs.com",
    contracts=_normal_gateway_contracts("saic.apir"),
    allowed_spec_ids=frozenset(),
    profile_source=VehicleProfileSource.GATEWAY_VEHICLE_INFO,
)

ENDPOINTS: Mapping[str, OmpManufacturerEndpoint] = {
    endpoint.endpoint_id: endpoint
    for endpoint in (SERES_OMP, CHERY_OMP, BAIC_OMP, JAC_OMP, SAIC_OMP)
}


def endpoint_for_id(endpoint_id: str) -> OmpManufacturerEndpoint:
    try:
        return ENDPOINTS[endpoint_id]
    except KeyError as error:
        raise ValueError(f"unknown manufacturer endpoint {endpoint_id}") from error
