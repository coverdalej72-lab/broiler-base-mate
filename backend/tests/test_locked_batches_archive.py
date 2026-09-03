"""Locked Batches Archive endpoints (Mar 2026) + SEC-006 isolation.

Covers:
  GET  /api/eob/locked-batches
  GET  /api/eob/locked-batches/{id}
  GET  /api/eob/locked-batches/{id}/pdf
  POST /api/eob/locked-batches/{id}/resend
"""
import os

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

DEFAULT_FARM = "default"
DEFAULT_TOKEN = "o3JM8FS-wuEDbZE634lWm_5m_GrZx9rq"
NORTH_FARM = "north-creek"
NORTH_TOKEN = "JA-018MvYOhNc8hBnbfJ1DU0dxeaIiQ_"
SNAP_ID = "380e21fc-154e-4fc2-8dba-1d1fdacd1c9f"
MAGIC_KEY = "VO1Jd3ZFNuEdk69SX-7AzfgzHgtUuqB86_HB8sXS14Q"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    return s


@pytest.fixture(scope="module")
def owner_session():
    """Owner magic-link session cookie."""
    s = requests.Session()
    r = s.get(f"{BASE_URL}/api/auth/owner-magic", params={"key": MAGIC_KEY, "to": "/"},
              allow_redirects=False)
    if r.status_code not in (200, 302, 303, 307):
        pytest.fail(f"owner-magic failed {r.status_code}: {r.text[:300]}")
    me = s.get(f"{BASE_URL}/api/auth/me")
    if me.status_code != 200:
        pytest.fail(f"/api/auth/me after magic link failed {me.status_code}: {me.text[:300]}")
    return s


# ── list ───────────────────────────────────────────────────────────────
class TestListLockedBatches:
    def test_list_with_farm_token(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches",
                       params={"farm": DEFAULT_FARM, "t": DEFAULT_TOKEN})
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert isinstance(data, list) and len(data) >= 1
        snap = next((d for d in data if d["id"] == SNAP_ID), None)
        assert snap is not None, f"seeded snapshot {SNAP_ID} missing"
        assert snap["farmId"] == DEFAULT_FARM
        assert isinstance(snap.get("batchIdentifier"), str)
        assert "kpis" in snap
        for k in ("aveWeight", "fcr", "cfcr", "totalCaught"):
            assert k in snap["kpis"]
        # PII / blob exclusions
        for forbidden in ("_id", "html", "pdfBase64", "lockedBy", "email"):
            assert forbidden not in snap, f"{forbidden} leaked in list response"

    def test_list_with_owner_session(self, owner_session):
        r = owner_session.get(f"{BASE_URL}/api/eob/locked-batches", params={"farm": DEFAULT_FARM})
        assert r.status_code == 200
        assert any(d["id"] == SNAP_ID for d in r.json())

    def test_list_requires_auth(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches", params={"farm": DEFAULT_FARM})
        assert r.status_code == 401, f"expected 401, got {r.status_code}"

    def test_list_rejects_bad_token(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches",
                       params={"farm": DEFAULT_FARM, "t": "not-a-real-token-xxxxxxxxxxxxxx"})
        assert r.status_code == 401

    def test_foreign_token_cannot_read_other_farm(self, client):
        """north-creek token must NOT unlock the default farm's batches."""
        r = client.get(f"{BASE_URL}/api/eob/locked-batches",
                       params={"farm": DEFAULT_FARM, "t": NORTH_TOKEN})
        assert r.status_code == 401, f"ISOLATION BREACH: {r.status_code} {r.text[:200]}"

    def test_other_farm_has_no_locked_batches(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches",
                       params={"farm": NORTH_FARM, "t": NORTH_TOKEN})
        assert r.status_code == 200
        data = r.json()
        assert all(d["farmId"] == NORTH_FARM for d in data)
        assert all(d["id"] != SNAP_ID for d in data)


# ── detail ─────────────────────────────────────────────────────────────
class TestGetLockedBatch:
    def test_detail_with_token(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}",
                       params={"farm": DEFAULT_FARM, "t": DEFAULT_TOKEN})
        assert r.status_code == 200, r.text[:300]
        doc = r.json()
        assert doc["id"] == SNAP_ID
        assert "_id" not in doc
        assert doc.get("report")

    def test_detail_requires_auth(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}", params={"farm": DEFAULT_FARM})
        assert r.status_code == 401

    def test_detail_wrong_farm_404(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}",
                       params={"farm": NORTH_FARM, "t": NORTH_TOKEN})
        assert r.status_code == 404


# ── pdf ────────────────────────────────────────────────────────────────
class TestLockedBatchPdf:
    def test_pdf_with_token(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}/pdf",
                       params={"farm": DEFAULT_FARM, "t": DEFAULT_TOKEN})
        assert r.status_code == 200, r.text[:300]
        ctype = r.headers.get("content-type", "")
        assert "application/pdf" in ctype or "text/html" in ctype, ctype
        if "application/pdf" in ctype:
            assert r.content[:4] == b"%PDF", "not a real PDF payload"
            assert len(r.content) > 1000
            assert "attachment" in r.headers.get("content-disposition", "") or \
                   "inline" in r.headers.get("content-disposition", "")

    def test_pdf_requires_auth(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}/pdf",
                       params={"farm": DEFAULT_FARM})
        assert r.status_code == 401

    def test_pdf_bad_id_404(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches/does-not-exist/pdf",
                       params={"farm": DEFAULT_FARM, "t": DEFAULT_TOKEN})
        assert r.status_code == 404

    def test_pdf_foreign_farm_blocked(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}/pdf",
                       params={"farm": NORTH_FARM, "t": NORTH_TOKEN})
        assert r.status_code == 404


# ── resend ─────────────────────────────────────────────────────────────
class TestLockedBatchResend:
    def test_resend_requires_auth(self, client):
        r = client.post(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}/resend",
                        params={"farm": DEFAULT_FARM}, json={})
        assert r.status_code == 401

    def test_resend_bad_token(self, client):
        r = client.post(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}/resend",
                        params={"farm": DEFAULT_FARM, "t": "bogus-token-000000000000"}, json={})
        assert r.status_code == 401

    def test_resend_bad_id_404(self, client):
        r = client.post(f"{BASE_URL}/api/eob/locked-batches/nope-nope/resend",
                        params={"farm": DEFAULT_FARM, "t": DEFAULT_TOKEN}, json={})
        assert r.status_code == 404

    def test_resend_sends_email(self, client):
        r = client.post(f"{BASE_URL}/api/eob/locked-batches/{SNAP_ID}/resend",
                        params={"farm": DEFAULT_FARM, "t": DEFAULT_TOKEN}, json={},
                        timeout=90)
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        assert body.get("ok") is True
        assert "@" in body.get("sentTo", "")
