"""Tests for the document-ingestion decision organ.

Covers the pure-organ contract (signature, return shape, determinism,
fail-safe, stdlib-only) plus the extracted routing logic: extraction-method
selection, classification normalisation, project routing, idempotency, and
truncation.
"""
import json
import os

import pytest

from organ import (
    CLASSIFICATION_FALLBACK,
    DEFAULT_ALLOWED_CLASSIFICATIONS,
    DEFAULT_TRUNCATE_LIMIT,
    decide,
)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_return_shape():
    result = decide({"content_type": "application/pdf"}, {})
    assert "output" in result
    assert "rationale" in result
    assert "self_metric" in result
    assert "confidence" in result["self_metric"]
    conf = result["self_metric"]["confidence"]
    assert 0.0 <= conf <= 1.0


def test_signature():
    import inspect

    params = list(inspect.signature(decide).parameters.keys())
    assert params == ["state", "context"]


def test_empty_state_failsafe():
    result = decide({}, {})
    assert result["output"]["next_status"] == "noop"
    assert result["output"]["should_skip"] is True
    assert 0.0 <= result["self_metric"]["confidence"] <= 1.0


def test_none_state_failsafe():
    result = decide(None, None)
    assert result["output"]["next_status"] == "noop"


def test_determinism():
    state = {
        "content_type": "application/pdf",
        "original_filename": "spec.pdf",
        "candidate_classification": "spec_doc",
        "extracted_text_length": 12000,
        "projects": [{"id": 1, "slug": "default"}, {"id": 2, "slug": "property-discovery"}],
    }
    r1 = decide(state, {})
    r2 = decide(state, {})
    assert r1 == r2


def test_no_side_effects_on_input():
    state = {"content_type": "application/pdf", "projects": [{"id": 1, "slug": "default"}]}
    snapshot = json.dumps(state, sort_keys=True)
    decide(state, {})
    assert json.dumps(state, sort_keys=True) == snapshot


def test_stdlib_only():
    import ast

    here = os.path.dirname(__file__)
    with open(os.path.join(here, "organ.py")) as f:
        tree = ast.parse(f.read())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module)
    stdlib_allowed = {"typing"}
    external = [i for i in imports if i and i not in stdlib_allowed]
    assert not external, f"Non-stdlib imports: {external}"


# ---------------------------------------------------------------------------
# Extraction-method selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ct,name,expected",
    [
        ("application/pdf", "x.pdf", "pdf_extract"),
        ("application/pdf", "x.bin", "pdf_extract"),
        (None, "report.pdf", "pdf_extract"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "x.docx",
            "docx",
        ),
        ("application/msword", "x.doc", "docx"),
        (None, "notes.docx", "docx"),
        ("image/png", "shot.png", "ocr"),
        (None, "scan.jpeg", "ocr"),
        (None, "diagram.tiff", "ocr"),
        ("text/plain", "readme.txt", "plain"),
        ("application/octet-stream", "weird.xyz", "plain"),
    ],
)
def test_extraction_method(ct, name, expected):
    result = decide({"content_type": ct, "original_filename": name}, {})
    assert result["output"]["extraction_method"] == expected


def test_pdf_is_high_confidence():
    pdf = decide({"content_type": "application/pdf", "original_filename": "a.pdf"}, {})
    plain = decide({"content_type": "text/plain", "original_filename": "a.txt"}, {})
    assert pdf["self_metric"]["confidence"] > plain["self_metric"]["confidence"]


def test_unknown_type_low_confidence():
    # No content_type and no filename: pure guess -> plain, lowest confidence.
    result = decide({"status": "uploaded"}, {})
    assert result["output"]["extraction_method"] == "plain"
    assert result["self_metric"]["confidence"] < 0.6


# ---------------------------------------------------------------------------
# Classification normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", list(DEFAULT_ALLOWED_CLASSIFICATIONS))
def test_valid_classifications_pass_through(label):
    result = decide({"candidate_classification": label}, {})
    assert result["output"]["classification"] == label


def test_unknown_classification_falls_back():
    result = decide({"candidate_classification": "rocket_science"}, {})
    assert result["output"]["classification"] == CLASSIFICATION_FALLBACK


def test_missing_classification_falls_back():
    result = decide({"content_type": "application/pdf"}, {})
    assert result["output"]["classification"] == CLASSIFICATION_FALLBACK


def test_classification_case_insensitive():
    result = decide({"candidate_classification": "  SPEC_DOC "}, {})
    assert result["output"]["classification"] == "spec_doc"


def test_custom_allowed_classifications():
    ctx = {"allowed_classifications": ["legal_doc", "other"]}
    assert decide({"candidate_classification": "legal_doc"}, ctx)["output"]["classification"] == "legal_doc"
    # spec_doc no longer allowed -> fallback
    assert decide({"candidate_classification": "spec_doc"}, ctx)["output"]["classification"] == "other"


# ---------------------------------------------------------------------------
# Project routing
# ---------------------------------------------------------------------------


def test_explicit_project_wins():
    state = {
        "project_id": 7,
        "projects": [{"id": 7, "slug": "default"}, {"id": 9, "slug": "domain"}],
    }
    out = decide(state, {})["output"]
    assert out["target_project_id"] == 7
    assert out["target_project_reason"] == "explicit"


def test_explicit_project_not_in_list_falls_through():
    state = {
        "project_id": 99,  # not present
        "projects": [{"id": 1, "slug": "default"}, {"id": 2, "slug": "domain"}],
    }
    out = decide(state, {})["output"]
    assert out["target_project_id"] == 2  # first non-default
    assert out["target_project_reason"] == "first_non_default"


def test_first_non_default_chosen():
    state = {
        "projects": [
            {"id": 1, "slug": "default"},
            {"id": 3, "slug": "property-discovery"},
            {"id": 5, "slug": "analytics"},
        ]
    }
    out = decide(state, {})["output"]
    assert out["target_project_id"] == 3
    assert out["target_project_reason"] == "first_non_default"


def test_default_when_only_default():
    state = {"projects": [{"id": 1, "slug": "default"}]}
    out = decide(state, {})["output"]
    assert out["target_project_id"] == 1
    assert out["target_project_reason"] == "default"


def test_must_create_when_no_projects():
    out = decide({"content_type": "application/pdf"}, {})["output"]
    assert out["target_project_id"] is None
    assert out["target_project_reason"] == "must_create_default"


def test_non_default_ordered_by_id():
    state = {
        "projects": [
            {"id": 8, "slug": "b"},
            {"id": 2, "slug": "a"},
            {"id": 1, "slug": "default"},
        ]
    }
    assert decide(state, {})["output"]["target_project_id"] == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_terminal_status_skips():
    state = {"status": "facts_extracted", "content_type": "application/pdf"}
    result = decide(state, {})
    assert result["output"]["should_skip"] is True
    assert result["output"]["next_status"] == "skip"
    assert result["self_metric"]["confidence"] == 1.0


def test_uploaded_status_proceeds():
    result = decide({"status": "uploaded", "content_type": "application/pdf"}, {})
    assert result["output"]["should_skip"] is False
    assert result["output"]["next_status"] == "extracting"


def test_failed_status_reprocesses():
    result = decide({"status": "failed", "content_type": "application/pdf"}, {})
    assert result["output"]["next_status"] == "extracting"
    assert result["output"]["overall"] == "reprocess"
    steps = [a["step"] for a in result["output"]["actions"]]
    assert "reset" in steps


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_truncation_triggered():
    state = {"content_type": "text/plain", "extracted_text_length": 60000}
    out = decide(state, {})["output"]
    assert out["truncate"] is True
    assert out["truncated_length"] == DEFAULT_TRUNCATE_LIMIT


def test_no_truncation_under_limit():
    state = {"content_type": "text/plain", "extracted_text_length": 1000}
    out = decide(state, {})["output"]
    assert out["truncate"] is False
    assert out["truncated_length"] == 1000


def test_truncation_custom_limit():
    state = {"content_type": "text/plain", "extracted_text_length": 600}
    out = decide(state, {"truncate_limit": 500})["output"]
    assert out["truncate"] is True
    assert out["truncated_length"] == 500


def test_missing_text_length_no_truncation():
    out = decide({"content_type": "text/plain"}, {})["output"]
    assert out["truncate"] is False
    assert out["truncated_length"] is None


def test_bad_text_length_failsafe():
    out = decide({"extracted_text_length": "not-a-number"}, {})["output"]
    assert out["truncate"] is False


# ---------------------------------------------------------------------------
# Action shape
# ---------------------------------------------------------------------------


def test_actions_cover_pipeline():
    state = {
        "content_type": "application/pdf",
        "original_filename": "spec.pdf",
        "candidate_classification": "spec_doc",
        "projects": [{"id": 1, "slug": "default"}, {"id": 2, "slug": "domain"}],
    }
    steps = [a["step"] for a in decide(state, {})["output"]["actions"]]
    assert steps == ["extract_text", "classify", "synthesise_interview", "extract_facts"]


def test_every_action_has_urgency():
    out = decide({"status": "failed", "content_type": "application/pdf"}, {})["output"]
    for action in out["actions"]:
        assert action["urgency"] in {"immediate", "soon", "none"}


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------


def test_sample_files_decide_cleanly():
    here = os.path.dirname(__file__)
    samples_dir = os.path.join(here, "samples")
    for fname in ("pdf_spec_state.json", "image_scan_state.json", "already_ingested_state.json"):
        path = os.path.join(samples_dir, fname)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            state = json.load(f)
        result = decide(state, {})
        assert isinstance(result["output"], dict)
        assert 0.0 <= result["self_metric"]["confidence"] <= 1.0
        assert isinstance(result["rationale"], str) and result["rationale"]
