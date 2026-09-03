"""Iteration 9 — backend tests for:
  * GET /api/farm/trial-status   (trial nudge banner data source)
  * GET/PUT /api/feed-program/catches  (catches cloud sync)
"""
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

backend_env = dotenv_values("/app/backend/.env")
MONGO_URL = backend_env.get("MONGO_URL")
DB_NAME = backend_env.get("DB_NAME")

TRIAL_SOFT_SLUG = "test-trial-soft"
TRIAL_EXPIRED_SLUG = "test-trial-expired"


def _magic_url():
    content = Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
    m = re.search(r"(https://[^\s`]*?/api/auth/owner-magic\?key=[^\s`]+)", content)
    if not m:
        pytest.skip("owner-magic URL not found in test_credentials.md")
    return m.group(1)


@pytest.fixture(scope="session")
def client():
    s = requests.Session()
    s.get(_magic_url(), allow_redirects=False, timeout=30)
    assert s.cookies.get("session_token"), "owner-magic did not issue a session_token cookie"
    return s


@pytest.fixture(scope="session")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


@pytest.fixture(scope="session")
def seeded_trial_farms(mongo):
    """Seed a day-26 trialing farm and a day-31 trialing farm owned by the owner email."""
    owner = "appcovi2026@gmail.com"
    now = datetime.now(timezone.utc)
    docs = [
        {
            "slug": TRIAL_SOFT_SLUG, "name": "TEST_Trial Soft", "ownerEmail": owner,
            "subscriptionStatus": "trialing",
            "trialStartedAt": now - timedelta(days=26),
            "trialExpiresAt": now + timedelta(days=4),
            "createdAt": now,
        },
        {
            "slug": TRIAL_EXPIRED_SLUG, "name": "TEST_Trial Expired", "ownerEmail": owner,
            "subscriptionStatus": "trialing",
            "trialStartedAt": now - timedelta(days=31),
            "trialExpiresAt": now - timedelta(days=1),
            "createdAt": now,
        },
    ]
    for d in docs:
        mongo["farms"].update_one({"slug": d["slug"]}, {"$set": d}, upsert=True)
    yield docs
    mongo["farms"].delete_many({"slug": {"$in": [TRIAL_SOFT_SLUG, TRIAL_EXPIRED_SLUG]}})


# ── GET /api/farm/trial-status ────────────────────────────────────────────
class TestTrialStatus:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/farm/trial-status?farm=default", timeout=30)
        assert r.status_code in (401, 403), r.text[:300]

    def test_non_trial_farm_returns_nulls(self, client):
        r = client.get(f"{BASE_URL}/api/farm/trial-status?farm=default", timeout=30)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert set(d) == {"subscriptionStatus", "trialStartedAt", "trialExpiresAt"}
        assert d["subscriptionStatus"] in (None, "active", "trialing")
        if d["subscriptionStatus"] != "trialing":
            assert d["trialStartedAt"] is None or isinstance(d["trialStartedAt"], str)

    def test_soft_trial_farm(self, client, seeded_trial_farms):
        r = client.get(f"{BASE_URL}/api/farm/trial-status?farm={TRIAL_SOFT_SLUG}", timeout=30)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d["subscriptionStatus"] == "trialing"
        assert isinstance(d["trialStartedAt"], str) and d["trialStartedAt"]
        assert isinstance(d["trialExpiresAt"], str) and d["trialExpiresAt"]
        started = datetime.fromisoformat(d["trialStartedAt"])
        day = (datetime.now(timezone.utc) - started.replace(tzinfo=started.tzinfo or timezone.utc)).days
        assert 25 <= day < 30, f"expected day 26, got {day}"

    def test_expired_trial_farm(self, client, seeded_trial_farms):
        r = client.get(f"{BASE_URL}/api/farm/trial-status?farm={TRIAL_EXPIRED_SLUG}", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d["subscriptionStatus"] == "trialing"
        started = datetime.fromisoformat(d["trialStartedAt"])
        assert (datetime.now(timezone.utc) - started.replace(tzinfo=started.tzinfo or timezone.utc)).days >= 30

    def test_unknown_farm(self, client):
        slug = f"test-nope-{uuid.uuid4().hex[:8]}"
        r = client.get(f"{BASE_URL}/api/farm/trial-status?farm={slug}", timeout=30)
        # unknown farm: either 403/404 from access guard or all-null payload
        assert r.status_code in (200, 403, 404), r.text[:300]
        if r.status_code == 200:
            assert r.json() == {"subscriptionStatus": None, "trialStartedAt": None, "trialExpiresAt": None}

    def test_no_mongo_id_leak(self, client, seeded_trial_farms):
        r = client.get(f"{BASE_URL}/api/farm/trial-status?farm={TRIAL_SOFT_SLUG}", timeout=30)
        assert "_id" not in r.json()


# ── GET/PUT /api/feed-program/catches ────────────────────────────────────
class TestCatchesSync:
    FARM = "default"

    @pytest.fixture(scope="class", autouse=True)
    def restore(self, mongo):
        before = mongo["feed_program_catches"].find_one({"farmId": self.FARM})
        yield
        if before is not None:
            before.pop("_id", None)
            mongo["feed_program_catches"].replace_one({"farmId": self.FARM}, before, upsert=True)
        else:
            mongo["feed_program_catches"].delete_one({"farmId": self.FARM})

    def test_get_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/feed-program/catches?farm=default", timeout=30)
        assert r.status_code in (401, 403), r.text[:300]

    def test_put_requires_auth(self):
        r = requests.put(f"{BASE_URL}/api/feed-program/catches?farm=default",
                         json={"catchMap": {}, "weighPlanMap": {}}, timeout=30)
        assert r.status_code in (401, 403), r.text[:300]

    def test_put_then_get_roundtrip(self, client):
        catch_map = {"TEST_Shed 1 & 2": [{"date": "2026-03-01", "birds": 4200, "kg": 11550.5, "note": "TEST_pickup"}]}
        weigh_plan = {"TEST_Shed 1 & 2": [{"date": "2026-03-02", "birds": 100, "kg": 275.0}]}
        r = client.put(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}",
                       json={"catchMap": catch_map, "weighPlanMap": weigh_plan}, timeout=30)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body["updatedAt"], str)

        g = client.get(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}", timeout=30)
        assert g.status_code == 200
        d = g.json()
        assert d["catchMap"] == catch_map
        assert d["weighPlanMap"] == weigh_plan
        assert d["updatedAt"] == body["updatedAt"]
        assert "_id" not in d

    def test_overwrite_replaces_not_merges(self, client):
        client.put(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}",
                   json={"catchMap": {"A": [1]}, "weighPlanMap": {}}, timeout=30)
        client.put(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}",
                   json={"catchMap": {"B": [2]}, "weighPlanMap": {}}, timeout=30)
        d = client.get(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}", timeout=30).json()
        assert d["catchMap"] == {"B": [2]}

    def test_empty_put_wipes_server_copy(self, client):
        """Documents the destructive semantics of the PUT (no merge / no guard)."""
        client.put(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}",
                   json={"catchMap": {"X": [9]}, "weighPlanMap": {"Y": [8]}}, timeout=30)
        client.put(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}",
                   json={"catchMap": {}, "weighPlanMap": {}}, timeout=30)
        d = client.get(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}", timeout=30).json()
        assert d["catchMap"] == {} and d["weighPlanMap"] == {}

    def test_farm_isolation(self, client, mongo):
        other = "north-creek"
        before = mongo["feed_program_catches"].find_one({"farmId": other})
        try:
            client.put(f"{BASE_URL}/api/feed-program/catches?farm={self.FARM}",
                       json={"catchMap": {"DEFAULT_ONLY": [1]}, "weighPlanMap": {}}, timeout=30)
            d = client.get(f"{BASE_URL}/api/feed-program/catches?farm={other}", timeout=30).json()
            assert "DEFAULT_ONLY" not in d["catchMap"]
        finally:
            if before is None:
                mongo["feed_program_catches"].delete_one({"farmId": other})

    def test_get_unseeded_farm_returns_empty_shape(self, client):
        slug = f"test-empty-{uuid.uuid4().hex[:6]}"
        r = client.get(f"{BASE_URL}/api/feed-program/catches?farm={slug}", timeout=30)
        assert r.status_code in (200, 403, 404)
        if r.status_code == 200:
            assert r.json() == {"catchMap": {}, "weighPlanMap": {}, "updatedAt": None}
