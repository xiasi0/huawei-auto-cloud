"""Minimal, non-identifying diagnostics for the new domain."""

from __future__ import annotations

from typing import Any

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("coordinator")
    if coordinator is None:
        return {"entry": {"configured": True}}
    return {
        "entry": {"configured": True},
        "account_generation": coordinator.account.account_generation,
        "routes": {
            route_id: {
                "endpoint_id": route.endpoint_id,
                "enterprise_code": route.enterprise_code,
                "spec_id": route.spec_id,
                "session_generation": coordinator.sessions[route.session_id].generation,
                "last_error": coordinator.route_errors.get(route_id),
            }
            for route_id, route in coordinator.routes.items()
            if route.session_id in coordinator.sessions
        },
    }
