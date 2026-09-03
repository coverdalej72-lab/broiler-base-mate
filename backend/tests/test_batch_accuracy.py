"""Batch Accuracy Tracker (Feb 2026) — GET /api/eob/batch-accuracy +
POST /api/eob/lock-batch fingerprint.predictedEob round-trip."""
import os
import re
import uuid
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

FARM = "default"


def _magic_url():
    content = Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
    m = re.search(r"(https://[^\s`]*?/api/auth/owner-magic\?key=[^\s`]+)", content)
    if not m:
        pytest.skip("owner-magic link not found in test_credentials.md")
    return m.group(1)


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    r = s.get(_magic_url(), allow_redirects=True, timeout=60)
    if r.status_code >= 400 or "session_token" not in s.cookies.get_dict():
        pytest.fail(f"owner-magic login failed: {r.status_code} cookies={s.cookies.get_dict()}")
    return s


@pytest.fixture(scope="module")
def created_batches():
    ids = []
    yield ids
    # cleanup — remove TEST_ snapshots straight from Mongo
    try:
        from pymongo import MongoClient
        env = dotenv_values("/app/backend/.env")
        cl = MongoClient(env["MONGO_URL"])
        cl[env["DB_NAME"]].eob_snapshots.delete_many({"batchIdentifier": {"$in": ids}})
    except Exception as e:  # pragma: no cover
        print(f"cleanup failed: {e}")


def _report(**over):
    rep = {
        "batchName": "TEST batch",
        "totalPlaced": 40000,
        "totalMorts": 900,
        "totalCaught": 39000,
        "aveWeight": 2.55,
        "fcr": 1.62,
        "cfcr": 1.593,
        "cageRating": 0.98,
        "correctedAge": 40.2,
        "actualAge": 39.0,
        "totalLiveWeightKg": 99450.0,
    }
    rep.update(over)
    return rep


PREDICTED = {
    "capturedAt": "2026-07-01T00:00:00.000Z",
    "finishAge": 40,
    "aveWeightKg": 2.5,
    "liveWeightKg": 97500,
    "fcr": 1.6,
    "cfcr": 1.587,
    "cage": 0.988,
    "grade": "B",
    "totalBirds": 39000,
}


class TestBatchAccuracyEndpoint:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/eob/batch-accuracy?farm={FARM}", timeout=30)
        assert r.status_code in (401, 403), r.text[:300]

    def test_returns_list(self, client):
        r = client.get(f"{BASE_URL}/api/eob/batch-accuracy?farm={FARM}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert isinstance(data, list)
        for row in data:
            assert set(["batchIdentifier", "lockedAt", "predicted", "actual"]).issubset(row.keys())
            assert "_id" not in row
            assert "html" not in row and "pdfBase64" not in row

    def test_lock_with_prediction_then_accuracy(self, client, created_batches):
        batch = f"TEST_ACC_{uuid.uuid4().hex[:8]}"
        created_batches.append(batch)
        r = client.post(
            f"{BASE_URL}/api/eob/lock-batch",
            json={
                "batchIdentifier": batch,
                "farmSlug": FARM,
                "farmName": "TEST Farm",
                "report": _report(batchName=batch),
                "fingerprint": {"predictedEob": PREDICTED},
            },
            timeout=180,
        )
        assert r.status_code == 200, r.text[:500]
        body = r.json()
        assert body.get("ok") is True
        assert body.get("alreadyLocked") is False

        acc = client.get(f"{BASE_URL}/api/eob/batch-accuracy?farm={FARM}", timeout=60)
        assert acc.status_code == 200, acc.text[:300]
        rows = acc.json()
        row = next((x for x in rows if x["batchIdentifier"] == batch), None)
        assert row is not None, f"locked batch {batch} missing from accuracy list"
        assert row["predicted"]["aveWeightKg"] == 2.5
        assert row["predicted"]["grade"] == "B"
        assert row["predicted"]["fcr"] == 1.6
        assert row["actual"]["aveWeight"] == 2.55
        assert row["actual"]["fcr"] == 1.62
        assert row["actual"]["cageRating"] == 0.98
        assert row["actual"]["correctedAge"] == 40.2
        assert row["actual"]["totalCaught"] == 39000
        assert row["lockedAt"]

    def test_lock_without_prediction_yields_null_predicted(self, client, created_batches):
        batch = f"TEST_NOPRED_{uuid.uuid4().hex[:8]}"
        created_batches.append(batch)
        r = client.post(
            f"{BASE_URL}/api/eob/lock-batch",
            json={
                "batchIdentifier": batch,
                "farmSlug": FARM,
                "report": _report(batchName=batch),
            },
            timeout=180,
        )
        assert r.status_code == 200, r.text[:500]

        rows = client.get(f"{BASE_URL}/api/eob/batch-accuracy?farm={FARM}", timeout=60).json()
        row = next((x for x in rows if x["batchIdentifier"] == batch), None)
        assert row is not None
        assert row["predicted"] is None

    def test_farm_isolation(self, client, created_batches):
        """A batch locked on `default` must not surface on another farm slug."""
        rows = client.get(f"{BASE_URL}/api/eob/batch-accuracy?farm=north-creek", timeout=60)
        assert rows.status_code == 200, rows.text[:300]
        ids = [x["batchIdentifier"] for x in rows.json()]
        for b in created_batches:
            assert b not in ids

    def test_unknown_farm(self, client):
        r = client.get(f"{BASE_URL}/api/eob/batch-accuracy?farm=does-not-exist-xyz", timeout=60)
        assert r.status_code in (200, 403, 404), r.text[:300]
        if r.status_code == 200:
            assert r.json() == []
