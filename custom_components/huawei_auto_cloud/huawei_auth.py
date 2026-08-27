from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .auth import P256KeyPair, b64url
from .omp.client import _urllib_transport
from .const import HUAWEI_AUTO_CLIENT_ID, DEFAULT_DEVICE_MODEL, DEFAULT_NATIVE_DEVICE_MODEL, DEFAULT_PACKAGE_NAME

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]

HUAWEI_ACCOUNT_BASE_URL = "https://hwid-drcn.platform.hicloud.com"
HUAWEI_OAUTH_BASE_URL = "https://oauth-login.platform.hicloud.com"
HUAWEI_LOGIN_V3_PATH = "/AccountServer/IDM/loginV3"
HUAWEI_GET_RESOURCE_PATH = "/AccountServer/IUserInfoMng/getResource"
HUAWEI_GET_SMS_CODE_PATH = "/AccountServer/IDM/getSMSCodeV3"
HUAWEI_ST_AUTH_PATH = "/IdmClientApi/IDM/stAuth"
HUAWEI_SET_ASYM_PUBLIC_KEY_PATH = "/IdmClientApi/v2/setAsymPublicKey"
HUAWEI_SILENT_TOKEN_PATH = "/oauth2/v3/silent_token"
DEFAULT_HUAWEI_IOS_VERSION = "69220"
DEFAULT_HUAWEI_IOS_CVERSION = "ios_HwID_6.15.0.300"
HUAWEI_OAUTH_SCOPE = (
    "openid https://www.huawei.com/auth/account/mobile.number "
    "https://www.huawei.com/auth/account/base.profile profile "
    "https://www.huawei.com/auth/account/birthday "
    "https://www.huawei.com/auth/account/country email "
    "https://www.huawei.com/auth/account/realname/state"
)


class HuaweiAuthError(RuntimeError):
    def __init__(self, action: str, response: Any) -> None:
        safe_response = _safe_response(response)
        super().__init__(f"Huawei {action} failed: {_safe_summary(safe_response)}")
        self.response = safe_response


class HuaweiIosAuthClient:
    def __init__(
        self,
        *,
        device_id: str,
        device_model: str = DEFAULT_DEVICE_MODEL,
        native_device_model: str = DEFAULT_NATIVE_DEVICE_MODEL,
        jsessionid: str | None = None,
        cookies: Mapping[str, str] | None = None,
        timeout: float = 20.0,
        transport: Transport | None = None,
    ) -> None:
        self.device_id = device_id
        self.device_model = device_model
        self.native_device_model = native_device_model
        self.timeout = timeout
        self.transport = transport or _urllib_transport
        self._cookies = {
            str(name): str(value)
            for name, value in (cookies or {}).items()
            if isinstance(name, str) and isinstance(value, str) and name and value
        }
        if jsessionid:
            self._cookies["JSESSIONID"] = jsessionid

    @property
    def jsessionid(self) -> str | None:
        return self._cookies.get("JSESSIONID")

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def request_sms(self, phone: str) -> None:
        root = ET.Element("SMSCodeV3Req")
        fields = {
            "version": DEFAULT_HUAWEI_IOS_VERSION,
            "accountType": "2",
            "userID": phone,
            "userAccount": phone,
            "languageCode": "zh-Hans-CN",
            "reqClientType": "125",
            "smsReqType": "2",
            "mobilePhone": phone,
            "loginChannel": "125000000",
            "sceneID": "2",
        }
        for key, value in fields.items():
            ET.SubElement(root, key).text = value
        device_info = ET.SubElement(root, "deviceInfo")
        for key, value in self._device_info().items():
            ET.SubElement(device_info, key).text = str(value)
        response = self._post(
            HUAWEI_GET_SMS_CODE_PATH,
            ET.tostring(root, encoding="utf-8", xml_declaration=True),
            self._headers("text/xml; charset=utf-8"),
        )
        _raise_huawei_result_error("SMS request", response)

    def login_with_sms_password(self, phone: str, sms_code: str, password: str) -> dict[str, str]:
        public_key = self._fetch_rsa_public_key()
        body = urlencode(
            {
                "ver": DEFAULT_HUAWEI_IOS_VERSION,
                "acT": "2",
                "ac": phone,
                "pw": _rsa_oaep_encrypt_hex(password, public_key),
                "smsCodeType": "1",
                "dvT": "6",
                "dvID": self.device_id,
                "tmT": self.native_device_model,
                "clT": "125",
                "cn": "125000000",
                "os": "iOS15.8.8",
                "app": DEFAULT_PACKAGE_NAME,
                "dvN": self.device_model,
                "uuid": self.device_id,
                "vCode": sms_code,
                "vAcT": "2",
                "vAc": phone,
                "lang": "zh-Hans-CN",
                "dS": "0",
                "mA": "1",
                "deviceInfo": json.dumps(self._device_info(), separators=(",", ":")),
                "flag": "1",
            }
        ).encode("utf-8")
        headers = self._headers("application/x-www-form-urlencoded")
        response = self._post(HUAWEI_LOGIN_V3_PATH, body, headers)
        fields = _parse_form_response(response)
        if fields.get("resultCode") not in (None, "0"):
            raise HuaweiAuthError("SMS password login", fields)
        return fields

    def st_auth(self, service_token: str) -> dict[str, Any]:
        payload = {
            "chkAcctChange": 0,
            "appID": DEFAULT_PACKAGE_NAME,
            "serviceToken": service_token,
            "reqClientType": 125,
            "loginChannel": 125000000,
            "version": DEFAULT_HUAWEI_IOS_VERSION,
            "isGetAccount": 0,
            "isGetAgrVers": 1,
            "deviceSecure": 0,
            "deviceInfo": {**self._device_info(), "wifiSSID": "", "netType": "0"},
        }
        response = self._post_json(HUAWEI_ST_AUTH_PATH, payload, self._headers("application/json; charset=utf-8"))
        if not isinstance(response, dict):
            raise HuaweiAuthError("stAuth", response)
        _raise_huawei_result_error("stAuth", response)
        return response

    def set_asym_public_key(self, user_id: str, service_token: str, key_pair: P256KeyPair) -> P256KeyPair:
        session_id = self.jsessionid
        if not session_id:
            raise HuaweiAuthError("setAsymPublicKey", "missing JSESSIONID")
        payload = {
            "userID": user_id,
            "serviceToken": service_token,
            "publicKey": key_pair.public_key_b64,
            "asymKeyAlgo": 1,
            "asymKeyID": key_pair.key_id,
            "appID": DEFAULT_PACKAGE_NAME,
            "reqClientType": 125,
            "version": DEFAULT_HUAWEI_IOS_VERSION,
            "deviceInfo": self._device_info(),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        authorization = _digest_authorization(user_id, service_token, "setAsymPublicKey")
        headers = self._headers("application/json; charset=utf-8")
        headers.update(
            {
                "Authorization": authorization,
                "Cookie": f"JSESSIONID={session_id}",
                "x-asym-authorization": _x_asym_authorization(
                    key_pair,
                    HUAWEI_SET_ASYM_PUBLIC_KEY_PATH,
                    authorization,
                    session_id,
                    body,
                ),
            }
        )
        response = self._post(HUAWEI_SET_ASYM_PUBLIC_KEY_PATH, body, headers)
        if not isinstance(response, dict):
            raise HuaweiAuthError("setAsymPublicKey", response)
        _raise_huawei_result_error("setAsymPublicKey", response)
        return key_pair

    def silent_token(self, service_token: str) -> str:
        body = urlencode(
            {
                "grant_type": "service_token",
                "scope": HUAWEI_OAUTH_SCOPE,
                "service_token": service_token,
                "device_type": "6",
                "package_name": DEFAULT_PACKAGE_NAME,
                "siteId": "1",
                "need_code": "true",
                "device_id": self.device_id,
                "uuid": self.device_id,
            }
        ).encode("utf-8")
        query = urlencode(
            {
                "client_id": HUAWEI_AUTO_CLIENT_ID,
                "Version": DEFAULT_HUAWEI_IOS_VERSION,
                "cVersion": DEFAULT_HUAWEI_IOS_CVERSION,
                "srcAppName": DEFAULT_PACKAGE_NAME,
            }
        )
        status, response_headers, response_body = self.transport(
            "POST",
            f"{HUAWEI_OAUTH_BASE_URL}{HUAWEI_SILENT_TOKEN_PATH}?{query}",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
                "Accept-Language": "zh-cn",
                "terminal-type": "iPhone",
            },
            body,
            self.timeout,
        )
        response = _decode_response(response_body)
        if status >= 400 or not isinstance(response, dict) or not isinstance(response.get("code"), str):
            raise HuaweiAuthError("silent_token", response)
        return response["code"]

    def _fetch_rsa_public_key(self) -> str:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<GetResourceReq>"
            f"<version>{DEFAULT_HUAWEI_IOS_VERSION}</version>"
            "<resourceID>upLogin</resourceID>"
            "<reqClientType>125</reqClientType>"
            "<languageCode>zh-Hans-CN</languageCode>"
            "</GetResourceReq>"
        ).encode("utf-8")
        response = self._post(HUAWEI_GET_RESOURCE_PATH, body, self._headers("text/xml; charset=utf-8"))
        public_key = _extract_public_key(response)
        if not public_key:
            raise HuaweiAuthError("RSA resource lookup", "missing public key")
        return public_key

    def _post(self, path: str, body: bytes, headers: dict[str, str]) -> Any:
        query_params = _huawei_query()
        query = urlencode(query_params)
        request_headers = dict(headers)
        if "Cookie" not in request_headers and self._cookies:
            request_headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self._cookies.items())
        request_headers.setdefault("SOAPAction", f"{HUAWEI_ACCOUNT_BASE_URL}{path}?{query}")
        request_headers.setdefault("Authorization", str(int(time.time() * 1000)))
        status, response_headers, response_body = self.transport(
            "POST",
            f"{HUAWEI_ACCOUNT_BASE_URL}{path}?{query}",
            request_headers,
            body,
            self.timeout,
        )
        self._capture_account_cookies(response_headers)
        response = _decode_response(response_body)
        if status >= 400:
            raise HuaweiAuthError(f"HTTP {status}", response)
        return response

    def _post_json(self, path: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._post(path, body, headers)

    def _headers(self, content_type: str) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Accept-Language": "zh-cn",
            "Content-Type": content_type,
        }

    def _device_info(self) -> dict[str, Any]:
        return {
            "terminalCategory": 9,
            "deviceType": 6,
            "deviceID": self.device_id,
            "terminalType": "iPhone",
            "deviceAliasName": self.device_model,
            "uuid": self.device_id,
        }

    def _capture_account_cookies(self, headers: dict[str, str]) -> None:
        for key, value in headers.items():
            if key.lower() != "set-cookie":
                continue
            for item in value.splitlines():
                cookie, _, _attributes = item.partition(";")
                name, _, cookie_value = cookie.strip().partition("=")
                if name and cookie_value:
                    self._cookies[name] = cookie_value


def _huawei_query() -> dict[str, str]:
    return {
        "Version": DEFAULT_HUAWEI_IOS_VERSION,
        "cVersion": DEFAULT_HUAWEI_IOS_CVERSION,
        "ctrID": uuid.uuid4().hex,
        "srcAppName": DEFAULT_PACKAGE_NAME,
    }


def _rsa_oaep_encrypt_hex(password: str, public_key_pem: str) -> str:
    public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    encrypted = public_key.encrypt(
        password.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return encrypted.hex().upper()


def _digest_authorization(user_id: str, service_token: str, method: str) -> str:
    timestamp = str(int(time.time() * 1000))
    random_suffix = "".join(str(secrets.randbelow(10) + 1) for _ in range(3))
    nonce = f"{timestamp}:{random_suffix}"
    response = hmac.new(service_token.encode("utf-8"), f"{nonce}:{method}".encode("utf-8"), hashlib.sha256).hexdigest().upper()
    return f"Digest user={user_id},nonce={nonce},response={response}"


def _x_asym_authorization(
    key_pair: P256KeyPair,
    request_path: str,
    authorization: str,
    session_id: str,
    body: bytes,
) -> str:
    normalized_headers = {"authorization": authorization.strip(), "jsessionid": session_id.strip()}
    canonical_headers = "".join(f"{key}:{value}\n" for key, value in sorted(normalized_headers.items()))
    path = request_path if request_path.endswith("/") else request_path + "/"
    encoded_body_hash = hashlib.sha256(b64url(body).encode("utf-8")).hexdigest().upper()
    request_signature = hashlib.sha256(f"{path}\n{canonical_headers}\n{encoded_body_hash}".encode("utf-8")).hexdigest().upper()
    now = int(time.time())
    jwt_header = {"kid": key_pair.key_id, "typ": "JWT_PRO", "alg": "ES256"}
    jwt_payload = {
        "iss": HUAWEI_AUTO_CLIENT_ID,
        "iat": now,
        "exp": now + 3600,
        "requestSignature": request_signature,
        "signedHeaders": "authorization;jsessionid;",
    }
    signing_input = f"{b64url(json.dumps(jwt_header, separators=(',', ':')).encode('utf-8'))}.{b64url(json.dumps(jwt_payload, separators=(',', ':')).encode('utf-8'))}"
    return "Bearer " + signing_input + "." + key_pair.sign_jwt_input(signing_input)


def _extract_public_key(response: Any) -> str | None:
    root = response if isinstance(response, ET.Element) else None
    if root is None and isinstance(response, str):
        try:
            root = ET.fromstring(response)
        except ET.ParseError:
            return None
    if root is None:
        return None
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag == "value" and element.text:
            return _public_key_pem(element.text.strip())
        if tag == "resourcecontent" and element.text:
            try:
                content = json.loads(element.text)
            except json.JSONDecodeError:
                continue
            public_key = content.get("public-key")
            if isinstance(public_key, str) and public_key:
                return _public_key_pem(public_key)
    return None


def _public_key_pem(value: str) -> str:
    if "BEGIN PUBLIC KEY" in value:
        return value
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(value[i : i + 64] for i in range(0, len(value), 64)) + "\n-----END PUBLIC KEY-----"


def _parse_form_response(value: Any) -> dict[str, str]:
    if not isinstance(value, str):
        raise HuaweiAuthError("form response parse", type(value).__name__)
    from urllib.parse import parse_qs

    fields = {key: items[0] for key, items in parse_qs(value, keep_blank_values=True).items() if items}
    if not fields:
        raise HuaweiAuthError("empty form response", value)
    return fields


def _raise_huawei_result_error(action: str, response: Any) -> None:
    if isinstance(response, dict):
        result_code = response.get("resultCode")
        if result_code is not None and str(result_code) != "0":
            raise HuaweiAuthError(action, response)
        return
    if isinstance(response, ET.Element):
        for element in response.iter():
            if element.tag.rsplit("}", 1)[-1] != "result":
                continue
            result_code = element.attrib.get("resultCode")
            if result_code is not None and result_code != "0":
                raise HuaweiAuthError(action, {"resultCode": result_code})
            return


def _decode_response(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace")
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return ET.fromstring(text)
        except ET.ParseError:
            return text


def _safe_summary(response: Any) -> str:
    if isinstance(response, dict):
        return json.dumps(response, ensure_ascii=False)
    if isinstance(response, str):
        return response
    return type(response).__name__


def _safe_response(response: Any) -> Any:
    if isinstance(response, dict):
        code_keys = ("resultCode", "errorCode", "returnCode")
        text_keys = ("msg", "message", "errorDesc")
        summary: dict[str, Any] = {key: response[key] for key in code_keys if key in response}
        for key in text_keys:
            if key not in response:
                continue
            value = response[key]
            summary[key] = f"str length={len(value)}" if isinstance(value, str) else type(value).__name__
        return summary
    if isinstance(response, str):
        return f"{type(response).__name__} length={len(response)}"
    return type(response).__name__
