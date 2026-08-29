"""SEC-001 regression: every farm-scoped write endpoint must reject anonymous
callers with 401. GET endpoints stay open so the mobile reader UX still works
without a login."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or "http://localhost:8001"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


WRITE_ENDPOINTS = [
    ("POST",   "/api/silos",                     {"name": "X"}),
    ("PATCH",  "/api/silos/nonexistent",         {"name": "X"}),
    ("DELETE", "/api/silos/nonexistent",         None),
    ("POST",   "/api/readings/batch",            {"readings": [{"siloId": "x", "feedType": "S", "amountRemaining": 1, "unit": "kg"}]}),
    ("DELETE", "/api/readings/nonexistent",      None),
    ("POST",   "/api/deliveries",                {"feedType": "S", "amount": 1, "unit": "kg"}),
    ("DELETE", "/api/deliveries/nonexistent",    None),
    ("POST",   "/api/photos",                    {"category": "mort_sheet", "imageData": "x"}),
    ("DELETE", "/api/photos/nonexistent",        None),
    ("PATCH",  "/api/farm-config",               {}),
    ("PUT",    "/api/feed-program/state",        {"edits": "a=1", "sheetNames": []}),
    ("POST",   "/api/feed-program/history/x/restore", None),
    ("DELETE", "/api/batch/reset",               None),
]


@pytest.mark.parametrize("method,path,body", WRITE_ENDPOINTS)
def test_write_endpoint_requires_auth(api, method, path, body):
    r = api.request(method, f"{BASE_URL}{path}", json=body, timeout=15)
    assert r.status_code == 401, f"{method} {path} → {r.status_code} (expected 401): {r.text[:200]}"


# Read endpoints stay open — verify a sample of them
READ_ENDPOINTS = [
    "/api/silos",
    "/api/readings/today",
    "/api/deliveries",
    "/api/photos",
    "/api/eob/locked-batches",
    "/api/shed-groups",
]


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoint_stays_open(api, path):
    r = api.get(f"{BASE_URL}{path}", timeout=15)
    assert r.status_code == 200, f"GET {path} regressed to {r.status_code}"
