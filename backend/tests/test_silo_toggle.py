"""Backend tests for Silo Active/FeedType toggle feature (Jan 2026)."""
import os
import requests
import pytest

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://harvest-hub-634.preview.emergentagent.com").rstrip("/")
MAGIC = f"{BASE}/api/auth/owner-magic?key=VO1Jd3ZFNuEdk69SX-7AzfgzHgtUuqB86_HB8sXS14Q&to=/"


@pytest.fixture(scope="module")
def sess():
    s = requests.Session()
    r = s.get(MAGIC, allow_redirects=True, timeout=15)
    assert r.status_code == 200, f"magic login failed: {r.status_code}"
    return s


def _silos(sess, farm="default"):
    r = sess.get(f"{BASE}/api/silos?farm={farm}", timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


# ---- shed-groups shape ----
def test_shed_groups_has_active_and_default_feed_type(sess):
    r = sess.get(f"{BASE}/api/shed-groups?farm=default", timeout=15)
    assert r.status_code == 200
    groups = r.json()
    assert isinstance(groups, list) and len(groups) > 0
    for g in groups:
        for s in g["silos"]:
            assert "active" in s and isinstance(s["active"], bool)
            assert "defaultFeedType" in s  # may be None


# ---- PATCH active auto-sets manualOverride ----
def test_patch_active_sets_manual_override(sess):
    silos = _silos(sess, "default")
    target = silos[0]
    sid = target["id"]
    try:
        r = sess.patch(f"{BASE}/api/silos/{sid}", json={"active": False}, timeout=15)
        assert r.status_code == 200, r.text
        # Verify via GET
        after = [s for s in _silos(sess, "default") if s["id"] == sid][0]
        assert after["active"] is False
        assert after.get("manualOverride") is True, f"expected manualOverride auto-set True, got {after}"
    finally:
        # restore
        sess.patch(f"{BASE}/api/silos/{sid}", json={"active": True, "manualOverride": False}, timeout=15)


# ---- PATCH feedType with manualOverride=false explicit ----
def test_patch_feed_type_can_reset_manual_override(sess):
    silos = _silos(sess, "default")
    target = silos[0]
    sid = target["id"]
    original_ft = target.get("defaultFeedType")
    try:
        # first, set manualOverride true via active toggle
        sess.patch(f"{BASE}/api/silos/{sid}", json={"active": False}, timeout=15)
        # now set feedType & reset override
        r = sess.patch(f"{BASE}/api/silos/{sid}",
                       json={"defaultFeedType": "Withdrawal", "manualOverride": False},
                       timeout=15)
        assert r.status_code == 200, r.text
        after = [s for s in _silos(sess, "default") if s["id"] == sid][0]
        assert after.get("defaultFeedType") == "Withdrawal"
        assert after.get("manualOverride") is False
    finally:
        sess.patch(f"{BASE}/api/silos/{sid}",
                   json={"defaultFeedType": original_ft or "Grower", "active": True, "manualOverride": False},
                   timeout=15)


# ---- readings/today shape ----
def test_readings_today_includes_silo_meta(sess):
    r = sess.get(f"{BASE}/api/readings/today?farm=default", timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    sheds = data.get("sheds") or data.get("shedGroups") or []
    assert sheds, f"no sheds in response: {list(data.keys())}"
    found = False
    for shed in sheds:
        for s in shed.get("silos", []):
            assert "siloId" in s
            assert "active" in s
            assert "siloFeedType" in s
            assert "manualOverride" in s
            found = True
    assert found, "no silos found under sheds[].silos[]"


# ---- batch reset resets manualOverride (destructive on throwaway farm) ----
def test_batch_reset_resets_manual_override(sess):
    farm = "north-creek"
    silos = _silos(sess, farm)
    if not silos:
        pytest.skip("no silos on north-creek")
    sid = silos[0]["id"]
    # Set manualOverride true via toggle
    sess.patch(f"{BASE}/api/silos/{sid}", json={"active": False}, timeout=15)
    after = [s for s in _silos(sess, farm) if s["id"] == sid][0]
    assert after.get("manualOverride") is True
    # Reset batch
    r = sess.delete(f"{BASE}/api/batch/reset?farm={farm}", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "silosOverrideReset" in body, f"missing silosOverrideReset in {body}"
    assert body["silosOverrideReset"] >= 1
    # Confirm
    post = [s for s in _silos(sess, farm) if s["id"] == sid][0]
    assert post.get("manualOverride") is False
    # Restore active (reset may have turned it back on or left off — normalize)
    sess.patch(f"{BASE}/api/silos/{sid}", json={"active": True, "manualOverride": False}, timeout=15)
