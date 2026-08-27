"""Huawei account and OMP authorization client."""

from __future__ import annotations

import json
import secrets
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..const import (
    DEFAULT_DEVICE_MODEL,
    DEFAULT_NATIVE_DEVICE_MODEL,
    DEFAULT_OMP_CLIENT_TYPE,
    DEFAULT_PACKAGE_NAME,
    DEFAULT_USER_AGENT,
    OMP_BASE_ORIGIN,
)

JSON = dict[str, Any]
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]

class OmpApiError(RuntimeError):
    def __init__(self, status: int, response: Any, *, response_headers: Mapping[str, str] | None = None) -> None:
        super().__init__(f"Huawei Auto Cloud request failed with HTTP {status}")
        self.status = status
        self.response = _safe_error_response(response)
        self.response_headers = dict(response_headers or {})


class OmpClient:
    """Huawei account and OMP authorization client."""

    def __init__(self, *, timeout: float = 20.0, transport: Transport | None = None) -> None:
        self.timeout = timeout
        self._transport = transport or _urllib_transport
        self._cookies: dict[str, str] = {}
        self._omp_warm_attempted = False

    @property
    def omp_cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def set_omp_cookies(self, cookies: Mapping[str, str]) -> None:
        self._cookies = {str(key): str(value) for key, value in cookies.items() if key and value}

    def user_auth(self, auth_code: str, *, device_id: str, device_model: str = DEFAULT_DEVICE_MODEL,
                  native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL, client_type: str = DEFAULT_OMP_CLIENT_TYPE) -> Any:
        return self._post_omp(
            "/xcar/omp/xbs/account/user/auth",
            {"authCode": auth_code, "clientType": client_type, "device": {"type": "1", "id": device_id, "model": device_model}},
            _omp_session_headers(native_device_model),
        )

    def refresh_user_token(self, access_token: str, refresh_token: str, *, device_id: str, xid: str,
                           user_id: str | None, native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL) -> Any:
        return self._post_omp(
            "/xcar/omp/xbs/account/user/refresh",
            {"clientType": DEFAULT_OMP_CLIENT_TYPE, "device": {"type": "1", "id": device_id, "model": DEFAULT_DEVICE_MODEL}, "at": access_token, "rt": refresh_token},
            _omp_session_headers(native_device_model, xid=xid, user_id=user_id),
        )

    def vehicle_auth(self, *, xid: str, device_id: str, user_id: str | None,
                     enterprise_code: str = "", native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL) -> Any:
        return self._post_omp(
            "/xcar/omp/xbs/account/vehicle/auth",
            {"deviceInfo": {"type": "1", "id": device_id, "model": DEFAULT_DEVICE_MODEL}},
            _omp_session_headers(native_device_model, xid=xid, user_id=user_id, enterprise_code=enterprise_code),
        )

    def vehicle_management_list(self, *, xid: str, device_id: str, user_id: str | None, enterprise_code: str,
                                 native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL, refresh: bool = True) -> Any:
        return self._post_omp(
            "/xcar/omp/xbs/vehicle/management/list",
            {"refreshFlag": "true" if refresh else "false", "deviceInfo": {"type": "1", "id": device_id, "model": DEFAULT_DEVICE_MODEL}},
            _omp_session_headers(native_device_model, xid=xid, user_id=user_id, enterprise_code=enterprise_code),
        )

    def vehicle_management_query(self, *, vehicle_id: str, xid: str, device_id: str, user_id: str | None,
                                 enterprise_code: str,
                                 native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL) -> Any:
        """Fetch one vehicle's OMP merged information and resource declaration."""
        return self._post_omp(
            "/xcar/omp/xbs/vehicle/management/query",
            {
                "vehicleId": vehicle_id,
                "enterpriseCode": enterprise_code,
                "deviceInfo": {"type": "1", "id": device_id, "model": DEFAULT_DEVICE_MODEL},
            },
            _omp_session_headers(native_device_model, xid=xid, user_id=user_id, enterprise_code=enterprise_code),
        )

    def force_login(self, *, xid: str, device_id: str, user_id: str,
                    native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL) -> Any:
        return self._post_omp(
            "/xcar/omp/xbs/account/user/kickout",
            {"deviceInfo": {"type": "1", "id": device_id, "model": DEFAULT_DEVICE_MODEL}},
            _omp_session_headers(native_device_model, xid=xid, user_id=user_id),
        )

    def vehicle_refresh(self, *, xid: str, device_id: str, user_id: str | None, enterprise_code: str,
                        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL) -> Any:
        return self._post_omp(
            "/xcar/omp/xbs/account/vehicle/refresh",
            {"tokenType": 0, "requireAccountId": False, "deviceInfo": {"type": "1", "id": device_id, "model": DEFAULT_DEVICE_MODEL}},
            _omp_session_headers(native_device_model, xid=xid, user_id=user_id, enterprise_code=enterprise_code),
        )

    def _post_omp(self, path: str, payload: JSON, headers: dict[str, str]) -> Any:
        if not self._omp_warm_attempted:
            self._warm_omp_session()
        return self._request("POST", f"{OMP_BASE_ORIGIN}{path}", _omp_headers(headers), json.dumps(payload, ensure_ascii=False).encode("utf-8"), use_cookies=True)

    def _warm_omp_session(self) -> None:
        try:
            self._request("POST", f"{OMP_BASE_ORIGIN}/xcar/omp/xbs/page/queryCustomizedPage", _omp_headers(), b'{"pageType":0}', use_cookies=True)
        except OmpApiError:
            return
        self._omp_warm_attempted = True

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        *,
        use_cookies: bool = False,
    ) -> Any:
        request_headers = dict(headers)
        if use_cookies and self._cookies:
            request_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in self._cookies.items())
        status, response_headers, response_body = self._transport(method, url, request_headers, body, self.timeout)
        if use_cookies:
            self._capture_cookies(response_headers)
        response = _decode_response(response_body)
        if status >= 300:
            raise OmpApiError(status, response, response_headers=response_headers)
        return response

    def _capture_cookies(self, headers: Mapping[str, str]) -> None:
        for key, value in headers.items():
            if key.lower() == "set-cookie":
                for item in value.splitlines():
                    name, _, cookie_value = item.partition("=")
                    if name and cookie_value:
                        self._cookies[name.strip()] = cookie_value.split(";", 1)[0]


def _omp_headers(extra_headers: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "*/*", "Accept-Language": "zh-cn", "User-Agent": DEFAULT_USER_AGENT, "pkgName": DEFAULT_PACKAGE_NAME, "traceID": secrets.token_hex(8).upper()}
    if extra_headers:
        headers.update({key: value for key, value in extra_headers.items() if value is not None})
    return headers


def _omp_session_headers(native_device_model: str, *, xid: str | None = None, user_id: str | None = None,
                         enterprise_code: str = "") -> dict[str, str]:
    return {"deviceModel": native_device_model, "uid": user_id or "", "xid": xid or "", "EC": enterprise_code}


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, dict[str, str], bytes]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
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


def safe_response_shape(response: Any) -> dict[str, Any]:
    """Return diagnostic-only response metadata; never include values or secrets."""
    if not isinstance(response, Mapping):
        return {"response_type": type(response).__name__}
    return {
        "response_type": "object",
        "top_level_keys": sorted(str(key) for key in response)[:30],
        "code": _safe_scalar(response.get("code")),
        "result_code": _safe_scalar(response.get("resultCode")),
        "session_key_status": _find_session_key_status(response),
    }


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    return value if isinstance(value, (str, int, float, bool)) or value is None else type(value).__name__


def _find_session_key_status(value: Any) -> str | int | float | bool | None:
    if isinstance(value, Mapping):
        user_info = value.get("userInfo")
        if isinstance(user_info, Mapping) and "sessionKeyStatus" in user_info:
            return _safe_scalar(user_info["sessionKeyStatus"])
        for child in value.values():
            status = _find_session_key_status(child)
            if status is not None:
                return status
    elif isinstance(value, list):
        for child in value:
            status = _find_session_key_status(child)
            if status is not None:
                return status
    return None
