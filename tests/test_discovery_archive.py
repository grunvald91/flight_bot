from pathlib import Path

from bot.services.discovery_archive import DiscoveryArchive


def _payload():
    return {
        "offers": [{
            "price": {"amount": 4000, "currency": "RUB"},
            "_discovery": {"city": "Стамбул", "country": "Турция"},
            "checkout_ref": {"opaque": "SECRET_REFERENCE"},
            "variants": [{"conditions": {"baggage": {"pieces": 0, "provider_ref": "DEEP_SECRET"},
                                         "checkout_ref": "DEEP_REFERENCE"},
                          "checkout_ref": "NESTED_SECRET"}],
            "legs": [{"from": "Москва", "to": "Стамбул", "segments": []}],
        }],
        "meta": {"origin": "MOW", "departure_date": "2026-10-01", "unverified": 7},
    }


def test_archive_survives_new_instance_without_checkout_capability(tmp_path):
    db = tmp_path / "private" / "results.sqlite3"
    archive = DiscoveryArchive(db, ttl_seconds=86400, clock=lambda: 1000.0)
    archive.put("opaque-key", _payload())
    reopened = DiscoveryArchive(db, ttl_seconds=86400, clock=lambda: 1001.0)
    result = reopened.get("opaque-key")
    assert result["offers"][0]["price"]["amount"] == 4000
    assert result["meta"]["unverified"] == 7
    assert "checkout_ref" not in str(result)
    assert "SECRET_REFERENCE" not in db.read_bytes().decode("utf-8", errors="ignore")
    assert "NESTED_SECRET" not in db.read_bytes().decode("utf-8", errors="ignore")
    assert "DEEP_SECRET" not in db.read_bytes().decode("utf-8", errors="ignore")
    assert "DEEP_REFERENCE" not in db.read_bytes().decode("utf-8", errors="ignore")


def test_archive_expires_from_original_write_not_process_restart(tmp_path):
    db = tmp_path / "results.sqlite3"
    DiscoveryArchive(db, ttl_seconds=86400, clock=lambda: 1000.0).put("key", _payload())
    assert DiscoveryArchive(db, ttl_seconds=86400, clock=lambda: 1000.0 + 86399).get("key")
    assert DiscoveryArchive(db, ttl_seconds=86400, clock=lambda: 1000.0 + 86401).get("key") is None


def test_archive_rejects_invalid_key_without_touching_storage(tmp_path):
    db = tmp_path / "results.sqlite3"
    archive = DiscoveryArchive(db)
    assert archive.get("key/../../profile") is None
    assert not db.exists()
