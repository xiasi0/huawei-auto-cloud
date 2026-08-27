"""Phone-named account assets and long-lived device identity storage.

The account asset deliberately retains raw local diagnostic responses and is
therefore sensitive plain JSON. Only the reusable account password is encrypted
with the account's device-identity key. Home Assistant host administrators and
backup readers are the trusted boundary for the remaining asset contents.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import uuid
from typing import Any, Mapping, TYPE_CHECKING

from cryptography.fernet import Fernet

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


# Keep the Home Assistant Store major version stable. Payload shape changes
# intentionally require removing and re-adding the integration instead of an
# implicit Store migration.
ASSET_STORAGE_VERSION = 1
IDENTITY_STORAGE_VERSION = 1
IDENTITY_STORAGE_KEY = f"{DOMAIN}/device_identity.json"
_FALLBACK_ASSET_LOCKS: dict[str, asyncio.Lock] = {}


def asset_storage_key(asset_key: str) -> str:
    """Return the legacy-style per-account asset filename for local diagnosis."""
    safe_key = _safe_asset_key(asset_key)
    filename = safe_key if safe_key.endswith(".json") else f"{safe_key}.json"
    return f"{DOMAIN}/accounts/{filename}"


def account_subject_digest(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def encrypt_password(password: str, key: str) -> str:
    """Encrypt the only credential that is intentionally not plain diagnostic data."""
    return Fernet(key.encode("ascii")).encrypt(password.encode("utf-8")).decode("ascii")


class PhoneAssetStore:
    """Phone-named account asset with directly inspectable local diagnostics.

    This deliberately matches the legacy integration's local-debugging model:
    account, vehicle, gateway-session, resource, and raw response records remain top-level JSON
    fields. Home Assistant storage-file access is therefore a sensitive local
    administrative capability. Device identity encryption material remains in
    its separate identity file.
    """

    def __init__(self, hass: HomeAssistant, asset_key: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, ASSET_STORAGE_VERSION, asset_storage_key(asset_key))
        self._lock = _asset_lock(hass, asset_key)

    async def async_load(self) -> tuple[int, dict[str, Any]]:
        raw = await self._store.async_load()
        if not raw:
            return 0, {}
        revision = raw.get("revision")
        if not isinstance(revision, int) or revision < 0:
            raise ValueError("phone-named account asset is invalid")
        return revision, {
            key: value
            for key, value in raw.items()
            if key not in {"schema_version", "revision"}
        }

    async def async_save_if_revision(
        self,
        expected_revision: int,
        revision: int,
        payload: Mapping[str, Any],
    ) -> bool:
        """Write only when no concurrent config-flow/coordinator write won first."""
        async with self._lock:
            current = await self._store.async_load()
            current_revision = current.get("revision", 0) if isinstance(current, Mapping) else 0
            if current_revision != expected_revision:
                return False
            await self._async_write(revision, payload)
            return True

    async def async_remove(self) -> None:
        async with self._lock:
            await self._store.async_remove()

    async def _async_write(self, revision: int, payload: Mapping[str, Any]) -> None:
        await self._store.async_save({"schema_version": ASSET_STORAGE_VERSION, "revision": revision, **dict(payload)})


class IdentityStore:
    """Maps an account digest to its long-lived local device identity.

    This record deliberately outlives a ConfigEntry. Removing an integration
    clears account credentials but preserves the identity so a later setup for
    the same Huawei account does not look like a newly created client.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, IDENTITY_STORAGE_VERSION, IDENTITY_STORAGE_KEY)

    async def async_get_or_create(self, account_subject: str) -> dict[str, Any]:
        digest = account_subject_digest(account_subject)
        raw = await self._store.async_load()
        accounts = raw.get("accounts", {}) if isinstance(raw, dict) else {}
        stored = accounts.get(digest) if isinstance(accounts, dict) else None
        identity = dict(stored) if isinstance(stored, dict) else _new_identity()
        identity["account_subject_digest"] = digest
        _ensure_identity(identity)
        accounts = dict(accounts) if isinstance(accounts, dict) else {}
        accounts[digest] = _stored_identity(identity)
        await self._store.async_save({"accounts": accounts})
        return identity

    async def async_save(self, identity: Mapping[str, Any]) -> None:
        digest = identity.get("account_subject_digest")
        if not isinstance(digest, str) or not digest:
            raise ValueError("identity is not bound to an account")
        raw = await self._store.async_load()
        accounts = raw.get("accounts", {}) if isinstance(raw, dict) else {}
        accounts = dict(accounts) if isinstance(accounts, dict) else {}
        accounts[digest] = _stored_identity(identity)
        await self._store.async_save({"accounts": accounts})

def _new_identity() -> dict[str, Any]:
    device_id = str(uuid.uuid4()).upper()
    return {
        "device_id": device_id,
        "omp_device_id": device_id,
        "ivcs_device_id": device_id,
        "credential_key": Fernet.generate_key().decode("ascii"),
        "device_model": "iPhone",
        "native_device_model": "iPhone8,1",
    }


def _ensure_identity(identity: dict[str, Any]) -> None:
    if not isinstance(identity.get("credential_key"), str) or not identity["credential_key"]:
        identity["credential_key"] = Fernet.generate_key().decode("ascii")
    device_id = identity.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        device_id = str(uuid.uuid4()).upper()
        identity["device_id"] = device_id
    for key in ("omp_device_id", "ivcs_device_id"):
        if not isinstance(identity.get(key), str) or not identity[key]:
            identity[key] = device_id
    identity.setdefault("device_model", "iPhone")
    identity.setdefault("native_device_model", "iPhone8,1")


def _stored_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in identity.items() if key != "account_subject_digest"}


def _safe_asset_key(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", ".", "+", "@"} else "_" for char in str(value))
    return safe.strip(" .") or "unknown"


def _asset_lock(hass: HomeAssistant, asset_key: str) -> asyncio.Lock:
    """Return the one in-process lock for every writer of this account asset."""
    data = getattr(hass, "data", None)
    if isinstance(data, dict):
        locks = data.setdefault(f"{DOMAIN}_asset_locks", {})
        if isinstance(locks, dict):
            return locks.setdefault(asset_key, asyncio.Lock())
    return _FALLBACK_ASSET_LOCKS.setdefault(asset_key, asyncio.Lock())


def canonical_asset_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove derived per-vehicle copies from a directly inspectable asset.

    Raw discovery responses are preserved exactly once under ``vehicle_gateway``.
    The vehicle layer owns only the route binding and normalized static
    projection; raw list-item duplication is not persisted there.
    """
    canonical = deepcopy(dict(payload))
    vehicles = canonical.get("vehicles")
    if not isinstance(vehicles, Mapping):
        return canonical
    canonical["vehicles"] = {
        route_id: {
            key: value
            for key, value in vehicle.items()
            if key not in {"vehicle_id", "list_response_item"}
        }
        for route_id, vehicle in vehicles.items()
        if isinstance(vehicle, Mapping)
    }
    return canonical
