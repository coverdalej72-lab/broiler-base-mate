"""Regression test — the End-of-Batch email must include every processor-standard
metric growers get on their settlement sheet.

Previously the email only included: totalPlaced/Caught/Morts/Mortality/AveWt/FCR/cFCR/TotalFeed
Missing (rendered as '—' in the email): Total KG picked up, Ave Age, Corr. Age

Guards against regressions when someone touches _render_eob_html or the EobReport model.
"""
import sys
sys.path.insert(0, "/app/backend")

from server import _render_eob_html, EobReport  # noqa: E402


def test_eob_email_renders_all_processor_metrics():
    r = EobReport(
        batchName="Batch #2601",
        totalPlaced=526815,
        totalCaught=510544,
        totalMorts=16271,
        mortalityPct=3.09,
        aveWeight=3.069,
        fcr=1.522,
        cfcr=1.355,
        actualAge=41.4,
        correctedAge=33.0,
        totalLiveWeightKg=1567081,
        totalPurchased=2385180,
    )
    html = _render_eob_html(r, "Test Farm", "grower@test")

    # Labels
    for label in ["Ave Weight", "Total KG", "Ave Age", "Corr. Age", "FCR", "cFCR to 2.45", "Total Feed"]:
        assert label in html, f"Missing label: {label}"

    # Values
    assert "526,815" in html    # birds placed
    assert "510,544" in html    # caught
    assert "16,271" in html     # morts
    assert "3.09" in html       # mortality %
    assert "3.069" in html      # ave weight kg
    assert "1,567,081" in html  # total live weight kg
    assert "41.4" in html       # actual age
    assert "33.0" in html       # corrected age
    assert "1.522" in html      # fcr
    assert "1.355" in html      # cfcr
    assert "2,385,180" in html  # total feed


def test_eob_missing_metrics_render_as_dash():
    """A minimal EobReport (only batchName) must still render without crashing —
    every unspecified KPI tile shows '—' instead of throwing."""
    r = EobReport(batchName="Fresh Batch")
    html = _render_eob_html(r, "Test Farm", "grower@test")
    assert "END OF BATCH REPORT" in html
    assert "Fresh Batch" in html
    # New tiles present but empty
    for label in ["Total KG", "Ave Age", "Corr. Age", "cFCR to 2.45"]:
        assert label in html
