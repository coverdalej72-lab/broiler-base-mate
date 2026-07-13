"""Regression test — the End-of-Batch email must produce a valid PDF attachment
so head-office receivers can archive a print-perfect copy alongside the HTML email.
"""
import asyncio
import sys

sys.path.insert(0, "/app/backend")

from server import _render_eob_html, _render_html_to_pdf, EobReport, EobShedRow  # noqa: E402


def test_eob_pdf_renders_from_html():
    r = EobReport(
        farmName="Double B",
        batchNumber=121,
        lastBatchNumber=114,
        batchName="Batch #121",
        generatedDate="13 Jul 2026",
        totalPlaced=532589,
        totalCaught=515872,
        totalMorts=16717,
        mortalityPct=3.14,
        aveWeight=3.069,
        fcr=1.522,
        cfcr=1.355,
        actualAge=41.4,
        correctedAge=33.0,
        totalLiveWeightKg=1567081,
        totalPurchased=2306260,
        sheds=[EobShedRow(shed="1", placed=41000, morts=872, caught=40128, balance=0, mtec=1898)],
    )
    html = _render_eob_html(r, "Double B", "grower@test")
    pdf = asyncio.run(_render_html_to_pdf(html))
    assert pdf is not None, "PDF renderer returned None"
    assert pdf.startswith(b"%PDF"), f"Not a valid PDF (first 4 bytes: {pdf[:4]!r})"
    assert len(pdf) > 5000, f"PDF suspiciously small: {len(pdf)} bytes"


def test_eob_pdf_handles_empty_report():
    """Should still produce a valid PDF for a minimal report (no crash)."""
    r = EobReport(batchName="Fresh Batch")
    html = _render_eob_html(r, "Fresh Farm", "grower@test")
    pdf = asyncio.run(_render_html_to_pdf(html))
    assert pdf is not None
    assert pdf.startswith(b"%PDF")
