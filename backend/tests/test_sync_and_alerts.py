"""
Regression tests for the two URGENT bugs:
  1) Silo readings sync chain: Reader → /api/readings/batch → /api/readings/today
  2) Farm Buddy alerts threshold + feed-program state persistence.
"""
import os
import re
from datetime import datetime, timezone

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://harvest-hub-634.preview.emergentagent.com").rstrip("/")
FARM = "default"


# ── Fixtures ────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def groups_and_silos(api):
    g = api.get(f"{BASE_URL}/api/shed-groups", params={"farm": FARM}, timeout=20)
    assert g.status_code == 200, g.text
    groups = g.json()
    assert len(groups) >= 1
    s = api.get(f"{BASE_URL}/api/silos", params={"farm": FARM}, timeout=20)
    assert s.status_code == 200, s.text
    return groups, s.json()


@pytest.fixture(scope="module")
def first_group_silos(groups_and_silos):
    groups, silos = groups_and_silos
    g0 = groups[0]
    gsilos = [x for x in silos if x.get("shedGroupId") == g0["id"]]
    assert len(gsilos) >= 1, "Expected at least one silo in first shed-group"
    return g0, gsilos


# ── 1) Readings sync chain ──────────────────────────────────────────────
class TestReadingsSyncChain:

    def test_post_reading_and_today_returns_saved(self, api, first_group_silos):
        group, gsilos = first_group_silos
        silo = gsilos[0]
        # POST a reading for today
        payload = {
            "readings": [{
                "siloId": silo["id"],
                "feedType": "Broiler Grower",
                "amountRemaining": 18.5,
                "unit": "t",
                "notes": "TEST_sync_reading",
            }]
        }
        r = api.post(f"{BASE_URL}/api/readings/batch", params={"farm": FARM}, json=payload, timeout=20)
        assert r.status_code == 201, r.text
        inserted = r.json()
        assert isinstance(inserted, list) and len(inserted) == 1
        assert inserted[0]["amountRemaining"] == 18.5

        # GET /api/readings/today must include sheds[].silos[].saved=true for this silo
        t = api.get(f"{BASE_URL}/api/readings/today", params={"farm": FARM}, timeout=20)
        assert t.status_code == 200, t.text
        body = t.json()
        # Shape assertions
        assert "date" in body and re.match(r"^\d{4}-\d{2}-\d{2}$", body["date"]), f"Bad date field: {body.get('date')}"
        assert "sheds" in body and isinstance(body["sheds"], list)
        target = next((sh for sh in body["sheds"] if sh["shedGroupId"] == group["id"]), None)
        assert target is not None, f"Shed group {group['id']} not in response"
        assert "shedGroupName" in target and target["shedGroupName"]
        assert "silos" in target and isinstance(target["silos"], list)
        # find the silo we just saved
        sil = next((x for x in target["silos"] if x["siloId"] == silo["id"]), None)
        assert sil is not None, f"Silo {silo['id']} missing from today's response"
        assert sil["saved"] is True, f"Expected saved=True, got {sil}"
        assert "letter" in sil
        assert "unit" in sil
        assert sil["amountRemaining"] == 18.5

    def test_today_filters_to_today_only(self, api, first_group_silos):
        """POST a reading dated yesterday → /api/readings/today should not surface it as 'saved' for today."""
        group, gsilos = first_group_silos
        # Use second silo so we don't collide with previous test
        silo = gsilos[1] if len(gsilos) > 1 else gsilos[0]
        yesterday = "2025-01-01T03:00:00+00:00"  # well in the past
        payload = {
            "readingDate": yesterday,
            "readings": [{
                "siloId": silo["id"],
                "feedType": "Broiler Grower",
                "amountRemaining": 7.0,
                "unit": "t",
                "notes": "TEST_old_reading",
            }],
        }
        r = api.post(f"{BASE_URL}/api/readings/batch", params={"farm": FARM}, json=payload, timeout=20)
        assert r.status_code == 201, r.text

        t = api.get(f"{BASE_URL}/api/readings/today", params={"farm": FARM}, timeout=20)
        assert t.status_code == 200
        body = t.json()
        target = next((sh for sh in body["sheds"] if sh["shedGroupId"] == group["id"]), None)
        sil = next((x for x in target["silos"] if x["siloId"] == silo["id"]), None)
        # If we didn't ALSO post a today reading for this silo, saved should be False
        # (depending on previous test it might be saved=True for silo[0] but this silo is fresh)
        # Note: gsilos[1] may equal gsilos[0] only if there's a single silo
        if silo["id"] != gsilos[0]["id"]:
            assert sil is not None
            assert sil["saved"] is False, f"Yesterday's reading leaked into today: {sil}"


# ── 2) Farm Buddy alerts (threshold-based) ──────────────────────────────
class TestFarmBuddyAlerts:

    def test_alerts_shape(self, api):
        r = api.get(f"{BASE_URL}/api/farm-buddy/alerts", params={"farm": FARM}, timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(["riskLevel", "alerts", "checkedAt"]).issubset(body.keys()), body.keys()
        assert isinstance(body["alerts"], list)
        assert body["riskLevel"] in ("ok", "watch", "critical")

    def test_critical_alert_after_3t_reading(self, api, groups_and_silos):
        """POST 1t to EVERY silo of a shed-group → total <5t → critical alert mentions group name."""
        groups, silos = groups_and_silos
        # pick the LAST group to keep tests stable (first group is used by sync tests above)
        target_group = groups[-1]
        gsilos = [s for s in silos if s.get("shedGroupId") == target_group["id"]]
        assert gsilos, "No silos in target group"

        # Put 1 t in each silo of that group (total <= ~3t for 3 silos → critical < 5 t)
        readings = [{
            "siloId": s["id"],
            "feedType": "Broiler Grower",
            "amountRemaining": 1.0,
            "unit": "t",
            "notes": "TEST_critical_alert",
        } for s in gsilos]
        r = api.post(f"{BASE_URL}/api/readings/batch", params={"farm": FARM}, json={"readings": readings}, timeout=20)
        assert r.status_code == 201, r.text

        a = api.get(f"{BASE_URL}/api/farm-buddy/alerts", params={"farm": FARM}, timeout=20)
        assert a.status_code == 200, a.text
        body = a.json()
        assert body["riskLevel"] == "critical", f"Expected critical, got {body['riskLevel']}: {body}"
        crit = [x for x in body["alerts"] if x.get("level") == "critical" and x.get("shedGroupId") == target_group["id"]]
        assert crit, f"No critical alert for group {target_group['name']}: {body['alerts']}"
        assert "ORDER FEED NOW" in crit[0]["message"]
        assert target_group["name"] in crit[0]["message"]


# ── 3) Farm Buddy /recommend new fields ─────────────────────────────────
class TestFarmBuddyRecommend:

    def test_recommend_accepts_new_shed_snapshot_fields(self, api):
        payload = {
            "farmContext": {
                "farmName": "TEST_farm",
                "placementDate": "2025-12-01",
                "sheds": [{
                    "shedNum": 1,
                    "name": "Shed 1&2",
                    "birdsPlaced": 50000,
                    "birdsOriginalPlaced": 55000,
                    "mortsToDate": 1200,
                    "birdsCaught": 3800,
                    "upcomingCatches": [{"date": "2026-01-20", "birds": 5000}],
                    "dayAge": 28,
                    "siloA": 8.0,
                    "siloB": 6.0,
                    "siloC": 4.0,
                    "siloTotal": 18.0,
                    "dailyUsageT": 6.5,
                    "daysOfFeedLeft": 2.7,
                }],
                "recentDeliveries": [],
            }
        }
        r = api.post(f"{BASE_URL}/api/farm-buddy/recommend", json=payload, timeout=60)
        assert r.status_code == 200, f"Status {r.status_code}: {r.text[:500]}"
        body = r.json()
        for k in ("headline", "detail", "actions", "riskLevel"):
            assert k in body, f"missing key {k}"
        assert isinstance(body["detail"], list)
        assert isinstance(body["actions"], list)
        assert body["riskLevel"] in ("ok", "watch", "urgent", "critical")


# ── 4) Feed Program state round-trip ────────────────────────────────────
class TestFeedProgramState:

    def test_state_round_trip(self, api):
        edits_payload = '[{"sheet":"SHED 1 & 2","row":2,"col":3,"value":"TEST_placement"}]'
        sheets = ["SHED 1 & 2", "SHED 3 & 4"]
        before = datetime.now(timezone.utc)

        p = api.put(f"{BASE_URL}/api/feed-program/state", params={"farm": FARM},
                    json={"edits": edits_payload, "sheetNames": sheets}, timeout=20)
        assert p.status_code == 200, p.text
        pbody = p.json()
        assert pbody.get("ok") is True
        assert "updatedAt" in pbody

        g = api.get(f"{BASE_URL}/api/feed-program/state", params={"farm": FARM}, timeout=20)
        assert g.status_code == 200, g.text
        gbody = g.json()
        assert gbody["edits"] == edits_payload, "edits did not round-trip"
        assert gbody["sheetNames"] == sheets
        # updatedAt is recent
        ts = gbody["updatedAt"]
        assert ts, "updatedAt missing"
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception as e:
            pytest.fail(f"updatedAt not ISO parseable: {ts} ({e})")
        delta = (dt - before).total_seconds()
        assert -5 <= delta <= 120, f"updatedAt not recent (delta={delta}s, ts={ts})"
