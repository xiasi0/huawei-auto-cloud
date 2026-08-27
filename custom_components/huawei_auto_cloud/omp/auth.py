"""OMP account and IVCS vehicle-gateway authorization transitions."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Mapping

from ..auth import (
    extract_credentials,
    extract_enterprise_authorizations,
    extract_refreshed_enterprise_authorization,
    _find_vehicle_tokens,
)
from ..models import AccountSession, VehicleGatewaySession
from .client import OmpApiError, OmpClient
from .enterprises import BINDINGS, IvcsBinding


class OmpAuthorizationError(RuntimeError):
    pass


def vehicle_authorization_bindings() -> tuple[IvcsBinding, ...]:
    """Return the finite enterprise bindings used for vehicle authorization."""
    return tuple(BINDINGS.values())


def has_unroutable_vehicle_authorization(response: object) -> bool:
    """Whether OMP returned a vehicle token with no registered gateway."""
    registered_codes = {binding.enterprise_code for binding in BINDINGS.values()}
    for token in _find_vehicle_tokens(response):
        if not isinstance(token, Mapping):
            continue
        authorization = token.get("accessToken")
        enterprise_code = token.get("enterpriseCode")
        if isinstance(authorization, str) and authorization and (
            not isinstance(enterprise_code, str)
            or not enterprise_code
            or enterprise_code not in registered_codes
        ):
            return True
    return False


def create_vehicle_gateway_sessions(response: object) -> dict[str, VehicleGatewaySession]:
    """Build IVCS sessions only from explicit, registered token records.

    This is intentionally limited to manufacturers whose initial authorization
    is represented by the existing OMP ``vehicle_auth`` response. A new
    manufacturer requiring an explicit EC bootstrap or another auth exchange
    needs a separate verified authorization design; do not add token probing
    or fallback behavior here.
    """
    authorizations = extract_enterprise_authorizations(response)
    sessions: dict[str, VehicleGatewaySession] = {}
    for binding in BINDINGS.values():
        authorization = authorizations.get(binding.enterprise_code)
        if authorization:
            session_id = f"{binding.binding_id}:{uuid.uuid4()}"
            sessions[session_id] = VehicleGatewaySession(
                session_id=session_id,
                binding_id=binding.binding_id,
                enterprise_code=binding.enterprise_code,
                authorization=authorization,
                generation=1,
            )
    if not sessions:
        raise OmpAuthorizationError("vehicle authorization did not contain a registered enterprise token")
    return sessions


def refresh_vehicle_gateway_session(client: OmpClient, account: AccountSession, session: VehicleGatewaySession) -> VehicleGatewaySession:
    """Replace one scope only; callers retain all unrelated sessions unchanged."""
    response = client.vehicle_refresh(
        xid=account.xid,
        device_id=account.omp_device_id,
        user_id=account.omp_user_id,
        enterprise_code=session.enterprise_code,
        native_device_model=account.native_device_model,
    )
    authorization = extract_refreshed_enterprise_authorization(response, session.enterprise_code)
    if not authorization:
        raise OmpAuthorizationError("vehicle gateway refresh did not return its authorization")
    return replace(session, authorization=authorization, generation=session.generation + 1)


def refresh_vehicle_gateway_sessions(
    client: OmpClient,
    account: AccountSession,
    sessions: Mapping[str, VehicleGatewaySession],
) -> tuple[dict[str, VehicleGatewaySession], dict[str, str]]:
    """Refresh every IVCS scope independently and retain successful results."""
    refreshed: dict[str, VehicleGatewaySession] = {}
    failures: dict[str, str] = {}
    for session_id, session in sessions.items():
        try:
            refreshed[session_id] = refresh_vehicle_gateway_session(client, account, session)
        except (OmpApiError, OmpAuthorizationError) as error:
            failures[session_id] = type(error).__name__
    return refreshed, failures


def refresh_account_session(client: OmpClient, account: AccountSession) -> AccountSession:
    """Refresh the shared account material and invalidate all vehicle scopes."""
    response = client.refresh_user_token(
        account.access_token,
        account.refresh_token,
        device_id=account.omp_device_id,
        xid=account.xid,
        user_id=account.omp_user_id,
        native_device_model=account.native_device_model,
    )
    credentials = extract_credentials(response)
    required = (credentials.get("access_token"), credentials.get("refresh_token"), credentials.get("xid"))
    if not all(isinstance(value, str) and value for value in required):
        raise OmpAuthorizationError("account refresh did not return a usable OMP session")
    user_info = credentials.get("user_info")
    user_id = user_info.get("userId") if isinstance(user_info, Mapping) else account.omp_user_id
    return replace(
        account,
        account_generation=account.account_generation + 1,
        access_token=str(credentials["access_token"]),
        refresh_token=str(credentials["refresh_token"]),
        xid=str(credentials["xid"]),
        omp_user_id=str(user_id),
    )
