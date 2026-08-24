"""OMP account and enterprise authorization transitions."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Mapping

from ..auth import (
    extract_credentials,
    extract_enterprise_authorizations,
    extract_refreshed_enterprise_authorization,
)
from ..models import AccountSession, EnterpriseSession
from .client import OmpClient
from .enterprises import ENDPOINTS


class OmpAuthorizationError(RuntimeError):
    pass


def create_enterprise_sessions(account: AccountSession, response: object) -> dict[str, EnterpriseSession]:
    """Build sessions only from explicit, registered enterprise token records.

    This is intentionally limited to manufacturers whose initial authorization
    is represented by the existing OMP ``vehicle_auth`` response. A new
    manufacturer requiring an explicit EC bootstrap or another auth exchange
    needs a separate verified authorization design; do not add token probing
    or fallback behavior here.
    """
    authorizations = extract_enterprise_authorizations(response)
    sessions: dict[str, EnterpriseSession] = {}
    for endpoint in ENDPOINTS.values():
        authorization = authorizations.get(endpoint.enterprise_code)
        if authorization:
            session_id = f"{endpoint.endpoint_id}:{uuid.uuid4()}"
            sessions[session_id] = EnterpriseSession(
                session_id=session_id,
                endpoint_id=endpoint.endpoint_id,
                enterprise_code=endpoint.enterprise_code,
                authorization=authorization,
                generation=1,
            )
    if not sessions:
        raise OmpAuthorizationError("vehicle authorization did not contain a registered enterprise token")
    return sessions


def refresh_enterprise_session(client: OmpClient, account: AccountSession, session: EnterpriseSession) -> EnterpriseSession:
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
        raise OmpAuthorizationError("enterprise refresh did not return its authorization")
    return replace(session, authorization=authorization, generation=session.generation + 1)


def refresh_account_session(client: OmpClient, account: AccountSession) -> AccountSession:
    """Refresh the shared account material and invalidate all enterprise scopes."""
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
