from __future__ import annotations

from datetime import datetime
import json
import secrets
import ssl
import time
import uuid
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .const import (
    APIG_BASE_URLS,
    APIG_BASE_URL,
    DEFAULT_APIG_CLIENT_VERSION,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_NATIVE_DEVICE_MODEL,
    DEFAULT_OMP_CLIENT_TYPE,
    DEFAULT_PACKAGE_NAME,
    DEFAULT_USER_AGENT,
    DEFAULT_VEHICLE_EC,
    OMP_BASE_URL,
)

JSON = dict[str, Any]
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]

DEFAULT_DYNAMIC_INFO_SECTIONS: JSON = {
    "vehicleStatus": 0,
    "door": 0,
    "window": 0,
    "tire": 0,
    "seat": 0,
    "lamp": 0,
    "charge": 0,
    "hvac": 0,
    "fuel": 0,
    "welcome": 0,
    "departurePlan": 0,
    "airConditionPlan": 0,
    "warmCoolingBox": 0,
    "sentryPlan": 0,
}


class AitoApiError(RuntimeError):
    def __init__(self, status: int, response: Any, *, url: str | None = None) -> None:
        safe_response = _safe_error_response(response)
        message = f"AITO request failed with HTTP {status}"
        if url:
            message = f"{message} on {url}"
        response_summary = _safe_response_summary(safe_response)
        if response_summary:
            message = f"{message}: {response_summary}"
        super().__init__(message)
        self.status = status
        self.response = safe_response
        self.url = url


class AitoCommandError(RuntimeError):
    """A vehicle command was rejected or did not finish in time."""

    def __init__(self, message: str, *, result_code: Any = None) -> None:
        super().__init__(message)
        self.result_code = result_code


class AitoApiClient:
    def __init__(
        self,
        *,
        omp_base_url: str = OMP_BASE_URL,
        apig_base_url: str = APIG_BASE_URL,
        apig_authorization: str | None = None,
        apig_client_version: str = DEFAULT_APIG_CLIENT_VERSION,
        ivcs_device_id: str | None = None,
        omp_cookies: Mapping[str, str] | None = None,
        timeout: float = 20.0,
        transport: Transport | None = None,
        apig_verify_ssl: bool = True,
    ) -> None:
        self.omp_base_url = omp_base_url.rstrip("/")
        self.apig_authorization = apig_authorization
        self.apig_client_version = apig_client_version
        self.ivcs_device_id = ivcs_device_id
        self.timeout = timeout
        self.transport = transport or _urllib_transport
        self.apig_transport = transport or (_urllib_transport if apig_verify_ssl else _urllib_insecure_transport)
        self._cookies = {
            str(name): str(value)
            for name, value in (omp_cookies or {}).items()
            if isinstance(name, str) and isinstance(value, str) and name and value
        }
        self._omp_warm_attempted = False
        # Enterprise code used on OMP requests and APIG gateway selection.
        # Defaults to SERES (AITO/问界); callers set this to the account's
        # real enterprise once known (e.g. CHERY for LUXEED/智界 vehicles).
        self._enterprise_code = DEFAULT_VEHICLE_EC
        self._apig_base_url = apig_base_url.rstrip("/")

    @property
    def enterprise_code(self) -> str:
        return self._enterprise_code

    @enterprise_code.setter
    def enterprise_code(self, value: str) -> None:
        """Set the enterprise code and switch the APIG gateway accordingly."""
        self._enterprise_code = value
        gateway = APIG_BASE_URLS.get(value)
        if gateway:
            self._apig_base_url = gateway.rstrip("/")

    @property
    def apig_base_url(self) -> str:
        return self._apig_base_url

    def user_auth(
        self,
        auth_code: str,
        *,
        device_id: str,
        device_model: str = DEFAULT_DEVICE_MODEL,
        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL,
        client_type: str = DEFAULT_OMP_CLIENT_TYPE,
    ) -> Any:
        payload: JSON = {
            "authCode": auth_code,
            "clientType": client_type,
            "device": {"type": "1", "id": device_id, "model": device_model},
        }
        return self._post_omp(
            "/xcar/omp/xbs/account/user/auth",
            payload,
            extra_headers=_omp_session_headers(native_device_model),
        )

    def refresh_user_token(
        self,
        access_token: str,
        refresh_token: str,
        *,
        device_id: str,
        device_model: str = DEFAULT_DEVICE_MODEL,
        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL,
        client_type: str = DEFAULT_OMP_CLIENT_TYPE,
        xid: str | None = None,
        ec: str = "",
        user_id: str | None = None,
    ) -> Any:
        payload: JSON = {
            "clientType": client_type,
            "device": {"type": "1", "id": device_id, "model": device_model},
            "at": access_token,
            "rt": refresh_token,
        }
        headers = _omp_session_headers(native_device_model, xid=xid, user_id=user_id, ec=ec)
        return self._post_omp(
            "/xcar/omp/xbs/account/user/refresh",
            payload,
            extra_headers=headers,
        )

    def vehicle_auth(
        self,
        *,
        xid: str,
        device_id: str,
        device_model: str = DEFAULT_DEVICE_MODEL,
        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL,
        ec: str = "",
        user_id: str | None = None,
    ) -> Any:
        """Establish a vehicle session after a fresh OMP user authentication."""
        payload = {"deviceInfo": {"type": "1", "id": device_id, "model": device_model}}
        headers = _omp_session_headers(native_device_model, xid=xid, user_id=user_id, ec=ec)
        return self._post_omp(
            "/xcar/omp/xbs/account/vehicle/auth",
            payload,
            extra_headers=headers,
        )

    def vehicle_refresh(
        self,
        *,
        xid: str,
        device_id: str,
        device_model: str = DEFAULT_DEVICE_MODEL,
        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL,
        ec: str | None = None,
        user_id: str | None = None,
    ) -> Any:
        ec = self.enterprise_code if ec is None else ec
        payload: JSON = {
            "tokenType": 0,
            "requireAccountId": False,
            "deviceInfo": {"type": "1", "id": device_id, "model": device_model},
        }
        headers = _omp_session_headers(native_device_model, xid=xid, user_id=user_id, ec=ec)
        return self._post_omp(
            "/xcar/omp/xbs/account/vehicle/refresh",
            payload,
            extra_headers=headers,
        )

    def force_login(
        self,
        *,
        xid: str,
        device_id: str,
        device_model: str = DEFAULT_DEVICE_MODEL,
        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL,
        user_id: str,
        ec: str = "",
    ) -> Any:
        return self._post_omp(
            "/xcar/omp/xbs/account/user/kickout",
            {"deviceInfo": {"type": "1", "id": device_id, "model": device_model}},
            extra_headers=_omp_session_headers(native_device_model, xid=xid, user_id=user_id, ec=ec),
        )

    def apig_vehicles(self) -> Any:
        return self._request_apig("GET", "/vcam/v1/accounts/vehicles")

    def vehicle_management_list(
        self,
        *,
        xid: str,
        device_id: str,
        device_model: str = DEFAULT_DEVICE_MODEL,
        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL,
        ec: str | None = None,
        user_id: str | None = None,
        refresh: bool = True,
    ) -> Any:
        """Return the official OMP vehicle profile and feature list."""
        ec = self.enterprise_code if ec is None else ec
        payload: JSON = {
            "refreshFlag": "true" if refresh else "false",
            "deviceInfo": {"type": "1", "id": device_id, "model": device_model},
        }
        return self._post_omp(
            "/xcar/omp/xbs/vehicle/management/list",
            payload,
            extra_headers=_omp_session_headers(native_device_model, xid=xid, user_id=user_id, ec=ec),
        )

    def vehicle_dictionary_values(
        self,
        codes: list[str],
        *,
        xid: str,
        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL,
        ec: str | None = None,
        user_id: str | None = None,
    ) -> Any:
        """Return the official OMP dictionary records for the requested codes."""
        ec = self.enterprise_code if ec is None else ec
        return self._post_omp(
            "/xcar/omp/xbs/v2/queryBatchDictItem",
            {"dicItemCodes": codes},
            extra_headers=_omp_session_headers(native_device_model, xid=xid, user_id=user_id, ec=ec),
        )

    def dynamic_infos(self, vehicle_id: str, sections: JSON | None = None) -> Any:
        return self._request_apig(
            "POST",
            "/vctrl/v2/controls/dynamic-infos",
            dict(DEFAULT_DYNAMIC_INFO_SECTIONS if sections is None else sections),
            vehicle_id=vehicle_id,
        )

    def latest_energy_report(self, vehicle_id: str) -> Any:
        """Return the official total, daily, and monthly energy report."""
        return self._request_apig(
            "GET",
            "/vdas/v1/report/energy/latest",
            vehicle_id=vehicle_id,
        )

    def location(self, vehicle_id: str) -> Any:
        return self._request_apig("GET", "/vcam/v1/find-car/location", vehicle_id=vehicle_id)

    def firmware_current_version(self, vehicle_id: str) -> Any:
        return self._request_apig("GET", "/vota/v1/firmware/current-version", vehicle_id=vehicle_id)

    def control_air_conditioner(
        self,
        vehicle_id: str,
        *,
        enabled: bool,
        target_temp: int | None = None,
    ) -> None:
        """Run the observed A/C command and wait for its asynchronous result."""
        if enabled and target_temp is None:
            raise ValueError("AITO air-conditioner requires targetTemp when enabling")
        query = {"enabled": str(enabled).lower()}
        if target_temp is not None:
            query["targetTemp"] = str(target_temp)
        self._control_vctrl_query("/vctrl/v1/controls/air-conditioner", vehicle_id, query)

    def control_air_conditioner_rapid(self, vehicle_id: str, *, enabled: bool, mode: int) -> None:
        """Run the observed rapid cool or rapid heat command."""
        if mode not in {1, 2}:
            raise ValueError("AITO rapid air-conditioner mode must be 1 or 2")
        self._control_vctrl_query(
            "/vctrl/v1/controls/air-conditioner/rapid",
            vehicle_id,
            {"enabled": str(enabled).lower(), "mode": str(mode)},
        )

    def control_defrost(self, vehicle_id: str, *, enabled: bool) -> None:
        """Run the observed front-defrost command."""
        self._control_vctrl_query(
            "/vctrl/v1/controls/hvac",
            vehicle_id,
            {"enabled": str(enabled).lower()},
        )

    def control_now_departure_plan(self, vehicle_id: str, *, enabled: bool) -> None:
        """Start or stop the official default immediate departure plan."""
        self._control_vctrl_query(
            "/vctrl/v1/controls/departure-plans/now/0",
            vehicle_id,
            {"enabled": str(enabled).lower()},
        )

    def control_sentry_mode(self, vehicle_id: str, *, enabled: bool) -> None:
        """Enable or disable the observed immediate sentry mode control."""
        self._control_vctrl_query(
            "/vctrl/v1/controls/sentry",
            vehicle_id,
            {"open": str(enabled).lower()},
        )

    def _control_vctrl_query(self, path: str, vehicle_id: str, query: dict[str, str]) -> None:
        command_id = self._request(
            "POST",
            f"{self.apig_base_url}{path}?{urlencode(query)}",
            _apig_headers(
                self._require_apig_authorization(),
                self.apig_client_version,
                self.ivcs_device_id,
                vehicle_id,
            ),
            None,
            transport=self.apig_transport,
        )
        if not isinstance(command_id, str) or not command_id:
            raise AitoCommandError("AITO vehicle command did not return a command id")
        self._wait_for_command(vehicle_id, command_id)

    def _post_omp(self, path: str, payload: JSON, *, extra_headers: dict[str, str] | None = None) -> Any:
        if not self._omp_warm_attempted:
            self._warm_omp_session()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request(
            "POST",
            f"{self.omp_base_url}/{path.lstrip('/')}",
            _omp_headers(extra_headers),
            body,
            use_cookies=True,
        )

    def _warm_omp_session(self) -> None:
        try:
            body = json.dumps({"pageType": 0}, ensure_ascii=False).encode("utf-8")
            self._request(
                "POST",
                f"{self.omp_base_url}/xcar/omp/xbs/page/queryCustomizedPage",
                {
                    "Accept": "*/*",
                    "Accept-Language": "zh-cn",
                    "Content-Type": "application/json",
                    "User-Agent": DEFAULT_USER_AGENT,
                },
                body,
                use_cookies=True,
            )
        except Exception:
            pass
        else:
            self._omp_warm_attempted = True

    def _request_apig(
        self,
        method: str,
        path: str,
        payload: JSON | None = None,
        *,
        vehicle_id: str | None = None,
    ) -> Any:
        authorization = self._require_apig_authorization()
        body = None if method == "GET" else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        return self._request(
            method,
            f"{self._apig_base_url}/{path.lstrip('/')}",
            _apig_headers(authorization, self.apig_client_version, self.ivcs_device_id, vehicle_id),
            body,
            transport=self.apig_transport,
        )

    def _require_apig_authorization(self) -> str:
        if not self.apig_authorization:
            raise ValueError("missing APIG authorization")
        return self.apig_authorization

    def _wait_for_command(self, vehicle_id: str, command_id: str) -> None:
        deadline = time.monotonic() + 20
        while True:
            response = self._request_apig(
                "GET",
                f"/vctrl/v2/controls/commands/{quote(command_id, safe='')}",
                vehicle_id=vehicle_id,
            )
            result_code = response.get("resultCode") if isinstance(response, Mapping) else None
            if result_code in {0, "0"}:
                return
            if result_code not in {-100, "-100"}:
                raise AitoCommandError(
                    f"AITO vehicle command failed with resultCode={result_code!r}",
                    result_code=result_code,
                )
            if time.monotonic() >= deadline:
                raise AitoCommandError("AITO vehicle command timed out")
            time.sleep(0.5)

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        *,
        transport: Transport | None = None,
        use_cookies: bool = False,
    ) -> Any:
        selected_transport = transport or self.transport
        request_headers = dict(headers)
        if use_cookies and self._cookies:
            request_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in self._cookies.items())
        status, response_headers, response_body = selected_transport(method, url, request_headers, body, self.timeout)
        if use_cookies:
            self._capture_cookies(response_headers)
        response = _decode_response(response_body)
        if status >= 400:
            raise AitoApiError(status, response, url=url)
        return response

    def _capture_cookies(self, headers: dict[str, str]) -> None:
        for key, value in headers.items():
            if key.lower() != "set-cookie":
                continue
            for item in value.splitlines():
                cookie, _, _attributes = item.partition(";")
                name, _, cookie_value = cookie.strip().partition("=")
                if name and cookie_value:
                    self._cookies[name] = cookie_value

    @property
    def omp_cookies(self) -> dict[str, str]:
        return dict(self._cookies)


def _omp_headers(extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "zh-cn",
        "User-Agent": DEFAULT_USER_AGENT,
        "pkgName": DEFAULT_PACKAGE_NAME,
        "traceID": secrets.token_hex(8).upper(),
    }
    if extra_headers:
        headers.update({key: value for key, value in extra_headers.items() if value is not None})
    return headers


def _omp_session_headers(
    native_device_model: str,
    *,
    xid: str | None = None,
    user_id: str | None = None,
    ec: str | None = None,
) -> dict[str, str]:
    return {
        "deviceModel": native_device_model,
        "uid": user_id or "",
        "xid": xid or "",
        "EC": ec or "",
    }


def _apig_headers(authorization: str, client_version: str, device_id: str | None, vehicle_id: str | None) -> dict[str, str]:
    created = datetime.now().astimezone().strftime("%Y%m%d%H%M%S%f")[:-3]
    headers = {
        "Authorization": authorization,
        "X-Nonce": str(uuid.uuid4()).upper(),
        "X-Created": created,
        "X-App-Id": "0",
        "X-Client-Model": "iPhone",
        "X-Client-Language": "zh-Hans",
        "X-Client-Type": "2",
        "X-Client-Version": client_version,
        "Accept": "*/*",
        "Accept-Language": "zh-Hans-CN;q=1",
        "User-Agent": DEFAULT_USER_AGENT,
        "Content-Type": "application/json",
    }
    if device_id:
        headers["X-Device-Id"] = device_id
    if vehicle_id:
        headers["X-Vehicle-Id"] = vehicle_id
    return headers

def _urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, _response_headers(response.headers), response.read()
    except HTTPError as error:
        return error.code, _response_headers(error.headers), error.read()


def _urllib_insecure_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    request = Request(url, data=body, headers=headers, method=method)
    context = ssl._create_unverified_context()
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            return response.status, _response_headers(response.headers), response.read()
    except HTTPError as error:
        return error.code, _response_headers(error.headers), error.read()


def _response_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in headers.keys():
        values = headers.get_all(key) if hasattr(headers, "get_all") else None
        if values:
            result[key] = "\n".join(str(value) for value in values)
        else:
            result[key] = str(headers[key])
    return result


def _decode_response(body: bytes) -> Any:
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _safe_response_summary(response: Any) -> str:
    if isinstance(response, dict):
        return json.dumps(response, ensure_ascii=False) if response else ""
    if isinstance(response, str):
        return response
    return ""


def _safe_error_response(response: Any) -> Any:
    if isinstance(response, dict):
        safe: dict[str, Any] = {}
        _collect_safe_error_fields(response, safe)
        return safe
    if isinstance(response, str):
        return f"str response length={len(response)}"
    return response


def _collect_safe_error_fields(value: Any, safe: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _ERROR_CODE_FIELDS and key not in safe:
                safe[key] = _safe_error_code(item)
                continue
            if key in _ERROR_TEXT_FIELDS and key not in safe:
                safe[key] = _safe_error_text(item)
                continue
            if key in _ERROR_TEXT_FIELDS and _is_safe_business_message(item) and not _is_safe_business_message(safe[key]):
                safe[key] = item
                continue
            _collect_safe_error_fields(item, safe)
        return
    if isinstance(value, list):
        for item in value:
            _collect_safe_error_fields(item, safe)


def _safe_error_text(value: Any) -> str:
    if _is_safe_business_message(value):
        return value
    return f"str length={len(value)}" if isinstance(value, str) else type(value).__name__


def _is_safe_business_message(value: Any) -> bool:
    return isinstance(value, str) and value in _SAFE_BUSINESS_MESSAGES


def _safe_error_code(value: Any) -> Any:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return value
    if isinstance(value, str):
        return f"str length={len(value)}"
    return type(value).__name__


_ERROR_CODE_FIELDS = ("code", "resultCode", "errorCode", "returnCode")
_ERROR_TEXT_FIELDS = ("msg", "message", "error", "error_description")
_SAFE_BUSINESS_MESSAGES = {
    "Token invalid",
    "Token already been cancelled",
    "xid is expired",
    "not login",
    "not logged in",
}
