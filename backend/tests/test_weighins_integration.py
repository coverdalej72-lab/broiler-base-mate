"""Tests for the new /api/integrations/weighins endpoints (Weigh Birds tab).

Idempotent upsert on (farm, shed, age), farm-token auth, and cleanup of TEST_
data at the end.
"""
import os
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
FARM = "north-creek"
TOKEN = "JA-018MvYOhNc8hBnbfJ1DU0dxeaIiQ_"

WEIGHINS_URL = f"{BASE_URL}/api/integrations/weighins"


@pytest.fixture(scope="module")
def created_keys():
    """Track (shed, age) pairs created during tests for cleanup via Mongo direct."""
    keys = []
    yield keys
    # Cleanup via direct Mongo
    try:
        from pymongo import MongoClient
        client = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        db = client[os.environ.get("DB_NAME", "test_database")]
        for shed, age in keys:
            db["external_weighins"].delete_one({"farmId": FARM, "shed": shed, "age": age})
        client.close()
    except Exception as e:
        print(f"Cleanup warning: {e}")


# --- Auth ------------------------------------------------------------------
class TestWeighInsAuth:
    def test_post_without_token_returns_401(self):
        r = requests.post(
            f"{WEIGHINS_URL}?farm={FARM}",
            json={"entries": [{"shed": 9, "age": 21, "date": "2026-01-15", "weightGrams": 950}]},
        )
        assert r.status_code == 401, r.text

    def test_post_wrong_token_returns_401(self):
        r = requests.post(
            f"{WEIGHINS_URL}?farm={FARM}&t=wrong-token-xyz",
            json={"entries": [{"shed": 9, "age": 21, "date": "2026-01-15", "weightGrams": 950}]},
        )
        assert r.status_code == 401

    def test_get_without_token_returns_401(self):
        r = requests.get(f"{WEIGHINS_URL}?farm={FARM}")
        assert r.status_code == 401


# --- Idempotent upsert on (farm, shed, age) --------------------------------
class TestWeighInsUpsert:
    def test_post_valid_entry_succeeds(self, created_keys):
        r = requests.post(
            f"{WEIGHINS_URL}?farm={FARM}&t={TOKEN}",
            json={
                "entries": [
                    {"shed": 9, "age": 21, "date": "2026-01-15", "weightGrams": 1050.0, "staffName": "TEST_alice"}
                ],
                "source": "test-suite",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["upserted"] == 1
        created_keys.append((9, 21))

    def test_get_returns_entry(self):
        r = requests.get(f"{WEIGHINS_URL}?farm={FARM}&t={TOKEN}")
        assert r.status_code == 200
        entries = r.json()["entries"]
        match = [e for e in entries if e["shed"] == 9 and e["age"] == 21]
        assert len(match) == 1
        assert match[0]["weightGrams"] == 1050.0
        assert match[0]["staffName"] == "TEST_alice"
        # ensure no mongo _id leak
        assert "_id" not in match[0]

    def test_reupsert_same_key_overwrites(self, created_keys):
        # Save Shed 9/Age 21 again with different weight
        r = requests.post(
            f"{WEIGHINS_URL}?farm={FARM}&t={TOKEN}",
            json={"entries": [{"shed": 9, "age": 21, "date": "2026-01-15", "weightGrams": 1200.0, "staffName": "TEST_bob"}]},
        )
        assert r.status_code == 200
        # Verify only one entry remains with the latest weight
        r2 = requests.get(f"{WEIGHINS_URL}?farm={FARM}&t={TOKEN}")
        entries = r2.json()["entries"]
        match = [e for e in entries if e["shed"] == 9 and e["age"] == 21]
        assert len(match) == 1, f"Expected 1 entry, got {len(match)}"
        assert match[0]["weightGrams"] == 1200.0
        assert match[0]["staffName"] == "TEST_bob"

    def test_different_age_creates_separate_entry(self, created_keys):
        r = requests.post(
            f"{WEIGHINS_URL}?farm={FARM}&t={TOKEN}",
            json={"entries": [{"shed": 9, "age": 28, "date": "2026-01-22", "weightGrams": 1600.0}]},
        )
        assert r.status_code == 200
        created_keys.append((9, 28))
        r2 = requests.get(f"{WEIGHINS_URL}?farm={FARM}&t={TOKEN}")
        entries = r2.json()["entries"]
        shed9 = [e for e in entries if e["shed"] == 9]
        assert len(shed9) >= 2


# --- Validation ------------------------------------------------------------
class TestWeighInsValidation:
    def test_empty_entries_returns_400(self):
        r = requests.post(f"{WEIGHINS_URL}?farm={FARM}&t={TOKEN}", json={"entries": []})
        assert r.status_code == 400

    def test_zero_weight_skipped(self, created_keys):
        r = requests.post(
            f"{WEIGHINS_URL}?farm={FARM}&t={TOKEN}",
            json={"entries": [{"shed": 12, "age": 10, "date": "2026-01-04", "weightGrams": 0}]},
        )
        # endpoint returns 200 with upserted=0 (weightGrams<=0 skipped)
        assert r.status_code == 200
        assert r.json()["upserted"] == 0
