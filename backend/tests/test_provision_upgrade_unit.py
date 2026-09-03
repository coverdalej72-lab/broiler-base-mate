"""Unit test for server._provision_purchase 'kind == upgrade' branch.

Simulates a completed upgrade payment by inserting a matching
payment_transactions doc, then calling _provision_purchase directly.
email_service.send_email is monkeypatched so no real email is sent and no
real Stripe call/charge occurs.
"""
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app/backend")


def test_upgrade_branch_updates_existing_farm(monkeypatch):
    import asyncio
    asyncio.run(_run(monkeypatch))


async def _run(monkeypatch):
    import email_service
    import server

    sent = []

    async def fake_send_email(**kwargs):
        sent.append(kwargs)
        return {"ok": True, "provider": "fake"}

    monkeypatch.setattr(email_service, "send_email", fake_send_email)

    slug = f"test-upg-{uuid.uuid4().hex[:6]}"
    sid = f"cs_test_fake_{uuid.uuid4().hex[:10]}"
    owner = "test_upgrade_owner@example.com"

    await server.farms_col.insert_one({
        "id": str(uuid.uuid4()), "slug": slug, "name": "TEST_ Upgrade Farm",
        "ownerEmail": owner, "subscriptionStatus": "trialing", "tier": "bronze_monthly",
        "farmToken": uuid.uuid4().hex,
    })
    await server.payments_col.insert_one({
        "id": str(uuid.uuid4()), "session_id": sid, "kind": "upgrade",
        "package_id": "gold_monthly", "amount": 30.0, "currency": "aud",
        "mode": "subscription", "email": owner, "farm_slug": slug,
        "stripe_subscription_id": "sub_fake_123",
        "provisioned": False, "payment_status": "paid", "status": "complete",
        "createdAt": datetime.now(timezone.utc),
    })
    try:
        farms_before = await server.farms_col.count_documents({})
        result = await server._provision_purchase(sid)
        assert result and result.get("upgradedFarm") == slug, result

        farm = await server.farms_col.find_one({"slug": slug})
        assert farm["subscriptionStatus"] == "active"
        assert farm["tier"] == "gold_monthly"
        assert farm["stripeSubscriptionId"] == "sub_fake_123"
        assert farm.get("upgradedAt") is not None
        # No new farm created
        assert await server.farms_col.count_documents({}) == farms_before

        # Confirmation email to the farm owner
        assert any(m.get("to") == owner for m in sent), sent
        assert any("upgraded" in (m.get("subject") or "").lower() for m in sent), sent

        # Idempotency: second call must short-circuit
        assert await server._provision_purchase(sid) is None
    finally:
        await server.farms_col.delete_many({"slug": slug})
        await server.payments_col.delete_many({"session_id": sid})
