"""Regression tests for the Grower Pickup Report reconciliation endpoint.

Exercises .docx upload parsing (via a downloaded sample file), the JSON
paste-text path, and the reconcile diff shape.
"""
import json
import os
from pathlib import Path

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or "http://localhost:8001"

SAMPLE_DOCX = Path("/tmp/pickup.docx")


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    return s


@pytest.mark.skipif(not SAMPLE_DOCX.exists(), reason="sample docx not downloaded")
def test_reconcile_docx_parses_all_82_rows(api):
    with SAMPLE_DOCX.open("rb") as f:
        r = api.post(
            f"{BASE_URL}/api/farm-buddy/reconcile-pickup-report-upload",
            files={"file": ("pickup.docx", f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            data={"appCatches": "[]", "generateNarrative": "false"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["reportRows"] == 82
    assert body["totals"]["reportBirds"] > 0
    # All 82 rows should be flagged missing (empty appCatches)
    assert body["totals"]["missing"] == 82


@pytest.mark.skipif(not SAMPLE_DOCX.exists(), reason="sample docx not downloaded")
def test_reconcile_matches_correctly(api):
    with SAMPLE_DOCX.open("rb") as f:
        r = api.post(
            f"{BASE_URL}/api/farm-buddy/reconcile-pickup-report-upload",
            files={"file": ("pickup.docx", f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            data={
                "appCatches": json.dumps([
                    {"shedNum": 1, "date": "28/08/2026", "age": 46, "birds": 2208, "aveWgt": 3.45},
                    {"shedNum": 3, "date": "28/08/2026", "age": 46, "birds": 3872, "aveWgt": 3.58},
                ]),
                "generateNarrative": "false",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["matched"] == 2
    assert body["totals"]["missing"] == 80


def test_reconcile_empty_body(api):
    r = api.post(
        f"{BASE_URL}/api/farm-buddy/reconcile-pickup-report",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"pickupText": "", "appCatches": [], "generateNarrative": False}),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["reportRows"] == 0
    assert body["totals"]["appRows"] == 0


def test_upload_rejects_empty_file(api):
    r = api.post(
        f"{BASE_URL}/api/farm-buddy/reconcile-pickup-report-upload",
        files={"file": ("empty.txt", b"", "text/plain")},
        data={"appCatches": "[]", "generateNarrative": "false"},
    )
    assert r.status_code == 400
    assert "pickup rows" in r.text.lower()
