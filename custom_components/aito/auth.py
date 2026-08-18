from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

_LOGGER = logging.getLogger(__name__)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class P256KeyPair:
    private_pem: bytes
    public_key_b64: str
    key_id: str

    @classmethod
    def generate(cls) -> "P256KeyPair":
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return cls(
            private_pem=private_pem,
            public_key_b64=base64.b64encode(public_der).decode("ascii"),
            key_id=secrets.token_hex(16),
        )

    @classmethod
    def from_storage(cls, data: dict[str, Any]) -> "P256KeyPair | None":
        try:
            key_id = data.get("p256_key_id")
            public_key = data.get("p256_public_key")
            private_key = data.get("p256_private_key_pem")
            if not all(isinstance(value, str) and value for value in (key_id, public_key, private_key)):
                return None
            private_pem = base64.b64decode(private_key)
            loaded_private_key = serialization.load_pem_private_key(private_pem, password=None)
            derived_public_der = loaded_private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if base64.b64encode(derived_public_der).decode("ascii") != public_key:
                return None
            return cls(
                private_pem=private_pem,
                public_key_b64=public_key,
                key_id=key_id,
            )
        except Exception:
            return None

    def as_storage(self) -> dict[str, str]:
        return {
            "p256_key_id": self.key_id,
            "p256_public_key": self.public_key_b64,
            "p256_private_key_pem": base64.b64encode(self.private_pem).decode("ascii"),
        }

    def sign_jwt_input(self, signing_input: str) -> str:
        private_key = serialization.load_pem_private_key(self.private_pem, password=None)
        der_signature = private_key.sign(signing_input.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        r_value, s_value = decode_dss_signature(der_signature)
        signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        return b64url(signature)


def extract_credentials(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    token_source = response.get("oauthToken") if isinstance(response.get("oauthToken"), dict) else response
    result = {
        "access_token": token_source.get("accessToken"),
        "refresh_token": token_source.get("refreshToken"),
        "xid": response.get("sessionKey") or token_source.get("xid"),
        "session_key": response.get("sessionKey"),
        "session_key_expire_in": response.get("sessionKeyExpireIn"),
        "user_info": response.get("userInfo"),
        "service_info": response.get("serviceInfo"),
        "service_user_info": response.get("serviceUserInfo"),
        "service_login_status": response.get("serviceLoginStatus"),
    }
    found = {key: value for key, value in result.items() if value is not None}
    if found.get("access_token") and found.get("refresh_token") and found.get("xid"):
        return _prefer_session_key(found, response)

    best = found
    for value in response.values():
        nested = extract_credentials(value)
        if nested:
            merged = {**found, **nested}
            if merged.get("access_token") and merged.get("refresh_token") and merged.get("xid"):
                return _prefer_session_key(merged, response)
            if len(merged) > len(best):
                best = merged
    return _prefer_session_key(best, response)


def session_key_status(response: Any) -> str | None:
    if isinstance(response, dict):
        user_info = response.get("userInfo")
        if isinstance(user_info, dict) and user_info.get("sessionKeyStatus") is not None:
            return str(user_info["sessionKeyStatus"])
        for value in response.values():
            status = session_key_status(value)
            if status is not None:
                return status
    if isinstance(response, list):
        for value in response:
            status = session_key_status(value)
            if status is not None:
                return status
    return None


def needs_user_session_refresh(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    return (
        str(response.get("code")) in {"401", "100011"}
        or str(response.get("resultCode")) in {"1000019", "3001002"}
        or any(
            response.get(key) in {"xid is expired", "not login", "not logged in"}
            for key in ("msg", "message", "error", "error_description")
        )
    )


def _prefer_session_key(credentials: dict[str, Any], response: Any) -> dict[str, Any]:
    session_key = _find_session_key(response)
    if not session_key:
        return credentials
    return {**credentials, "xid": session_key, "session_key": session_key}


def _find_session_key(value: Any) -> str | None:
    if isinstance(value, dict):
        session_key = value.get("sessionKey")
        if isinstance(session_key, str) and session_key:
            return session_key
        for nested in value.values():
            found = _find_session_key(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_session_key(nested)
            if found:
                return found
    return None


def extract_vehicle_authorization(response: Any, enterprise_code: str = "SERES") -> str | None:
    tokens = _find_vehicle_tokens(response)
    for token in tokens:
        if not isinstance(token, dict):
            continue
        if token.get("enterpriseCode") == enterprise_code and token.get("accessToken"):
            return str(token["accessToken"])
    # Fallback: accept the first token carrying an access token regardless of
    # enterprise (LUXEED/CHERY/STELATO/MAEXTRO accounts will not match SERES).
    for token in tokens:
        if isinstance(token, dict) and token.get("accessToken"):
            return str(token["accessToken"])
    _LOGGER.debug(
        "AITO vehicle token lookup failed: looking_for=%s token_count=%s token_entries=%s",
        enterprise_code,
        len(tokens),
        [
            {
                "enterpriseCode": t.get("enterpriseCode") if isinstance(t, dict) else None,
                "keys": sorted(t.keys()) if isinstance(t, dict) else None,
                "has_access_token": bool(t.get("accessToken")) if isinstance(t, dict) else None,
            }
            for t in tokens
        ],
    )
    _LOGGER.debug(
        "AITO vehicle token lookup failed: response_shape=%s",
        _shape_summary(response, depth=0, max_depth=4),
    )
    return None


def extract_vehicle_enterprise_code(response: Any) -> str | None:
    """Return the enterprise code of the first usable vehicle token, if any."""
    for token in _find_vehicle_tokens(response):
        if isinstance(token, dict) and token.get("accessToken"):
            enterprise_code = token.get("enterpriseCode")
            return str(enterprise_code) if enterprise_code else None
    return None


# Keys whose values must never reach the logs, plus a structural fallback for
# any free-form strings (see _shape_summary).
_SENSITIVE_KEYS = frozenset({
    "accessToken", "refreshToken", "sessionKey", "password", "pw", "at", "rt",
    "authorization", "access_token", "token", "ticket", "vin", "plateNo",
    "licensePlate", "phone", "mobileNumber", "mobile", "idNumber", "idType",
    "xid", "deviceId",
})


def _shape_summary(value: Any, depth: int, max_depth: int) -> Any:
    """Non-sensitive structural summary of the upstream response."""
    if depth > max_depth:
        return "..."
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            # Secret-bearing keys (and any free-form strings) are replaced by
            # a placeholder so DEBUG logs can never leak credentials or PII.
            if key in _SENSITIVE_KEYS or isinstance(item, str):
                out[key] = "<SECRET>"
            else:
                out[key] = _shape_summary(item, depth + 1, max_depth)
        return out
    if isinstance(value, list):
        if not value:
            return []
        inner = _shape_summary(value[0], depth + 1, max_depth)
        more = len(value) - 1
        return [inner, f"...+{more} more"] if more else [inner]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 60:
            return value[:24] + "...(len=%d)" % len(value)
        return value
    return type(value).__name__


def _find_vehicle_tokens(value: Any) -> list[Any]:
    if isinstance(value, dict):
        token_list = value.get("vehicleTokenInfoList")
        # An EMPTY list must fall through to the flat-token branch below
        # (vehicle/refresh returns a flat object, not a list).
        if token_list:
            return token_list
        # /account/vehicle/refresh returns a flat single-token object
        # (top-level accessToken + enterpriseCode) instead of a list.
        if value.get("accessToken") is not None:
            return [value]
        for nested in value.values():
            found = _find_vehicle_tokens(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_vehicle_tokens(nested)
            if found:
                return found
    return []
