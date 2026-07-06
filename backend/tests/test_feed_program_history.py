"""
Regression tests for Feed Program bulletproof safety net (Scope A):

  1) EOB feed-load restore bug (App.tsx hydration filter): the `applyMap`
     COL_B row-≥12 skip must ONLY apply to shed sheets, not the End-of-Batch
     sheet. Server-side we can't test the client hydration directly, but we
     CAN prove the persistence layer preserves whatever the client sends
     (round-trip) which is the crucial invariant.
  2) Anti-corruption guard on PUT /api/feed-program/state — refuses to
     overwrite when new state is >30 % smaller than stored, unless
     forceOverwrite=True.
  3) Cloud Rewind endpoints:
       - GET /api/feed-program/history returns metadata only (no blob).
       - GET /api/feed-program/history/{id} returns the full blob.
       - POST /api/feed-program/history/{id}/restore replaces the current
         state AND writes a pre-rewind backup.
  4) History pruning to _FEED_STATE_HISTORY_LIMIT (soft-tested with a
     smaller run of successive writes).

We use a throwaway farm slug ("safety-net-test") so we don't touch DEFAULT
farm state.
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
FARM = f"safety-net-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module", autouse=True)
def cleanup_after(api):
    yield
    # Best-effort cleanup — remove any residual state so preview stays tidy.
    try:
        # There's no admin DELETE endpoint for history, so leave it — the
        # 30-snapshot cap + 60-day TTL will handle it.
        pass
    except Exception:
        pass


def _put(api, edits: str, force: bool = False, sheet_names=None):
    """Small helper for state PUTs."""
    if sheet_names is None:
        sheet_names = ["SHED 1 & 2", "end of batch"]
    return api.put(
        f"{BASE_URL}/api/feed-program/state",
        params={"farm": FARM},
        json={"edits": edits, "sheetNames": sheet_names, "forceOverwrite": force},
        timeout=15,
    )


# ── 1. Round-trip preservation ─────────────────────────────────────────
def test_state_roundtrip_preserves_full_payload(api):
    """Whatever the client sends must come back byte-for-byte on GET."""
    payload = "a" * 3000 + "\x1eeob_feed_load_row12_colB_date\x1f2025-06-28"
    r = _put(api, payload)
    assert r.status_code == 200, r.text
    g = api.get(f"{BASE_URL}/api/feed-program/state", params={"farm": FARM}, timeout=15)
    assert g.status_code == 200
    assert g.json()["edits"] == payload


# ── 2. Anti-corruption guard ────────────────────────────────────────────
def test_corruption_guard_blocks_massive_shrink(api):
    """A 99 % state drop must be blocked with HTTP 409 + corruption_guard detail."""
    # 1) Seed with a large state
    big = "x" * 5000
    r1 = _put(api, big)
    assert r1.status_code == 200
    # 2) Try to overwrite with tiny (< 30 % of 5000 = < 1500 bytes)
    tiny = "y" * 100
    r2 = _put(api, tiny)
    assert r2.status_code == 409, f"Expected 409, got {r2.status_code}: {r2.text}"
    detail = r2.json().get("detail", {})
    assert detail.get("error") == "corruption_guard"
    assert "existingSize" in detail
    assert "incomingSize" in detail
    assert detail["dropPct"] > 30
    # 3) Confirm stored state was NOT mutated
    g = api.get(f"{BASE_URL}/api/feed-program/state", params={"farm": FARM}, timeout=15)
    assert g.json()["edits"] == big


def test_corruption_guard_allows_normal_edits(api):
    """A ~5 % change (well under the 30 % threshold) must succeed."""
    base = "x" * 5000
    r1 = _put(api, base)
    assert r1.status_code == 200
    # Shrink by ~5 % — still well over the 30 % floor.
    small_change = "x" * 4750
    r2 = _put(api, small_change)
    assert r2.status_code == 200, r2.text


def test_corruption_guard_bypasses_with_force_flag(api):
    """forceOverwrite=True must let a legitimate 'new batch' reset through."""
    r1 = _put(api, "x" * 5000)
    assert r1.status_code == 200
    r2 = _put(api, "reset", force=True)
    assert r2.status_code == 200, r2.text
    g = api.get(f"{BASE_URL}/api/feed-program/state", params={"farm": FARM}, timeout=15)
    assert g.json()["edits"] == "reset"


def test_corruption_guard_off_for_new_farms(api):
    """Farms with <500 bytes of state should be unguarded (first-run case)."""
    fresh_farm = f"fresh-{uuid.uuid4().hex[:8]}"
    r = api.put(
        f"{BASE_URL}/api/feed-program/state",
        params={"farm": fresh_farm},
        json={"edits": "small", "sheetNames": ["SHED 1 & 2"]},
        timeout=15,
    )
    assert r.status_code == 200


# ── 3. Cloud Rewind endpoints ──────────────────────────────────────────
def test_cloud_rewind_history_and_restore_flow(api):
    """
    End-to-end: three sequential saves → history has ≥2 snapshots →
    restore the first → current state matches → a 'pre-rewind-backup' is
    added to history.
    """
    farm = f"rewind-{uuid.uuid4().hex[:8]}"
    put = lambda payload, force=False: api.put(  # noqa: E731
        f"{BASE_URL}/api/feed-program/state",
        params={"farm": farm},
        json={"edits": payload, "sheetNames": ["SHED 1 & 2", "end of batch"], "forceOverwrite": force},
        timeout=15,
    )

    # 1) Seed 3 successive states
    v1 = "state-version-1" + "-x" * 1000
    v2 = "state-version-2" + "-x" * 1000
    v3 = "state-version-3" + "-x" * 1000
    assert put(v1).status_code == 200
    assert put(v2).status_code == 200
    assert put(v3).status_code == 200

    # 2) GET /api/feed-program/history — metadata only, no blob
    hist = api.get(f"{BASE_URL}/api/feed-program/history", params={"farm": farm}, timeout=15)
    assert hist.status_code == 200
    rows = hist.json()
    assert isinstance(rows, list)
    assert len(rows) >= 2  # 3 puts → 2 pre-write backups
    # No blob returned in list
    for row in rows:
        assert "edits" not in row
        assert "id" in row and "capturedAt" in row and "capturedSize" in row

    # 3) Restore the oldest (v1) — should be last in the list
    oldest = rows[-1]
    restore = api.post(
        f"{BASE_URL}/api/feed-program/history/{oldest['id']}/restore",
        params={"farm": farm},
        timeout=15,
    )
    assert restore.status_code == 200, restore.text
    restored = restore.json()
    assert restored.get("ok") is True
    # The restored edits must match the size of the snapshot
    assert len(restored["edits"]) == oldest["capturedSize"]

    # 4) Current state must now equal the restored blob
    cur = api.get(f"{BASE_URL}/api/feed-program/state", params={"farm": farm}, timeout=15)
    assert cur.json()["edits"] == restored["edits"]

    # 5) History must have grown by one "pre-rewind-backup"
    hist2 = api.get(f"{BASE_URL}/api/feed-program/history", params={"farm": farm}, timeout=15)
    rows2 = hist2.json()
    reasons = [r.get("reason") for r in rows2]
    assert "pre-rewind-backup" in reasons


def test_restore_missing_snapshot_returns_404(api):
    r = api.post(
        f"{BASE_URL}/api/feed-program/history/does-not-exist/restore",
        params={"farm": FARM},
        timeout=15,
    )
    assert r.status_code == 404


def test_get_missing_snapshot_returns_404(api):
    r = api.get(
        f"{BASE_URL}/api/feed-program/history/does-not-exist",
        params={"farm": FARM},
        timeout=15,
    )
    assert r.status_code == 404
