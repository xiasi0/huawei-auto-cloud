"""Route-scoped request-context construction and invariant checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import AccountSession, VehicleDiscoveryContext, VehicleGatewaySession, VehicleRequestContext, VehicleRoute
from .omp.enterprises import IvcsBinding, binding_for_id
from .omp.contracts import VehicleOperation


class RouteUnavailable(RuntimeError):
    """A route cannot safely send a request in its current state."""


@dataclass
class RouteRegistry:
    account: AccountSession
    routes: Mapping[str, VehicleRoute]
    sessions: Mapping[str, VehicleGatewaySession]

    def request_context(self, route_id: str, operation: VehicleOperation) -> VehicleRequestContext:
        route = self.routes.get(route_id)
        if route is None:
            raise RouteUnavailable("unknown route")
        binding = binding_for_id(route.binding_id)
        session = self.sessions.get(route.session_id)
        self._validate(route, binding, session)
        assert session is not None
        return VehicleRequestContext(
            route_id=route.route_id,
            vehicle_id=route.vehicle_id,
            binding_id=binding.binding_id,
            enterprise_code=route.enterprise_code,
            gateway_origin=binding.gateway_origin,
            authorization=vehicle_authorization(session),
            ivcs_device_id=self.account.ivcs_device_id,
            account_generation=self.account.account_generation,
            session_id=session.session_id,
            session_generation=session.generation,
            contract=binding.contract(operation),
        )

    def discovery_context(self, session_id: str, operation: VehicleOperation) -> VehicleDiscoveryContext:
        session = self.sessions.get(session_id)
        if session is None:
            raise RouteUnavailable("enterprise authorization session is unavailable")
        binding = binding_for_id(session.binding_id)
        if session.enterprise_code != binding.enterprise_code or not session.authorization:
            raise RouteUnavailable("enterprise discovery binding is inconsistent")
        return VehicleDiscoveryContext(
            binding_id=binding.binding_id,
            enterprise_code=binding.enterprise_code,
            gateway_origin=binding.gateway_origin,
            authorization=vehicle_authorization(session),
            ivcs_device_id=self.account.ivcs_device_id,
            account_generation=self.account.account_generation,
            session_id=session.session_id,
            session_generation=session.generation,
            contract=binding.contract(operation),
        )

    @staticmethod
    def _validate(route: VehicleRoute, binding: IvcsBinding, session: VehicleGatewaySession | None) -> None:
        if session is None:
            raise RouteUnavailable("route authorization session is unavailable")
        if route.enterprise_code != binding.enterprise_code or route.enterprise_code != session.enterprise_code:
            raise RouteUnavailable("route enterprise binding is inconsistent")
        if route.binding_id != session.binding_id:
            raise RouteUnavailable("route IVCS binding is inconsistent")
        if route.spec_id not in binding.allowed_spec_ids:
            raise RouteUnavailable("route vehicle specification is not allowed by its IVCS binding")
        if not session.authorization:
            raise RouteUnavailable("route authorization is missing")

    def is_current(self, context: VehicleRequestContext) -> bool:
        """Guard a response/command against account or session replacement."""
        try:
            binding = binding_for_id(context.binding_id)
            current = self.request_context(context.route_id, binding.operation_for_contract(context.contract.contract_id))
        except (RouteUnavailable, ValueError):
            return False
        return (
            current.account_generation == context.account_generation
            and current.session_id == context.session_id
            and current.session_generation == context.session_generation
            and current.contract.contract_id == context.contract.contract_id
        )


def vehicle_authorization(session: VehicleGatewaySession) -> str:
    """Return the refreshed vehicle-gateway credential for a binding scope."""
    if not session.authorization:
        raise RouteUnavailable("vehicle-gateway authorization is missing")
    return session.authorization
