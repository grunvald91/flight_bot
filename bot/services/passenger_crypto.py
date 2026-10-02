import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SCHEMA_VERSION = 3
PREFIX = b"PP3"


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _decode(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Invalid encrypted profile")
    return base64.b64decode(value, altchars=b"-_", validate=True)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ProfileCrypto:
    """One random DEK per envelope, independently authenticated fields, versioned KEK wrapping."""

    def __init__(self, key: bytes, *, kek_id: str = "1", old_keys: dict[str, bytes] | None = None):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("PROFILE_ENCRYPTION_KEY must decode to exactly 32 bytes")
        if not kek_id or not kek_id.isascii() or len(kek_id) > 64:
            raise ValueError("Invalid KEK identifier")
        self.kek_id = kek_id
        self._keys = dict(old_keys or {})
        self._keys[kek_id] = key
        if any(not isinstance(k, bytes) or len(k) != 32 for k in self._keys.values()):
            raise ValueError("Every KEK must be 32 bytes")
        self._key = key  # Preserves existing fingerprint derivation across key migration only with same active key.

    @classmethod
    def from_environment(cls) -> "ProfileCrypto":
        credential_dir = os.getenv("CREDENTIALS_DIRECTORY")
        if credential_dir:
            try:
                value = (Path(credential_dir) / "profile_kek").read_bytes()
                if not value or len(value) > 8192:
                    raise ValueError("Invalid credential")
                text = value.decode("ascii").strip()
                if text.startswith("{"):
                    ring = json.loads(text)
                    active = ring["active"]
                    decoded = {name: _decode(encoded) for name, encoded in ring["keys"].items()}
                    return cls(decoded[active], kek_id=active, old_keys=decoded)
                return cls(_decode(text), kek_id="1")
            except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError("Profile encryption credential is invalid") from None
        configured = os.getenv("PROFILE_ENCRYPTION_KEYS")
        if configured:
            try:
                keys = json.loads(configured)
                if not isinstance(keys, dict) or not keys:
                    raise ValueError("Invalid keyring")
                decoded = {kid: _decode(value) for kid, value in keys.items()}
                active = os.environ["PROFILE_ENCRYPTION_ACTIVE_KEK"]
                return cls(decoded[active], kek_id=active, old_keys=decoded)
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError("Profile encryption keyring is invalid") from exc
        encoded = os.getenv("PROFILE_ENCRYPTION_KEY", "")
        if not encoded:
            raise RuntimeError("PROFILE_ENCRYPTION_KEY is not configured")
        try:
            return cls(_decode(encoded), kek_id="1")
        except ValueError as exc:
            raise RuntimeError("PROFILE_ENCRYPTION_KEY is invalid") from exc

    @staticmethod
    def _aad(owner_id: int, profile_id: str, field: str, purpose: str) -> bytes:
        return _canonical(["flight-bot", SCHEMA_VERSION, owner_id, profile_id, field, purpose])

    @classmethod
    def _dek_aad(cls, owner_id: int, data: dict) -> bytes:
        # Authenticate the field inventory too: deleting an encrypted column
        # must fail rather than silently producing an incomplete profile.
        return cls._aad(owner_id, data["profile_id"],
                        "dek:" + ",".join(sorted(data["fields"])), data["purpose"])

    @staticmethod
    def metadata(envelope: bytes) -> dict:
        if not isinstance(envelope, bytes) or not envelope.startswith(PREFIX) or len(envelope) > 65536:
            raise ValueError("Unsupported passenger profile envelope; explicit migration required")
        try:
            data = json.loads(envelope[len(PREFIX):])
            if (not isinstance(data, dict) or set(data) != {"schema", "profile_id", "kek_id", "wrapped_dek", "fields", "purpose"}
                    or data["schema"] != SCHEMA_VERSION or not isinstance(data["profile_id"], str)
                    or len(data["profile_id"]) != 32 or not all(c in "0123456789abcdef" for c in data["profile_id"])
                    or not isinstance(data["kek_id"], str) or not data["kek_id"]
                    or data["purpose"] not in {"passenger-profile", "checkout-result"}
                    or not isinstance(data["fields"], dict) or not data["fields"]):
                raise ValueError("Invalid encrypted profile metadata")
            if len(_decode(data["wrapped_dek"])) < 28:
                raise ValueError("Invalid wrapped key")
            return data
        except (UnicodeError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid encrypted profile metadata") from exc

    def _unwrap(self, data: dict, owner_id: int) -> bytes:
        key = self._keys.get(data["kek_id"])
        if key is None:
            raise ValueError("Profile KEK unavailable; refusing decryption")
        wrapped = _decode(data["wrapped_dek"])
        dek = AESGCM(key).decrypt(wrapped[:12], wrapped[12:],
                                  self._dek_aad(owner_id, data))
        if len(dek) != 32:
            raise ValueError("Invalid decrypted profile key")
        return dek

    def encrypt(self, payload: dict, *, owner_id: int, purpose: str = "passenger-profile", profile_id: str | None = None) -> bytes:
        if purpose not in {"passenger-profile", "checkout-result"} or not isinstance(payload, dict) or not payload:
            raise ValueError("Unsupported encryption payload or purpose")
        profile_id = profile_id or secrets.token_hex(16)
        if len(profile_id) != 32 or any(c not in "0123456789abcdef" for c in profile_id):
            raise ValueError("Invalid profile identifier")
        dek = secrets.token_bytes(32)
        fields = {}

        def visit(obj: dict, prefix: str = "") -> None:
            for name, value in obj.items():
                if not isinstance(name, str) or not name or "." in name:
                    raise ValueError("Invalid profile field")
                path = f"{prefix}.{name}" if prefix else name
                if isinstance(value, dict):
                    if not value:
                        raise ValueError("Empty profile section")
                    visit(value, path)
                else:
                    nonce = secrets.token_bytes(12)
                    fields[path] = _encode(nonce + AESGCM(dek).encrypt(
                        nonce, _canonical(value), self._aad(owner_id, profile_id, path, purpose)))
        visit(payload)
        wrap_nonce = secrets.token_bytes(12)
        manifest = {"profile_id": profile_id, "purpose": purpose, "fields": fields}
        wrapped = wrap_nonce + AESGCM(self._keys[self.kek_id]).encrypt(
            wrap_nonce, dek, self._dek_aad(owner_id, manifest))
        return PREFIX + _canonical({"schema": SCHEMA_VERSION, "profile_id": profile_id,
                                    "kek_id": self.kek_id, "wrapped_dek": _encode(wrapped),
                                    "fields": fields, "purpose": purpose})

    def decrypt(self, envelope: bytes, *, owner_id: int, purpose: str = "passenger-profile") -> dict:
        data = self.metadata(envelope)
        if data["purpose"] != purpose:
            raise ValueError("Encryption purpose mismatch")
        dek = self._unwrap(data, owner_id)
        result = {}
        for path, encoded in data["fields"].items():
            if not isinstance(path, str) or any(not part or part.startswith("_") for part in path.split(".")):
                raise ValueError("Invalid encrypted field")
            ciphertext = _decode(encoded)
            value = json.loads(AESGCM(dek).decrypt(
                ciphertext[:12], ciphertext[12:],
                self._aad(owner_id, data["profile_id"], path, purpose)).decode("utf-8"))
            target = result
            parts = path.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
                if not isinstance(target, dict):
                    raise ValueError("Conflicting profile fields")
            if parts[-1] in target:
                raise ValueError("Duplicate profile field")
            target[parts[-1]] = value
        return result

    def rewrap(self, envelope: bytes, *, owner_id: int) -> bytes:
        """Rotate the KEK without touching DEK, fields, profile identity or profile version."""
        data = self.metadata(envelope)
        dek = self._unwrap(data, owner_id)
        nonce = secrets.token_bytes(12)
        data["wrapped_dek"] = _encode(nonce + AESGCM(self._keys[self.kek_id]).encrypt(
            nonce, dek, self._dek_aad(owner_id, data)))
        data["kek_id"] = self.kek_id
        return PREFIX + _canonical(data)

    def fingerprint(self, payload: dict, *, owner_id: int, purpose: str) -> str:
        canonical = _canonical(payload)
        key = hmac.new(self._key, f"fingerprint|{owner_id}|{purpose}".encode("ascii"), hashlib.sha256).digest()
        return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def generate_key() -> str:
    return _encode(secrets.token_bytes(32))
