"""Static, reviewed contracts for the common IVCS vehicle namespace."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CredentialPurpose(StrEnum):
    VEHICLE_GATEWAY = "vehicle_gateway"


class VehicleOperation(StrEnum):
    """Reviewed operations exposed by an IVCS vehicle binding."""

    VEHICLE_LIST = "vehicle_list"
    VEHICLE_PROFILE = "vehicle_profile"
    DYNAMIC_INFOS = "dynamic_infos"
    ENERGY_REPORT = "energy_report"
    LOCATION = "location"
    FIRMWARE = "firmware"
    COMMAND_STATUS = "command_status"
    AIR_CONDITIONER = "air_conditioner"
    RAPID_AIR_CONDITIONER = "rapid_air_conditioner"
    DEFROST = "defrost"
    DEPARTURE_PLAN = "departure_plan"
    SENTRY_MODE = "sentry_mode"


@dataclass(frozen=True)
class VehicleRequestContract:
    """One verified IVCS request shape; not a runtime DSL."""

    contract_id: str
    evidence_version: str
    method: str
    path_template: str
    required_header_names: frozenset[str]
    credential_purpose: CredentialPurpose

    def render_path(self, **values: str) -> str:
        path = self.path_template.format(**values)
        if not path.startswith("/"):
            raise ValueError(f"{self.contract_id} rendered a relative path")
        return path
