"""Backend tests for the MTEC Flock Record Card reconcile feature.

Covers:
  1. Non-PDF upload rejected with 400.
  2. PDF upload returns jobId instantly (proves background pattern).
  3. Poll job -> eventually 'done' with 12 sheds, dates 2026-09-07 to 2026-09-21.
  4. All sheds mismatched with bbmTotal == 0 on this clean farm.
  5. 'Fix' pattern via POST /api/integrations/morts source=mtec-reconcile
     hard-overwrites an inflated pre-existing value DOWN.
  6. GET /api/integrations/morts returns entries with source='mtec-reconcile'.
  7. Cleanup all test data.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://harvest-hub-634.preview.emergentagent.com").rstrip("/")
FARM = "default"
FARM_TOKEN = "_JDgeoWnHPi5RTiW5WDmZYxou8tpxqDo"
PDF_PATH = "/tmp/test_flock_card.pdf"

HEADERS = {"x-farm-token": FARM_TOKEN}

# Track things we created so we can clean up after
_created_dates: set[str] = set()


@pytest.fixture(scope="module")
def scan_result():
    """Upload PDF once, poll to completion, return final result payload."""
    assert os.path.exists(PDF_PATH), f"Missing test PDF at {PDF_PATH}"
    with open(PDF_PATH, "rb") as fp:
        files = {"file": ("flock_card.pdf", fp, "application/pdf")}
        t0 = time.time()
        r = requests.post(
            f"{BASE_URL}/api/mort-buddy/mtec-scan?farm={FARM}",
            files=files, headers=HEADERS, timeout=30,
        )
        elapsed_start = time.time() - t0
    assert r.status_code == 200, f"Start scan failed: {r.status_code} {r.text}"
    body = r.json()
    assert body.get("ok") and body.get("jobId"), body
    # Background pattern proof: the initial POST must return quickly (<15s),
    # NOT block for 40-60s waiting on Gemini (which would 502 through ingress).
    assert elapsed_start < 15, f"Start endpoint blocked {elapsed_start:.1f}s — regression to sync pattern"
    job_id = body["jobId"]

    # Poll up to 120s
    deadline = time.time() + 120
    final = None
    while time.time() < deadline:
        pr = requests.get(
            f"{BASE_URL}/api/mort-buddy/mtec-scan/{job_id}?farm={FARM}",
            headers=HEADERS, timeout=15,
        )
        assert pr.status_code == 200, f"Poll failed: {pr.status_code} {pr.text}"
        pj = pr.json()
        if pj.get("status") == "done":
            final = pj
            break
        if pj.get("status") == "error":
            pytest.fail(f"Scan job errored: {pj.get('error')}")
        time.sleep(4)
    assert final is not None, "Scan did not complete within 120s"
    return final


class TestMtecScan:
    def test_non_pdf_rejected(self):
        files = {"file": ("hello.txt", b"not a pdf", "text/plain")}
        r = requests.post(
            f"{BASE_URL}/api/mort-buddy/mtec-scan?farm={FARM}",
            files=files, headers=HEADERS, timeout=15,
        )
        assert r.status_code == 400, f"Expected 400 for non-PDF, got {r.status_code}: {r.text}"

    def test_scan_returns_12_sheds_and_dates(self, scan_result):
        assert scan_result["status"] == "done"
        sheds = scan_result.get("sheds") or []
        assert len(sheds) == 12, f"Expected 12 sheds, got {len(sheds)}"
        assert scan_result.get("dateRange") == ["2026-09-07", "2026-09-21"]

    def test_all_sheds_mismatched_bbm_zero(self, scan_result):
        # Clean farm: no external_morts overlap → every shed should be mismatched with bbmTotal==0.
        # (Test ordering matters — this must run before the overwrite test contaminates shed 2.)
        for s in scan_result["sheds"]:
            assert s["bbmTotal"] == 0, f"Shed {s['shed']} expected bbmTotal=0, got {s['bbmTotal']}"
            assert s["mtecTotal"] > 0, f"Shed {s['shed']} MTEC total should be > 0"
            assert s["mtecTotal"] != s["bbmTotal"], f"Shed {s['shed']} unexpectedly matches"

    def test_shed1_total_matches_known_value(self, scan_result):
        # Main agent verified via manual test: shed 1 total = 673 (matches PDF's own printed Shed Total).
        shed1 = next(s for s in scan_result["sheds"] if s["shed"] == 1)
        assert shed1["mtecTotal"] == 673, f"Shed 1 MTEC total drift: {shed1['mtecTotal']} (expected 673)"


class TestFixOverwriteDown:
    """Regression: 'Fix' must hard-overwrite an inflated pre-existing value
    DOWNWARD (contrast with default max-merge everywhere else)."""

    def test_fix_overwrites_inflated_value_downward(self, scan_result):
        shed2 = next(s for s in scan_result["sheds"] if s["shed"] == 2)
        # Pick the first mismatched day for shed 2 and inflate BBM there.
        assert shed2["mismatchDays"], "shed 2 should have mismatchDays"
        day = shed2["mismatchDays"][0]
        date = day["date"]
        real_morts = int(day["mtecMorts"])
        _created_dates.add(date)

        # Step 1: seed an inflated value
        seed = {
            "entries": [{"shed": 2, "date": date, "morts": 999, "culls": 0}],
            "source": "test-seed-inflate",
        }
        rs = requests.post(
            f"{BASE_URL}/api/integrations/morts?farm={FARM}",
            json=seed, headers=HEADERS, timeout=15,
        )
        assert rs.status_code == 200, rs.text

        # Confirm seed present
        rg = requests.get(f"{BASE_URL}/api/integrations/morts?farm={FARM}",
                          headers=HEADERS, timeout=15)
        assert rg.status_code == 200
        rows = [e for e in rg.json()["entries"] if e["shed"] == 2 and e["date"] == date]
        assert rows and rows[0]["morts"] == 999, f"Seed not stored: {rows}"

        # Step 2: emulate frontend Fix — post real MTEC value with source=mtec-reconcile.
        # This is exactly what fixShedFromMtec() does per the review request.
        fix_entries = [
            {"shed": 2, "date": d["date"], "morts": int(d["mtecMorts"]), "culls": int(d["mtecCulls"])}
            for d in shed2["mismatchDays"]
        ]
        for e in fix_entries:
            _created_dates.add(e["date"])
        rf = requests.post(
            f"{BASE_URL}/api/integrations/morts?farm={FARM}",
            json={"entries": fix_entries, "source": "mtec-reconcile"},
            headers=HEADERS, timeout=20,
        )
        assert rf.status_code == 200, rf.text

        # Step 3: verify overwrite DOWN happened.
        rg2 = requests.get(f"{BASE_URL}/api/integrations/morts?farm={FARM}",
                           headers=HEADERS, timeout=15)
        assert rg2.status_code == 200
        after = [e for e in rg2.json()["entries"] if e["shed"] == 2 and e["date"] == date]
        assert after, "row missing after fix"
        assert after[0]["morts"] == real_morts, (
            f"Fix did NOT overwrite DOWN: stored={after[0]['morts']}, expected={real_morts}"
        )
        assert after[0]["source"] == "mtec-reconcile"

    def test_get_returns_mtec_reconcile_source(self):
        r = requests.get(f"{BASE_URL}/api/integrations/morts?farm={FARM}",
                         headers=HEADERS, timeout=15)
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert any(e.get("source") == "mtec-reconcile" for e in entries), (
            "Expected at least one entry with source='mtec-reconcile'"
        )


def test_zzz_cleanup():
    """Delete every date we touched so we don't leave test data behind."""
    # Include the full scan window as insurance
    all_dates = set(_created_dates)
    # Add the whole 2026-09-07 → 2026-09-21 range as belt-and-braces cleanup
    from datetime import date, timedelta
    d = date(2026, 9, 7)
    end = date(2026, 9, 21)
    while d <= end:
        all_dates.add(d.isoformat())
        d += timedelta(days=1)

    for dt in sorted(all_dates):
        r = requests.delete(
            f"{BASE_URL}/api/integrations/morts?farm={FARM}&date={dt}",
            headers=HEADERS, timeout=15,
        )
        # 200 either way — delete_many is safe even when nothing matches.
        assert r.status_code == 200, f"cleanup failed for {dt}: {r.status_code} {r.text}"
