"""Tests for the Mar 2026 'Upgrade & Pay Now' Stripe path.

Module under test: server.py POST /api/farm/upgrade-checkout
                   server.py _provision_purchase (kind == "upgrade" branch, code review)
Plus light regression smoke on /api/trial/start, /api/checkout (sponsor),
locked-batches archive and QR-anonymous farm access.

NOTE: STRIPE_API_KEY in this environment is a LIVE key. These tests only CREATE
checkout sessions (no charge occurs unless card details are submitted) and never
open/complete the hosted checkout page.
"""
import os
import uuid

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

FARM = "default"
FARM_TOKEN = "o3JM8FS-wuEDbZE634lWm_5m_GrZx9rq"
OWNER_EMAIL = "appcovi2026@gmail.com"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def mdb():
    c = MongoClient(MONGO_URL)
    return c[DB_NAME]


@pytest.fixture(scope="module")
def created_sessions():
    return []


@pytest.fixture(scope="module", autouse=True)
def cleanup(mdb, created_sessions):
    yield
    for sid in created_sessions:
        mdb.payment_transactions.delete_many({"session_id": sid})
    mdb.farms.delete_many({"slug": {"$regex": "^test-noemail-"}})


# ─── /api/farm/upgrade-checkout ────────────────────────────────────────────
class TestUpgradeCheckout:
    def test_requires_auth(self, client):
        r = client.post(f"{BASE_URL}/api/farm/upgrade-checkout?farm={FARM}")
        assert r.status_code == 401, r.text
        assert "detail" in r.json()

    def test_bad_token_rejected(self, client):
        r = client.post(f"{BASE_URL}/api/farm/upgrade-checkout?farm={FARM}&t=not-a-real-token")
        assert r.status_code == 401, r.text

    def test_nonexistent_farm(self, client):
        r = client.post(f"{BASE_URL}/api/farm/upgrade-checkout?farm=does-not-exist-{uuid.uuid4().hex[:6]}")
        # No auth possible for a farm that doesn't exist -> 401 (auth guard first)
        assert r.status_code in (401, 404), r.text

    def test_creates_real_stripe_session(self, client, mdb, created_sessions):
        r = client.post(f"{BASE_URL}/api/farm/upgrade-checkout?farm={FARM}&t={FARM_TOKEN}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert set(["url", "session_id"]).issubset(data.keys())
        assert isinstance(data["url"], str)
        assert data["url"].startswith("https://checkout.stripe.com/"), data["url"]
        assert data["session_id"].startswith("cs_"), data["session_id"]
        created_sessions.append(data["session_id"])

        # Persisted payment doc
        doc = mdb.payment_transactions.find_one({"session_id": data["session_id"]})
        assert doc is not None, "payments doc not persisted"
        assert doc["kind"] == "upgrade"
        assert doc["mode"] == "subscription"
        assert doc["farm_slug"] == FARM
        assert doc["email"] == OWNER_EMAIL
        assert doc["currency"] == "aud"
        assert doc["provisioned"] is False
        assert doc["payment_status"] == "initiated"
        assert doc["amount"] > 0

    def test_session_has_no_trial_and_correct_amount(self, created_sessions):
        """Card must be charged immediately (no trial_period_days) + AUD amount."""
        assert created_sessions, "previous test must have created a session"
        import stripe
        stripe.api_key = backend_env.get("STRIPE_API_KEY")
        s = stripe.checkout.Session.retrieve(created_sessions[0], expand=["line_items"])
        assert s["mode"] == "subscription"
        assert s["currency"] == "aud"
        assert (s.get("amount_total") or 0) > 0
        assert s["customer_email"] == OWNER_EMAIL
        assert s["metadata"]["kind"] == "upgrade"
        assert s["metadata"]["upgrade_farm_slug"] == FARM
        sd = s.get("subscription_data") or {}
        assert not sd.get("trial_period_days"), f"unexpected trial period: {sd}"
        li = s["line_items"]["data"][0]
        assert li["price"]["recurring"]["interval"] in ("month", "year")
        assert li["price"]["currency"] == "aud"

    def test_missing_owner_email_returns_400(self, client, mdb):
        slug = f"test-noemail-{uuid.uuid4().hex[:6]}"
        token = uuid.uuid4().hex
        mdb.farms.insert_one({
            "id": str(uuid.uuid4()), "slug": slug, "name": "TEST_ No Email Farm",
            "farmToken": token, "subscriptionStatus": "trialing",
        })
        try:
            r = client.post(f"{BASE_URL}/api/farm/upgrade-checkout?farm={slug}&t={token}")
            assert r.status_code == 400, r.text
            assert "owner email" in r.json().get("detail", "").lower()
        finally:
            mdb.farms.delete_one({"slug": slug})

    def test_cross_farm_token_denied(self, client):
        """default's token must not authorise upgrade checkout for another farm."""
        r = client.post(f"{BASE_URL}/api/farm/upgrade-checkout?farm=north-creek&t={FARM_TOKEN}")
        assert r.status_code in (401, 403), r.text


# ─── Regression smoke ─────────────────────────────────────────────────────
class TestRegressionSmoke:
    def test_trial_start_idempotent_no_card(self, client, mdb):
        email = f"test_upgrade_{uuid.uuid4().hex[:8]}@example.com"
        payload = {"email": email, "name": "TEST_ Trial", "farmName": "TEST_ Trial Farm",
                   "packageId": "silver_monthly"}
        r = client.post(f"{BASE_URL}/api/trial/start", json=payload)
        assert r.status_code in (200, 201), r.text
        d = r.json()
        slug = d.get("farmSlug") or d.get("slug")
        assert slug, d
        try:
            farm = mdb.farms.find_one({"slug": slug})
            assert farm and farm.get("subscriptionStatus") == "trialing"
            assert farm.get("ownerEmail") == email
            # idempotency: second call reuses the same farm
            r2 = client.post(f"{BASE_URL}/api/trial/start", json=payload)
            assert r2.status_code in (200, 201), r2.text
            assert (r2.json().get("farmSlug") or r2.json().get("slug")) == slug
            assert mdb.farms.count_documents({"ownerEmail": email}) == 1
        finally:
            mdb.farms.delete_many({"ownerEmail": email})

    def test_trial_start_rejects_bad_email(self, client):
        r = client.post(f"{BASE_URL}/api/trial/start", json={"email": "nope", "name": "TEST_ Bad"})
        assert r.status_code == 400, r.text

    def test_sponsor_checkout(self, client, mdb, created_sessions):
        r = client.post(f"{BASE_URL}/api/checkout",
                        json={"packageId": "sponsor_10", "email": "test_sponsor@example.com", "originUrl": BASE_URL})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("url", "").startswith("https://checkout.stripe.com/"), d
        sid = d.get("session_id") or d.get("sessionId")
        if sid:
            created_sessions.append(sid)

    def test_checkout_invalid_package(self, client):
        r = client.post(f"{BASE_URL}/api/checkout", json={"packageId": "bogus", "email": "a@b.com", "originUrl": BASE_URL})
        assert r.status_code == 400, r.text

    def test_locked_batches_archive(self, client):
        r = client.get(f"{BASE_URL}/api/eob/locked-batches?farm={FARM}&t={FARM_TOKEN}")
        assert r.status_code == 200, r.text
        body = r.json()
        items = body if isinstance(body, list) else body.get("batches", body.get("items", body.get("snapshots")))
        assert isinstance(items, list), body
        assert "_id" not in r.text

    def test_qr_anonymous_farm_access(self, client):
        r = client.get(f"{BASE_URL}/api/farm-config?farm={FARM}&t={FARM_TOKEN}")
        assert r.status_code == 200, r.text
        assert "_id" not in r.text

    def test_feed_program_state_via_token(self, client):
        r = client.get(f"{BASE_URL}/api/feed-program/state?farm={FARM}&t={FARM_TOKEN}")
        assert r.status_code == 200, r.text
        assert "_id" not in r.text

    def test_trial_status(self, client):
        r = client.get(f"{BASE_URL}/api/farm/trial-status?farm={FARM}&t={FARM_TOKEN}")
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), dict)

    def test_farm_access_denied_without_token(self, client):
        r = client.get(f"{BASE_URL}/api/feed-program/state?farm={FARM}")
        assert r.status_code in (401, 403), r.text
