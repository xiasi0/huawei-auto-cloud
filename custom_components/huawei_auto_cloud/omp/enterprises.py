"""Static IVCS vehicle bindings and discovery plans.

The five HIMA brands use one IVCS namespace. Brand differences are data in
this finite table, not separate clients or provider implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .contracts import CredentialPurpose, VehicleOperation, VehicleRequestContract

class DiscoverySource(StrEnum):
    """A reviewed source for one part of vehicle discovery."""

    IVCS_VCAM = "ivcs_vcam"
    IVCS_VEHICLE_INFO = "ivcs_vehicle_info"
    OMP_MANAGEMENT_LIST = "omp_management_list"


@dataclass(frozen=True)
class DiscoveryPlan:
    """Select list and profile sources without branching on brand classes."""

    vehicle_list_source: DiscoverySource
    vehicle_profile_source: DiscoverySource


class _BindingBehavior:
    binding_id: str
    enterprise_code: str
    contracts: Mapping[VehicleOperation, VehicleRequestContract]

    def contract(self, operation: VehicleOperation) -> VehicleRequestContract:
        try:
            return self.contracts[operation]
        except KeyError as error:
            raise ValueError(f"{self.binding_id} does not support {operation}") from error

    def operation_for_contract(self, contract_id: str) -> VehicleOperation:
        for operation, contract in self.contracts.items():
            if contract.contract_id == contract_id:
                return operation
        raise ValueError(f"{self.binding_id} does not own request contract {contract_id}")

    def supports(self, operation: VehicleOperation) -> bool:
        return operation in self.contracts


@dataclass(frozen=True)
class IvcsBinding(_BindingBehavior):
    """One enterprise binding to the common IVCS vehicle namespace."""

    binding_id: str
    enterprise_code: str
    omp_enterprise_code: str
    gateway_origin: str
    contracts: Mapping[VehicleOperation, VehicleRequestContract]
    allowed_spec_ids: frozenset[str]
    discovery_plan: DiscoveryPlan

_VEHICLE_GATEWAY_HEADERS = frozenset({"Authorization", "X-Nonce", "X-Created", "X-Device-Id"})
_VEHICLE_CONTROL_HEADERS = _VEHICLE_GATEWAY_HEADERS | {"X-Vehicle-Id"}


def _common_ivcs_contracts(binding_id: str, evidence: str) -> Mapping[VehicleOperation, VehicleRequestContract]:
    """Common IVCS namespace contracts shared by every enterprise binding."""
    def contract(operation: str, method: str, path: str, headers: frozenset[str]) -> VehicleRequestContract:
        return VehicleRequestContract(
            f"{binding_id}.ivcs.{operation}", evidence, method, path, headers, CredentialPurpose.VEHICLE_GATEWAY
        )

    return {
        VehicleOperation.VEHICLE_LIST: contract("vehicles.v1", "GET", "/vcam/v1/accounts/vehicles", _VEHICLE_GATEWAY_HEADERS),
        VehicleOperation.VEHICLE_PROFILE: contract("vehicle-info.v1", "GET", "/vcam/v1/accounts/vehicle-infos/{vehicle_id}", _VEHICLE_GATEWAY_HEADERS),
        VehicleOperation.DYNAMIC_INFOS: contract("dynamic-infos.v2", "POST", "/vctrl/v2/controls/dynamic-infos", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.ENERGY_REPORT: contract("energy.latest.v1", "GET", "/vdas/v1/report/energy/latest", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.LOCATION: contract("location.v1", "GET", "/vcam/v1/find-car/location", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.FIRMWARE: contract("firmware.current.v1", "GET", "/vota/v1/firmware/current-version", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.COMMAND_STATUS: contract("command-status.v2", "GET", "/vctrl/v2/controls/commands/{command_id}", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.AIR_CONDITIONER: contract("air-conditioner.v1", "POST", "/vctrl/v1/controls/air-conditioner", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.RAPID_AIR_CONDITIONER: contract("air-conditioner.rapid.v1", "POST", "/vctrl/v1/controls/air-conditioner/rapid", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.DEFROST: contract("hvac.v1", "POST", "/vctrl/v1/controls/hvac", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.DEPARTURE_PLAN: contract("departure-plan.v1", "POST", "/vctrl/v1/controls/departure-plans/now/0", _VEHICLE_CONTROL_HEADERS),
        VehicleOperation.SENTRY_MODE: contract("sentry.v1", "POST", "/vctrl/v1/controls/sentry", _VEHICLE_CONTROL_HEADERS),
    }


_NORMAL_IVCS_OPERATIONS = frozenset(
    {
        VehicleOperation.VEHICLE_LIST,
        VehicleOperation.VEHICLE_PROFILE,
        VehicleOperation.DYNAMIC_INFOS,
        VehicleOperation.COMMAND_STATUS,
        VehicleOperation.AIR_CONDITIONER,
        VehicleOperation.RAPID_AIR_CONDITIONER,
        VehicleOperation.DEFROST,
        VehicleOperation.DEPARTURE_PLAN,
        VehicleOperation.SENTRY_MODE,
    }
)


def _binding(
    binding_id: str,
    enterprise_code: str,
    omp_enterprise_code: str,
    gateway_origin: str,
    allowed_spec_ids: frozenset[str],
    discovery_plan: DiscoveryPlan,
    evidence: str,
    enabled_operations: frozenset[VehicleOperation] = _NORMAL_IVCS_OPERATIONS,
) -> IvcsBinding:
    contracts = _common_ivcs_contracts(binding_id, evidence)
    return IvcsBinding(
        binding_id=binding_id,
        enterprise_code=enterprise_code,
        omp_enterprise_code=omp_enterprise_code,
        gateway_origin=gateway_origin,
        contracts={operation: contracts[operation] for operation in enabled_operations},
        allowed_spec_ids=allowed_spec_ids,
        discovery_plan=discovery_plan,
    )


_VCAM_DISCOVERY = DiscoveryPlan(DiscoverySource.IVCS_VCAM, DiscoverySource.IVCS_VEHICLE_INFO)
_SERES_DISCOVERY = DiscoveryPlan(DiscoverySource.IVCS_VCAM, DiscoverySource.OMP_MANAGEMENT_LIST)

SERES_BINDING = _binding(
    "seres_ivcs", "SERES", "", "https://apig.fgaiservice.com",
    frozenset({"seres_f3", "seres_aito_a15", "seres_x1", "seres_x1_24_u", "seres_x1ev_24_c", "seres_f1_24_h", "seres_f1_24_u"}), _SERES_DISCOVERY, "2026-08-seres",
    enabled_operations=_NORMAL_IVCS_OPERATIONS | {VehicleOperation.ENERGY_REPORT, VehicleOperation.LOCATION, VehicleOperation.FIRMWARE},
)
CHERY_BINDING = _binding(
    "chery_ivcs", "CHERY", "CHERY", "https://apir.chssatsp.icvcs.com",
    frozenset({"luxeed_r7"}), _VCAM_DISCOVERY, "2026-08-chery-runtime",
)
BAIC_BINDING = _binding(
    "baic_ivcs", "BAIC", "BAIC", "https://apir.bjevssa.icvcs.com",
    frozenset(), _VCAM_DISCOVERY, "2026-08-baic-static",
)
JAC_BINDING = _binding(
    "jac_ivcs", "JAC", "JAC", "https://apir.jacssa.icvcs.com",
    frozenset(), _VCAM_DISCOVERY, "2026-08-jac-static",
)
SAIC_BINDING = _binding(
    "saic_ivcs", "SAIC", "SAIC", "https://api-app.srih.icvcs.com",
    frozenset({"saic_h5"}), _VCAM_DISCOVERY, "2026-08-saic-runtime",
    enabled_operations=_NORMAL_IVCS_OPERATIONS | {VehicleOperation.ENERGY_REPORT, VehicleOperation.FIRMWARE},
)

BINDINGS: Mapping[str, IvcsBinding] = {
    binding.binding_id: binding
    for binding in (SERES_BINDING, CHERY_BINDING, BAIC_BINDING, JAC_BINDING, SAIC_BINDING)
}


def binding_for_id(binding_id: str) -> IvcsBinding:
    try:
        return BINDINGS[binding_id]
    except KeyError as error:
        raise ValueError(f"unknown IVCS binding {binding_id}") from error
