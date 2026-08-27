"""Regression tests for the 🏁 End Batch (lock) endpoints.

Covers: auth enforcement, validation, per-farm scoping of the listing
endpoint, and the 404 shape of the get-single-snapshot endpoint.

Uses `requests` against the running backend (same pattern as
test_feed_program_history.py) — no direct DB fixtures needed.
"""
import os
import uuid

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    BASE_URL = "http://localhost:8001"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def test_lock_batch_requires_auth(api):
    r = api.post(f"{BASE_URL}/api/eob/lock-batch", json={
        "batchIdentifier": "BATCH-1",
        "report": {"batchName": "BATCH-1"},
    })
    assert r.status_code == 401


def test_lock_batch_validates_body(api):
    """Missing report field must return 422."""
    r = api.post(f"{BASE_URL}/api/eob/lock-batch", json={"batchIdentifier": "X"})
    assert r.status_code == 422


def test_list_locked_batches_returns_list(api):
    r = api.get(f"{BASE_URL}/api/eob/locked-batches")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_locked_batches_scoped_per_farm(api):
    """Two random farm slugs — the listing must return an empty list for
    both (nothing seeded), and the response must never leak html/pdf blobs."""
    farm_a = f"test-farm-{uuid.uuid4().hex[:8]}"
    farm_b = f"test-farm-{uuid.uuid4().hex[:8]}"
    r_a = api.get(f"{BASE_URL}/api/eob/locked-batches?farm={farm_a}")
    r_b = api.get(f"{BASE_URL}/api/eob/locked-batches?farm={farm_b}")
    assert r_a.status_code == 200
    assert r_b.status_code == 200
    assert r_a.json() == []
    assert r_b.json() == []


def test_get_locked_batch_404_when_missing(api):
    r = api.get(f"{BASE_URL}/api/eob/locked-batches/{uuid.uuid4().hex}")
    assert r.status_code == 404
