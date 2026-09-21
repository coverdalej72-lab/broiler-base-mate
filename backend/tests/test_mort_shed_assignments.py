"""Tests for Mort Buddy daily shed→staff-name assignments feature.

Endpoints under test:
  GET  /api/mort-buddy/assignments?farm=<slug>
  PUT  /api/mort-buddy/assignments?farm=<slug>

Both endpoints resolve the "day" server-side via aest_today() (no client date).
Auth: session cookie OR ?t=<farmToken> (SEC-006 farm isolation).
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://harvest-hub-634.preview.emergentagent.com").rstrip("/")
FARM = "default"
FARM_TOKEN = "_JDgeoWnHPi5RTiW5WDmZYxou8tpxqDo"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module", autouse=True)
def cleanup(client):
    # after all tests, clear assignments
    yield
    try:
        client.put(
            f"{BASE_URL}/api/mort-buddy/assignments",
            params={"farm": FARM, "t": FARM_TOKEN},
            json={"assignments": {}},
            timeout=15,
        )
    except Exception:
        pass


# ─── Read ────────────────────────────────────────────────────────────────
def test_get_requires_farm_access_denied_without_token(client):
    r = client.get(f"{BASE_URL}/api/mort-buddy/assignments", params={"farm": FARM}, timeout=15)
    # Should be 401/403 without session or token
    assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}: {r.text[:200]}"


def test_get_with_token_ok(client):
    r = client.get(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "date" in data and "assignments" in data
    assert isinstance(data["assignments"], dict)
    # date is a server-computed AEST YYYY-MM-DD
    assert isinstance(data["date"], str) and len(data["date"]) == 10


# ─── Write + persistence ────────────────────────────────────────────────
def test_put_persists_and_get_reads_back_same_day(client):
    payload = {"assignments": {"1": "TEST_Mick", "2": "TEST_Mick", "3": "TEST_Dave"}}
    r = client.put(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        json=payload,
        timeout=15,
    )
    assert r.status_code == 200, r.text
    put_data = r.json()
    assert put_data.get("ok") is True
    assert put_data["assignments"] == payload["assignments"]
    put_date = put_data["date"]

    # GET back
    r2 = client.get(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        timeout=15,
    )
    assert r2.status_code == 200
    got = r2.json()
    # Same server-day (timezone regression check)
    assert got["date"] == put_date, f"GET date {got['date']} != PUT date {put_date} — TZ bug regression?"
    assert got["assignments"] == payload["assignments"]


def test_put_strips_and_drops_empty_values(client):
    payload = {"assignments": {"1": "  TEST_Alice  ", "2": "", "3": "   ", "4": "TEST_Bob"}}
    r = client.put(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        json=payload,
        timeout=15,
    )
    assert r.status_code == 200, r.text
    assigned = r.json()["assignments"]
    assert assigned == {"1": "TEST_Alice", "4": "TEST_Bob"}, assigned


def test_put_truncates_long_names_to_60(client):
    long_name = "TEST_" + "A" * 100
    r = client.put(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        json={"assignments": {"1": long_name}},
        timeout=15,
    )
    assert r.status_code == 200
    val = r.json()["assignments"]["1"]
    assert len(val) <= 60


def test_put_overwrite_replaces_previous_day(client):
    # First set 3 sheds
    client.put(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        json={"assignments": {"1": "TEST_X", "2": "TEST_Y", "3": "TEST_Z"}},
        timeout=15,
    )
    # Overwrite with only 1 shed
    r = client.put(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        json={"assignments": {"5": "TEST_Only"}},
        timeout=15,
    )
    assert r.status_code == 200
    # GET should reflect just the new mapping
    got = client.get(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        timeout=15,
    ).json()
    assert got["assignments"] == {"5": "TEST_Only"}


def test_put_empty_object_clears(client):
    r = client.put(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        json={"assignments": {}},
        timeout=15,
    )
    assert r.status_code == 200
    got = client.get(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        timeout=15,
    ).json()
    assert got["assignments"] == {}


def test_put_requires_auth(client):
    r = client.put(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM},
        json={"assignments": {"1": "TEST_NoAuth"}},
        timeout=15,
    )
    assert r.status_code in (401, 403), r.status_code


def test_put_rejects_bad_payload(client):
    r = client.put(
        f"{BASE_URL}/api/mort-buddy/assignments",
        params={"farm": FARM, "t": FARM_TOKEN},
        json={"assignments": "not-a-dict"},
        timeout=15,
    )
    assert r.status_code in (400, 422), r.status_code


# ─── Regression: existing morts endpoint still works ────────────────────
def test_morts_post_still_works(client):
    r = client.post(
        f"{BASE_URL}/api/integrations/morts",
        params={"farm": FARM, "t": FARM_TOKEN},
        json={"entries": [{"shed": 1, "morts": 0, "culls": 0, "date": "2026-01-01"}], "source": "TEST_regression"},
        timeout=15,
    )
    # Accept 200 or 201 depending on impl
    assert r.status_code in (200, 201), r.text[:300]
