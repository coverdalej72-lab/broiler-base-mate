"""
Regression tests for iteration_3 bug fixes:
  1) Farm Buddy AI /api/farm-buddy/alerts must not produce false 'empty_shed'
     pin alerts for phantom sheds (shed_groups from the 20-shed default seed
     that were never placed with birds, i.e. have zero historical readings).
     Only sheds that HAVE historical readings AND are currently disabled in
     enabledGroupIds should be flagged as EMPTY.
  2) www → apex 301 redirect middleware (SEO fix).

The tests snapshot the farm-config's enabledGroupIds up front and restore it
on teardown so we don't leave the default farm in a broken state.
"""
import os
from datetime import datetime, timezone

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
LOCAL_URL = "http://localhost:8001"
FARM = "default"


# ── Shared session ─────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ── Snapshot & restore farm-config so we don't corrupt DEFAULT farm ────
@pytest.fixture(scope="module")
def farm_config_backup(api):
    r = api.get(f"{BASE_URL}/api/farm-config", params={"farm": FARM}, timeout=20)
    assert r.status_code == 200, r.text
    original = r.json()
    original_enabled = list(original.get("enabledGroupIds") or [])
    yield original_enabled
    # Teardown: restore original enabledGroupIds so the app returns to its pre-test state
    api.patch(
        f"{BASE_URL}/api/farm-config",
        params={"farm": FARM},
        json={"enabledGroupIds": original_enabled},
        timeout=20,
    )


@pytest.fixture(scope="module")
def groups_and_readings_map(api):
    """Return (groups, silos, group_ids_with_readings, group_ids_without_readings)."""
    g = api.get(f"{BASE_URL}/api/shed-groups", params={"farm": FARM}, timeout=20)
    assert g.status_code == 200
    groups = g.json()
    s = api.get(f"{BASE_URL}/api/silos", params={"farm": FARM}, timeout=20)
    assert s.status_code == 200
    silos = s.json()
    # /api/readings only returns up to 1000; that's plenty for the seeded default farm
    rd = api.get(f"{BASE_URL}/api/readings", params={"farm": FARM, "limit": 1000}, timeout=20)
    assert rd.status_code == 200
    silo_ids_with = {r["siloId"] for r in rd.json()}
    silo_to_group = {si["id"]: si.get("shedGroupId") for si in silos}
    with_reads = {silo_to_group[sid] for sid in silo_ids_with if silo_to_group.get(sid)}
    without_reads = {g0["id"] for g0 in groups if g0["id"] not in with_reads}
    return groups, silos, with_reads, without_reads


# ── 1) Phantom-shed suppression ─────────────────────────────────────────
class TestPhantomShedSuppression:
    """
    Verify /api/farm-buddy/alerts does NOT emit empty_shed alerts for shed_groups
    that have zero historical readings, even when they are disabled.
    """

    def test_default_farm_reports_zero_empty_groups_when_all_enabled(
        self, api, farm_config_backup, groups_and_readings_map
    ):
        """Baseline: with all shed_groups enabled, syncHealth.emptyGroups == 0."""
        groups, _silos, _with, _without = groups_and_readings_map
        # Ensure all groups are enabled
        all_ids = [g["id"] for g in groups]
        api.patch(f"{BASE_URL}/api/farm-config", params={"farm": FARM},
                  json={"enabledGroupIds": all_ids}, timeout=20)

        r = api.get(f"{BASE_URL}/api/farm-buddy/alerts", params={"farm": FARM}, timeout=20)
        assert r.status_code == 200
        body = r.json()
        empty_alerts = [a for a in body["alerts"] if a.get("category") == "empty_shed"]
        assert empty_alerts == [], f"Expected no empty_shed alerts, got {empty_alerts}"
        assert body["syncHealth"]["emptyGroups"] == 0

    def test_disabling_phantom_shed_produces_no_alert(
        self, api, farm_config_backup, groups_and_readings_map
    ):
        """
        Core phantom-shed fix: disable a shed_group that has NEVER had a reading,
        and confirm NO empty_shed alert is produced for it (and emptyGroups stays 0).
        """
        groups, _silos, _with_reads, without_reads = groups_and_readings_map
        assert without_reads, (
            "Default farm has no groups without readings — test cannot exercise the phantom "
            "shed filter. Seed data may have drifted."
        )
        phantom_gid = next(iter(without_reads))
        all_ids = [g["id"] for g in groups]
        # Disable only the phantom shed
        enabled = [gid for gid in all_ids if gid != phantom_gid]
        p = api.patch(f"{BASE_URL}/api/farm-config", params={"farm": FARM},
                      json={"enabledGroupIds": enabled}, timeout=20)
        assert p.status_code == 200

        # Verify farm-config actually persisted the disable
        gcfg = api.get(f"{BASE_URL}/api/farm-config", params={"farm": FARM}, timeout=20).json()
        assert phantom_gid not in gcfg["enabledGroupIds"]

        r = api.get(f"{BASE_URL}/api/farm-buddy/alerts", params={"farm": FARM}, timeout=20)
        assert r.status_code == 200
        body = r.json()
        empty_alerts = [a for a in body["alerts"] if a.get("category") == "empty_shed"]
        assert all(a["shedGroupId"] != phantom_gid for a in empty_alerts), (
            f"Phantom shed {phantom_gid} produced an empty_shed alert: {empty_alerts}"
        )
        # Since all disabled sheds in this test are phantoms, emptyGroups must be 0.
        assert body["syncHealth"]["emptyGroups"] == 0, (
            f"syncHealth.emptyGroups should be 0 for phantom-only disable, got "
            f"{body['syncHealth']['emptyGroups']}"
        )

    def test_disabling_real_shed_with_history_produces_empty_alert(
        self, api, farm_config_backup, groups_and_readings_map
    ):
        """
        Positive path: disable a shed that HAS historical readings, and confirm the
        empty_shed alert IS produced (and emptyGroups counter increments).
        """
        groups, _silos, with_reads, _without = groups_and_readings_map
        assert with_reads, "No shed groups with readings — can't exercise this test."
        real_gid = next(iter(with_reads))
        real_name = next(g["name"] for g in groups if g["id"] == real_gid)

        all_ids = [g["id"] for g in groups]
        enabled = [gid for gid in all_ids if gid != real_gid]
        p = api.patch(f"{BASE_URL}/api/farm-config", params={"farm": FARM},
                      json={"enabledGroupIds": enabled}, timeout=20)
        assert p.status_code == 200

        r = api.get(f"{BASE_URL}/api/farm-buddy/alerts", params={"farm": FARM}, timeout=20)
        assert r.status_code == 200
        body = r.json()
        empty_alerts = [a for a in body["alerts"] if a.get("category") == "empty_shed"]
        real_empty = [a for a in empty_alerts if a["shedGroupId"] == real_gid]
        assert real_empty, (
            f"Expected empty_shed alert for {real_name} (has readings, currently disabled), "
            f"got alerts={empty_alerts}"
        )
        assert real_empty[0]["level"] == "info"
        assert "EMPTY" in real_empty[0]["message"]
        assert body["syncHealth"]["emptyGroups"] >= 1

    def test_critical_alert_still_fires_on_low_feed(
        self, api, farm_config_backup, groups_and_readings_map
    ):
        """
        Ensure the phantom filter did NOT accidentally silence real critical
        low-feed alerts. Post readings <5t/silo to a group with existing history
        and check a critical alert with category-less low-feed message is emitted.
        """
        groups, silos, with_reads, _without = groups_and_readings_map
        assert with_reads, "No shed groups with readings — can't exercise this test."
        real_gid = next(iter(with_reads))
        real_name = next(g["name"] for g in groups if g["id"] == real_gid)

        # Ensure that group is ENABLED (so the empty_shed short-circuit doesn't skip it)
        all_ids = [g["id"] for g in groups]
        api.patch(f"{BASE_URL}/api/farm-config", params={"farm": FARM},
                  json={"enabledGroupIds": all_ids}, timeout=20)

        gsilos = [s for s in silos if s.get("shedGroupId") == real_gid]
        assert gsilos, "Group has no silos"
        # Put 1 t in each silo — total will be <5 t regardless of silo count (<=3 silos)
        payload = {"readings": [
            {"siloId": s["id"], "feedType": "Broiler Grower",
             "amountRemaining": 1.0, "unit": "t", "notes": "TEST_phantom_critical"}
            for s in gsilos
        ]}
        pr = api.post(f"{BASE_URL}/api/readings/batch", params={"farm": FARM},
                      json=payload, timeout=20)
        assert pr.status_code == 201, pr.text

        r = api.get(f"{BASE_URL}/api/farm-buddy/alerts", params={"farm": FARM}, timeout=20)
        assert r.status_code == 200
        body = r.json()
        crit = [a for a in body["alerts"]
                if a.get("level") == "critical" and a.get("shedGroupId") == real_gid]
        assert crit, f"Expected critical low-feed alert for {real_name}, got {body['alerts']}"
        assert "ORDER FEED NOW" in crit[0]["message"]
        # Critical alerts should not carry the empty_shed category
        assert crit[0].get("category") != "empty_shed"

    def test_phantom_shed_excluded_from_alert_shed_ids(
        self, api, farm_config_backup, groups_and_readings_map
    ):
        """
        Belt-and-braces check: even if a phantom shed is disabled, its id must
        NEVER appear anywhere in the returned alerts list (not stale_reading,
        not missing_today, not empty_shed).
        """
        groups, _silos, _with, without_reads = groups_and_readings_map
        assert without_reads
        phantom_gid = next(iter(without_reads))
        all_ids = [g["id"] for g in groups]
        enabled = [gid for gid in all_ids if gid != phantom_gid]
        api.patch(f"{BASE_URL}/api/farm-config", params={"farm": FARM},
                  json={"enabledGroupIds": enabled}, timeout=20)

        r = api.get(f"{BASE_URL}/api/farm-buddy/alerts", params={"farm": FARM}, timeout=20)
        assert r.status_code == 200
        body = r.json()
        offending = [a for a in body["alerts"] if a.get("shedGroupId") == phantom_gid]
        assert offending == [], (
            f"Phantom shed {phantom_gid} still surfaced in alerts: {offending}"
        )


# ── 2) www → apex canonical 301 redirect ────────────────────────────────
class TestWwwToApexRedirect:
    """
    The redirect middleware must return 301 → https://<apex>/<path>[?query]
    whenever the request's Host header starts with 'www.'. Non-www hosts
    (like the preview URL) must pass through unchanged.

    Tested against localhost so we can spoof the Host header without ingress
    rewriting it.
    """

    def test_www_root_redirects_to_apex(self):
        r = requests.get(
            f"{LOCAL_URL}/",
            headers={"Host": "www.broilerbasemate.com.au"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 301, f"Expected 301, got {r.status_code}"
        assert r.headers.get("Location") == "https://broilerbasemate.com.au/", (
            f"Bad Location: {r.headers.get('Location')}"
        )

    def test_www_deep_path_redirects_preserving_path(self):
        r = requests.get(
            f"{LOCAL_URL}/api/farm-buddy/alerts",
            headers={"Host": "www.broilerbasemate.com.au"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 301
        assert r.headers.get("Location") == "https://broilerbasemate.com.au/api/farm-buddy/alerts"

    def test_www_preserves_query_string(self):
        r = requests.get(
            f"{LOCAL_URL}/api/farm-buddy/alerts",
            params={"farm": "default", "debug": "1"},
            headers={"Host": "www.broilerbasemate.com.au"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 301
        loc = r.headers.get("Location", "")
        assert loc.startswith("https://broilerbasemate.com.au/api/farm-buddy/alerts?"), loc
        assert "farm=default" in loc
        assert "debug=1" in loc

    def test_www_case_insensitive_host(self):
        """Host header matching must be case-insensitive per RFC."""
        r = requests.get(
            f"{LOCAL_URL}/foo",
            headers={"Host": "WWW.BroilerBaseMate.COM.AU"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 301
        assert r.headers.get("Location") == "https://broilerbasemate.com.au/foo"

    def test_www_subdomain_of_any_apex_redirects(self):
        """Middleware strips 'www.' prefix regardless of what the apex is."""
        r = requests.get(
            f"{LOCAL_URL}/",
            headers={"Host": "www.example.com"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 301
        assert r.headers.get("Location") == "https://example.com/"

    def test_non_www_host_passes_through(self):
        """Requests without a 'www.' prefix must NOT be redirected."""
        r = requests.get(
            f"{LOCAL_URL}/api/farm-buddy/alerts",
            params={"farm": FARM},
            headers={"Host": "broilerbasemate.com.au"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 200, f"Expected pass-through 200, got {r.status_code}"
        body = r.json()
        assert "riskLevel" in body

    def test_preview_url_passes_through(self):
        """The normal preview host (used by the frontend) must not be redirected."""
        r = requests.get(
            f"{BASE_URL}/api/farm-buddy/alerts",
            params={"farm": FARM},
            allow_redirects=False,
            timeout=20,
        )
        assert r.status_code == 200
        assert "riskLevel" in r.json()

    def test_www_redirect_is_reachable_via_public_url(self):
        """
        Some ingress configurations may forward the Host header; try the public
        preview URL with an explicit Host override. This is best-effort — if the
        edge rewrites Host, the request will pass through (200 or non-301). We
        only fail if we get an unexpected 5xx.
        """
        try:
            r = requests.get(
                f"{BASE_URL}/",
                headers={"Host": "www.broilerbasemate.com.au"},
                allow_redirects=False,
                timeout=15,
            )
        except requests.RequestException as e:
            pytest.skip(f"Public URL unreachable: {e}")
        # We don't assert 301 here — the ingress may strip the Host override.
        # We just make sure the app didn't blow up.
        assert r.status_code < 500, f"Server error on public www test: {r.status_code}"
