"""Small, explicit policy for route-level cloud polling."""

from __future__ import annotations

from typing import Any, Mapping

from .specs import VehicleSpec, dynamic_sections

# Offline or unknown routes ask the cloud only for this observed connection field.
# A full dynamic request is made only after it reports the vehicle online.
PRESENCE_SECTIONS: dict[str, int] = {"vehicleStatus": 0}


def connection_state(data: Mapping[str, Any]) -> bool | None:
    """Return True online, False offline, or None when upstream omits the field."""
    vehicle_status = data.get("vehicleStatus")
    if not isinstance(vehicle_status, Mapping):
        return None
    status = vehicle_status.get("connectStatus")
    if status in {0, "0"}:
        return False
    if status is None or isinstance(status, bool):
        return None
    return True


def sections_for_route(spec: VehicleSpec, online: bool | None) -> dict[str, int]:
    """Choose exactly one request shape for one scheduled route update."""
    return dynamic_sections(spec) if online is True else dict(PRESENCE_SECTIONS)
