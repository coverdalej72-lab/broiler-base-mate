"""Backend API tests for Coop Overwatch"""
import os
import time
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://mother-hen-ai.preview.emergentagent.com").rstrip("/")
API = f"{BASE}/api"


@pytest.fixture(scope="session")
def s():
    return requests.Session()


# ---------- Dashboard / seed data ----------
class TestDashboard:
    def test_dashboard(self, s):
        r = s.get(f"{API}/dashboard")
        assert r.status_code == 200
        data = r.json()
        assert "tiles" in data
        assert len(data["tiles"]) == 5
        assert "active_alerts" in data and "pending_decisions" in data
        for t in data["tiles"]:
            assert t["status"] in ("healthy", "warning", "critical", "offline")
            assert "shed" in t and "farm" in t

    def test_farms(self, s):
        r = s.get(f"{API}/farms")
        assert r.status_code == 200 and len(r.json()) >= 2

    def test_sheds(self, s):
        r = s.get(f"{API}/sheds")
        assert r.status_code == 200 and len(r.json()) >= 5

    def test_batches(self, s):
        r = s.get(f"{API}/batches")
        assert r.status_code == 200 and len(r.json()) >= 5


# ---------- Breeds ----------
class TestBreeds:
    def test_list_breeds(self, s):
        r = s.get(f"{API}/breeds")
        assert r.status_code == 200
        data = r.json()
        names = data.get("breeds", [])
        assert "Ross 308" in names and "Cobb 500" in names

    def test_curve(self, s):
        r = s.get(f"{API}/breeds/Ross%20308/curve", params={"metric": "weight_g"})
        assert r.status_code == 200
        data = r.json()
        curve = data.get("curve", data if isinstance(data, list) else [])
        assert isinstance(curve, list) and len(curve) == 50


# ---------- Farm/Shed CRUD ----------
class TestFarmShedCRUD:
    farm_id = None
    shed_id = None

    def test_create_farm(self, s):
        r = s.post(f"{API}/farms", json={"name": "TEST_Farm_X", "location": "Nowhere", "vendor_system": "Rotem"})
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "TEST_Farm_X"
        TestFarmShedCRUD.farm_id = data["id"]

    def test_create_shed(self, s):
        assert TestFarmShedCRUD.farm_id
        r = s.post(f"{API}/sheds", json={"farm_id": TestFarmShedCRUD.farm_id, "name": "TEST_Shed_1", "capacity": 10000})
        assert r.status_code == 200
        TestFarmShedCRUD.shed_id = r.json()["id"]

    def test_update_shed(self, s):
        assert TestFarmShedCRUD.shed_id
        r = s.patch(f"{API}/sheds/{TestFarmShedCRUD.shed_id}", json={"name": "TEST_Shed_1_Renamed"})
        assert r.status_code == 200
        # Verify persistence
        g = s.get(f"{API}/sheds/{TestFarmShedCRUD.shed_id}")
        assert g.status_code == 200 and g.json()["name"] == "TEST_Shed_1_Renamed"

    def test_delete_shed(self, s):
        assert TestFarmShedCRUD.shed_id
        r = s.delete(f"{API}/sheds/{TestFarmShedCRUD.shed_id}")
        assert r.status_code == 200
        g = s.get(f"{API}/sheds/{TestFarmShedCRUD.shed_id}")
        assert g.status_code == 404

    def test_delete_farm(self, s):
        if TestFarmShedCRUD.farm_id:
            s.delete(f"{API}/farms/{TestFarmShedCRUD.farm_id}")


# ---------- Batch flow ----------
class TestBatch:
    def test_batch_close_and_create(self, s):
        sheds = s.get(f"{API}/sheds").json()
        shed_id = sheds[0]["id"]
        # Create a new batch (should close active)
        r = s.post(f"{API}/batches", json={
            "shed_id": shed_id, "breed": "Ross 308",
            "start_date": "2026-01-01", "bird_count_start": 20000
        })
        assert r.status_code == 200
        bid = r.json()["id"]
        assert r.json()["status"] == "active"
        # Close
        r2 = s.post(f"{API}/batches/{bid}/close", json={"final_weight_g": 2000, "final_fcr": 1.5, "notes": "test"})
        assert r2.status_code == 200
        g = s.get(f"{API}/batches/{bid}").json()
        assert g["status"] == "closed"


# ---------- Reading ingestion -> Alerts + Decisions ----------
class TestReadingsAlerts:
    def test_ingest_anomalous(self, s):
        sheds = s.get(f"{API}/sheds").json()
        shed_id = sheds[0]["id"]
        # Ensure active batch exists
        s.post(f"{API}/batches", json={
            "shed_id": shed_id, "breed": "Ross 308",
            "start_date": "2026-01-01", "bird_count_start": 20000
        })
        payload = {
            "shed_id": shed_id, "temp_c": 40, "humidity_pct": 70,
            "ammonia_ppm": 50, "mortality_today": 20, "water_liters": 1,
            "static_pressure_pa": 20, "avg_weight_g": 800
        }
        r = s.post(f"{API}/readings", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        # Reading object returned directly
        assert body.get("shed_id") == shed_id
        time.sleep(1)
        alerts = s.get(f"{API}/alerts", params={"unack_only": "true"}).json()
        assert len(alerts) > 0
        decisions = s.get(f"{API}/decisions").json()
        assert len(decisions) > 0

    def test_ack_alert(self, s):
        alerts = s.get(f"{API}/alerts", params={"unack_only": "true"}).json()
        if not alerts:
            pytest.skip("no alerts to ack")
        aid = alerts[0]["id"]
        r = s.post(f"{API}/alerts/{aid}/ack")
        assert r.status_code == 200
        # verify
        all_alerts = s.get(f"{API}/alerts").json()
        found = [a for a in all_alerts if a["id"] == aid]
        assert found and found[0]["acknowledged"] is True

    def test_approve_decision(self, s):
        decisions = s.get(f"{API}/decisions").json()
        if not decisions:
            pytest.skip("no decisions")
        d = decisions[0]
        r = s.post(f"{API}/decisions/approve", json={"decision_id": d["id"], "approve": True})
        assert r.status_code == 200
        after = s.get(f"{API}/decisions").json()
        found = [x for x in after if x["id"] == d["id"]][0]
        assert found["status"] == "approved"


# ---------- Policy ----------
class TestPolicy:
    def test_update_policy(self, s):
        r = s.patch(f"{API}/policies/increase_ventilation", json={"mode": "auto", "max_change_pct": 20})
        assert r.status_code == 200
        assert r.json()["mode"] == "auto"
        # revert
        s.patch(f"{API}/policies/increase_ventilation", json={"mode": "recommend"})


# ---------- Mother Hen LLM ----------
class TestMotherHen:
    def test_deep_analysis(self, s):
        sheds = s.get(f"{API}/sheds").json()
        # Use a shed that hasn't been perturbed by other tests
        shed_id = sheds[-1]["id"]
        # ensure active batch + a reading exist
        batches = s.get(f"{API}/batches", params={"shed_id": shed_id, "status": "active"}).json()
        if not batches:
            s.post(f"{API}/batches", json={
                "shed_id": shed_id, "breed": "Ross 308",
                "start_date": "2026-01-01", "bird_count_start": 20000
            })
            s.post(f"{API}/readings", json={
                "shed_id": shed_id, "temp_c": 25, "humidity_pct": 60, "ammonia_ppm": 15
            })
        r = s.post(f"{API}/mother-hen/analyze/{shed_id}", timeout=90)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "summary" in data and isinstance(data["summary"], str) and len(data["summary"]) > 20
        # findings/context may be absent when data insufficient; assert on real case
        assert "no active batch" not in data["summary"].lower(), f"insufficient data: {data}"
