"""Shared HTTP client for the five IVCS vehicle gateways."""

from __future__ import annotations

from datetime import datetime
import json
import ssl
import time
import uuid
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from .const import (
    DEFAULT_VEHICLE_GATEWAY_CLIENT_VERSION,
    DEFAULT_USER_AGENT,
)
from .models import VehicleDiscoveryContext, VehicleRequestContext
from .omp.contracts import CredentialPurpose

JSON = dict[str, Any]
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]

class VehicleGatewayApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        response: Any,
        *,
        response_headers: Mapping[str, str] | None = None,
        binding_id: str | None = None,
        contract_id: str | None = None,
        method: str | None = None,
        path_template: str | None = None,
    ) -> None:
        super().__init__(f"vehicle gateway request failed with HTTP {status}")
        self.status = status
        self.response = _safe_error_response(response)
        self.response_headers = dict(response_headers or {})
        self.binding_id = binding_id
        self.contract_id = contract_id
        self.method = method
        self.path_template = path_template


class VehicleCommandError(RuntimeError):
    def __init__(self, message: str, *, result_code: Any = None) -> None:
        super().__init__(message)
        self.result_code = result_code


class VehicleGatewayClient:
    """Stateless vehicle-gateway transport; account/OMP auth stays elsewhere."""

    def __init__(self, *, timeout: float = 20.0, transport: Transport | None = None) -> None:
        self.timeout = timeout
        self._transport = transport or _urllib_vehicle_gateway_transport

    def request(
        self,
        context: VehicleRequestContext | VehicleDiscoveryContext,
        *,
        payload: JSON | None = None,
        query: Mapping[str, str] | None = None,
        path_values: Mapping[str, str] | None = None,
    ) -> Any:
        context.require(CredentialPurpose.VEHICLE_GATEWAY)
        path = context.contract.render_path(**dict(path_values or {}))
        if query:
            path = f"{path}?{urlencode(query)}"
        url = f"{context.gateway_origin.rstrip('/')}{path}"
        _validate_fixed_https_origin(url, context.gateway_origin)
        body = None if context.contract.method == "GET" else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = _vehicle_gateway_headers(context.authorization, context.ivcs_device_id, getattr(context, "vehicle_id", None))
        missing_headers = context.contract.required_header_names - headers.keys()
        if missing_headers:
            raise ValueError(f"request contract is missing required headers: {', '.join(sorted(missing_headers))}")
        try:
            return self._request(context.contract.method, url, headers, body)
        except VehicleGatewayApiError as error:
            raise VehicleGatewayApiError(
                error.status,
                error.response,
                response_headers=error.response_headers,
                binding_id=context.binding_id,
                contract_id=context.contract.contract_id,
                method=context.contract.method,
                path_template=context.contract.path_template,
            ) from error

    def command(self, context: VehicleRequestContext, *, query: Mapping[str, str], command_status_context: VehicleRequestContext) -> None:
        command_id = self.request(context, query=query)
        if not isinstance(command_id, str) or not command_id:
            raise VehicleCommandError("vehicle command did not return a command id")
        deadline = time.monotonic() + 20
        while True:
            response = self.request(command_status_context, path_values={"command_id": quote(command_id, safe="")})
            result_code = response.get("resultCode") if isinstance(response, Mapping) else None
            if result_code in {0, "0"}:
                return
            if result_code not in {-100, "-100"}:
                raise VehicleCommandError("vehicle command failed", result_code=result_code)
            if time.monotonic() >= deadline:
                raise VehicleCommandError("vehicle command timed out")
            time.sleep(0.5)

    def _request(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Any:
        status, response_headers, response_body = self._transport(method, url, headers, body, self.timeout)
        response = _decode_response(response_body)
        if status >= 300:
            raise VehicleGatewayApiError(status, response, response_headers=response_headers)
        return response


def _vehicle_gateway_headers(authorization: str, device_id: str, vehicle_id: str | None) -> dict[str, str]:
    created = datetime.now().astimezone().strftime("%Y%m%d%H%M%S%f")[:-3]
    headers = {
        "Authorization": authorization, "X-Nonce": str(uuid.uuid4()).upper(), "X-Created": created,
        "X-App-Id": "0", "X-Client-Model": "iPhone", "X-Client-Language": "zh-Hans", "X-Client-Type": "2",
        "X-Client-Version": DEFAULT_VEHICLE_GATEWAY_CLIENT_VERSION, "Accept": "*/*", "Accept-Language": "zh-Hans-CN;q=1",
        "User-Agent": DEFAULT_USER_AGENT, "Content-Type": "application/json", "X-Device-Id": device_id,
    }
    if vehicle_id:
        headers["X-Vehicle-Id"] = vehicle_id
    return headers


def _validate_fixed_https_origin(url: str, origin: str) -> None:
    target, expected = urlsplit(url), urlsplit(origin)
    if target.scheme != "https" or (target.scheme, target.netloc) != (expected.scheme, expected.netloc):
        raise ValueError("request escaped its verified HTTPS origin")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _urllib_vehicle_gateway_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    """Vehicle-gateway transport with fixed-origin TLS and no redirects."""
    request = Request(url, data=body, headers=headers, method=method)
    try:
        context = ssl._create_unverified_context()
        with build_opener(_NoRedirect, HTTPSHandler(context=context)).open(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def _decode_response(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _safe_error_response(response: Any) -> Any:
    if not isinstance(response, Mapping):
        return None
    return {key: response[key] for key in ("code", "message", "msg", "resultCode") if key in response}
