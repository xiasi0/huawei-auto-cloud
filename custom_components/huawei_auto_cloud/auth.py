from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


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


def extract_enterprise_authorizations(response: Any) -> dict[str, str]:
    """Return only explicit enterprise/token pairs from an auth response.

    There is intentionally no first-token fallback: an unlabelled token cannot
    be routed safely in a multi-enterprise account.
    """
    result: dict[str, str] = {}
    for token in _find_vehicle_tokens(response):
        if not isinstance(token, dict):
            continue
        enterprise = token.get("enterpriseCode")
        authorization = token.get("accessToken")
        if isinstance(enterprise, str) and enterprise and isinstance(authorization, str) and authorization:
            result[enterprise] = authorization
    return result


def extract_refreshed_enterprise_authorization(response: Any, enterprise_code: str) -> str | None:
    """Extract a refresh result for the enterprise explicitly sent in the request.

    A vehicle-refresh response may be either a normal labelled token record or
    a flat ``{"accessToken": ...}`` response. The latter is safe to accept
    only here: the caller sent one explicit enterprise code in its request and
    rejects a conflicting response enterprise code. It is not a token fallback
    and is never used to discover a new enterprise.
    """
    authorization = extract_enterprise_authorizations(response).get(enterprise_code)
    if authorization:
        return authorization
    if not isinstance(response, dict):
        return None
    response_enterprise = response.get("enterpriseCode")
    access_token = response.get("accessToken")
    if response_enterprise not in {None, "", enterprise_code}:
        return None
    return str(access_token) if isinstance(access_token, str) and access_token else None


def _find_vehicle_tokens(value: Any) -> list[Any]:
    if isinstance(value, dict):
        token_list = value.get("vehicleTokenInfoList")
        if isinstance(token_list, list):
            return token_list
        if value.get("enterpriseCode") is not None and value.get("accessToken") is not None:
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
