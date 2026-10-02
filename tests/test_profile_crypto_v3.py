import base64
import json
import secrets

import pytest
from cryptography.exceptions import InvalidTag
from bot.services.passenger_crypto import ProfileCrypto


def _profile():
    return {"passenger": {"first_name": "SENTINEL_PRIVATE", "document": {"number": "SENTINEL_DOC"}},
            "customer": {"email": "private@example.invalid"}, "consent": {"storage": True}}


def test_each_profile_has_distinct_dek_and_every_field_is_encrypted():
    crypto = ProfileCrypto(secrets.token_bytes(32))
    first = crypto.encrypt(_profile(), owner_id=42)
    second = crypto.encrypt(_profile(), owner_id=42)
    a, b = crypto.metadata(first), crypto.metadata(second)
    assert a["profile_id"] != b["profile_id"]
    assert a["wrapped_dek"] != b["wrapped_dek"]
    assert set(a["fields"]) == {"passenger.first_name", "passenger.document.number", "customer.email", "consent.storage"}
    assert b"SENTINEL" not in first and b"private@example" not in first
    assert crypto.decrypt(first, owner_id=42) == _profile()
    with pytest.raises(InvalidTag):
        crypto.decrypt(first, owner_id=43)


def test_aad_rejects_profile_field_schema_and_ciphertext_mutations():
    crypto = ProfileCrypto(secrets.token_bytes(32))
    original = crypto.encrypt(_profile(), owner_id=42)
    data = crypto.metadata(original)
    fields = data["fields"]
    fields["passenger.first_name"], fields["passenger.document.number"] = (
        fields["passenger.document.number"], fields["passenger.first_name"])
    with pytest.raises(InvalidTag):
        crypto.decrypt(b"PP3" + json.dumps(data).encode(), owner_id=42)
    data = crypto.metadata(original)
    del data["fields"]["customer.email"]
    with pytest.raises(InvalidTag):
        crypto.decrypt(b"PP3" + json.dumps(data).encode(), owner_id=42)
    for key, replacement in (("profile_id", secrets.token_hex(16)), ("schema", 4)):
        data = crypto.metadata(original)
        data[key] = replacement
        with pytest.raises((InvalidTag, ValueError)):
            crypto.decrypt(b"PP3" + json.dumps(data).encode(), owner_id=42)
    data = crypto.metadata(original)
    raw = bytearray(base64.urlsafe_b64decode(data["fields"]["customer.email"]))
    raw[-1] ^= 1
    data["fields"]["customer.email"] = base64.urlsafe_b64encode(raw).decode()
    with pytest.raises(InvalidTag):
        crypto.decrypt(b"PP3" + json.dumps(data).encode(), owner_id=42)


def test_rotation_reads_old_writes_new_and_keeps_fields_and_identity():
    old_key, new_key = secrets.token_bytes(32), secrets.token_bytes(32)
    old = ProfileCrypto(old_key, kek_id="old")
    envelope = old.encrypt(_profile(), owner_id=42)
    rotating = ProfileCrypto(new_key, kek_id="new", old_keys={"old": old_key})
    assert rotating.decrypt(envelope, owner_id=42) == _profile()
    rotated = rotating.rewrap(envelope, owner_id=42)
    before, after = old.metadata(envelope), rotating.metadata(rotated)
    assert before["fields"] == after["fields"]
    assert before["profile_id"] == after["profile_id"]
    assert after["kek_id"] == "new"
    assert ProfileCrypto(new_key, kek_id="new").decrypt(rotated, owner_id=42) == _profile()
    with pytest.raises(ValueError):
        old.decrypt(rotated, owner_id=42)


def test_legacy_and_missing_keys_fail_closed():
    crypto = ProfileCrypto(secrets.token_bytes(32))
    with pytest.raises(ValueError, match="migration required"):
        crypto.decrypt(b"PP2" + secrets.token_bytes(48), owner_id=42)
    old = ProfileCrypto(secrets.token_bytes(32), kek_id="old")
    envelope = old.encrypt(_profile(), owner_id=42)
    with pytest.raises(ValueError, match="KEK unavailable"):
        crypto.decrypt(envelope, owner_id=42)


def test_systemd_credential_keyring_rotation(tmp_path, monkeypatch):
    old_key, new_key = secrets.token_bytes(32), secrets.token_bytes(32)
    old_envelope = ProfileCrypto(old_key).encrypt(_profile(), owner_id=42)
    ring = {"active": "2", "keys": {
        "1": base64.urlsafe_b64encode(old_key).decode(),
        "2": base64.urlsafe_b64encode(new_key).decode(),
    }}
    (tmp_path / "profile_kek").write_text(json.dumps(ring), encoding="ascii")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    crypto = ProfileCrypto.from_environment()
    assert crypto.decrypt(old_envelope, owner_id=42) == _profile()
    rotated = crypto.rewrap(old_envelope, owner_id=42)
    assert ProfileCrypto(new_key, kek_id="2").decrypt(rotated, owner_id=42) == _profile()
