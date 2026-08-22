"""Route-scoped request-context construction and invariant checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import AccountSession, EnterpriseSession, OmpDiscoveryContext, OmpRequestContext, VehicleRoute
from .omp.enterprises import OmpManufacturerEndpoint, endpoint_for_id
from .omp.contracts import OmpOperation


class RouteUnavailable(RuntimeError):
    """A route cannot safely send a request in its current state."""


@dataclass
class RouteRegistry:
    account: AccountSession
    routes: Mapping[str, VehicleRoute]
    sessions: Mapping[str, EnterpriseSession]

    def request_context(self, route_id: str, operation: OmpOperation) -> OmpRequestContext:
        route = self.routes.get(route_id)
        if route is None:
            raise RouteUnavailable("unknown route")
        endpoint = endpoint_for_id(route.endpoint_id)
        session = self.sessions.get(route.session_id)
        self._validate(route, endpoint, session)
        assert session is not None
        return OmpRequestContext(
            route_id=route.route_id,
            vehicle_id=route.vehicle_id,
            endpoint_id=endpoint.endpoint_id,
            enterprise_code=route.enterprise_code,
            gateway_origin=endpoint.gateway_origin,
            authorization=session.authorization,
            ivcs_device_id=self.account.ivcs_device_id,
            account_generation=self.account.account_generation,
            session_id=session.session_id,
            session_generation=session.generation,
            contract=endpoint.contract(operation),
        )

    def discovery_context(self, session_id: str, operation: OmpOperation) -> OmpDiscoveryContext:
        session = self.sessions.get(session_id)
        if session is None:
            raise RouteUnavailable("enterprise authorization session is unavailable")
        endpoint = endpoint_for_id(session.endpoint_id)
        if session.enterprise_code != endpoint.enterprise_code or not session.authorization:
            raise RouteUnavailable("enterprise discovery binding is inconsistent")
        return OmpDiscoveryContext(
            endpoint_id=endpoint.endpoint_id,
            enterprise_code=endpoint.enterprise_code,
            gateway_origin=endpoint.gateway_origin,
            authorization=session.authorization,
            ivcs_device_id=self.account.ivcs_device_id,
            account_generation=self.account.account_generation,
            session_id=session.session_id,
            session_generation=session.generation,
            contract=endpoint.contract(operation),
        )

    @staticmethod
    def _validate(route: VehicleRoute, endpoint: OmpManufacturerEndpoint, session: EnterpriseSession | None) -> None:
        if session is None:
            raise RouteUnavailable("route authorization session is unavailable")
        if route.enterprise_code != endpoint.enterprise_code or route.enterprise_code != session.enterprise_code:
            raise RouteUnavailable("route enterprise binding is inconsistent")
        if route.endpoint_id != session.endpoint_id:
            raise RouteUnavailable("route endpoint binding is inconsistent")
        if route.spec_id not in endpoint.allowed_spec_ids:
            raise RouteUnavailable("route vehicle specification is not allowed by its endpoint")
        if not session.authorization:
            raise RouteUnavailable("route authorization is missing")

    def is_current(self, context: OmpRequestContext) -> bool:
        """Guard a response/command against account or session replacement."""
        try:
            endpoint = endpoint_for_id(context.endpoint_id)
            current = self.request_context(context.route_id, endpoint.operation_for_contract(context.contract.contract_id))
        except (RouteUnavailable, ValueError):
            return False
        return (
            current.account_generation == context.account_generation
            and current.session_id == context.session_id
            and current.session_generation == context.session_generation
            and current.contract.contract_id == context.contract.contract_id
        )
