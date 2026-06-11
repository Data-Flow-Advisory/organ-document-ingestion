"""Document Ingestion Decision Organ - Pure organ per orchestrator CONTRACT.

This organ decides how to ingest an unstructured uploaded document (PDF,
Word, image, plain text) into the discovery pipeline. It extracts the
pure *routing* decisions from discovery-engine's
``app/services/document_ingestion.py`` — the impure parts (file I/O, the
AI classifier call, DB writes) stay in the Flask service; this organ
consumes a document descriptor and decides:

  * which extraction method applies (pdf / docx / ocr / plain),
  * the validated/normalised classification label,
  * which project the synthesised Interview belongs to,
  * whether the pipeline should be skipped (idempotency),
  * whether the extracted text must be truncated for storage,
  * the next pipeline status to move the document to.

Pure: no side effects, deterministic, stdlib-only.

Signature: decide(state: dict, context: dict) -> dict
Returns: {output, rationale, self_metric} with self_metric.confidence required.
"""
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants (mirrors app/services/document_ingestion.py)
# ---------------------------------------------------------------------------

# Allowed classification labels. The AI classifier (impure, lives in the
# Flask service) proposes a label; this organ validates it against the set
# and falls back to ``other`` on anything unknown.
DEFAULT_ALLOWED_CLASSIFICATIONS: Tuple[str, ...] = (
    "spec_doc",
    "statement",
    "invoice",
    "compliance_cert",
    "email_thread",
    "other",
)

CLASSIFICATION_FALLBACK = "other"

# Cap the answer payload to avoid bloating one DB row with a 450-page PDF.
DEFAULT_TRUNCATE_LIMIT = 50_000

# Pipeline state machine: uploaded -> extracting -> classified
#                                  -> facts_extracted | failed
TERMINAL_STATUS = "facts_extracted"

# Extraction-method labels + the content-types / extensions that map to them.
_DOCX_CONTENT_TYPES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
)
_DOCX_EXTENSIONS = (".docx", ".doc")
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tiff", ".gif")


def decide(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Decide the ingestion plan for an uploaded document.

    Args:
        state: A document descriptor. All keys optional (fail-safe):
            {
              "content_type": "application/pdf",
              "original_filename": "spec.pdf",
              "status": "uploaded",            # current pipeline status
              "candidate_classification": "spec_doc",  # AI's proposed label
              "extracted_text_length": 12000,  # chars of extracted text
              "project_id": null,              # explicit target project
              "projects": [                    # the tenant's projects
                  {"id": 1, "slug": "default"},
                  {"id": 2, "slug": "property-discovery"}
              ]
            }
            Empty dict {} is valid (fail-safe -> noop plan).
        context: Optional overrides:
            {
              "allowed_classifications": [...],
              "truncate_limit": 50000
            }

    Returns:
        {
            "output": { ...ingestion plan... },
            "rationale": explanation_string,
            "self_metric": {"confidence": 0.0-1.0}
        }
    """
    state = state or {}
    context = context or {}

    allowed = tuple(context.get("allowed_classifications") or DEFAULT_ALLOWED_CLASSIFICATIONS)
    truncate_limit = context.get("truncate_limit", DEFAULT_TRUNCATE_LIMIT)
    try:
        truncate_limit = int(truncate_limit)
    except (TypeError, ValueError):
        truncate_limit = DEFAULT_TRUNCATE_LIMIT

    # ---- Fail-safe: empty / null state ------------------------------------
    if not state:
        return {
            "output": {
                "next_status": "noop",
                "should_skip": True,
                "extraction_method": None,
                "classification": None,
                "target_project_id": None,
                "target_project_reason": "no_state",
                "truncate": False,
                "truncated_length": None,
                "actions": [],
            },
            "rationale": "No document state provided; nothing to ingest.",
            "self_metric": {"confidence": 0.3},
        }

    status = str(state.get("status") or "uploaded").strip().lower()
    content_type = state.get("content_type")
    filename = state.get("original_filename")

    # ---- Idempotency: already fully ingested -> skip ----------------------
    if status == TERMINAL_STATUS:
        return {
            "output": {
                "next_status": "skip",
                "should_skip": True,
                "extraction_method": None,
                "classification": _normalise_classification(
                    state.get("candidate_classification"), allowed
                ),
                "target_project_id": state.get("project_id"),
                "target_project_reason": "already_ingested",
                "truncate": False,
                "truncated_length": None,
                "actions": [
                    {
                        "step": "skip",
                        "reason": "document already at terminal status",
                        "urgency": "none",
                    }
                ],
            },
            "rationale": (
                "Document is already 'facts_extracted'; pipeline is idempotent "
                "so this is a no-op."
            ),
            "self_metric": {"confidence": 1.0},
        }

    # ---- Active pipeline plan ---------------------------------------------
    method, method_certain = _extraction_method_for(content_type, filename)
    classification = _normalise_classification(
        state.get("candidate_classification"), allowed
    )
    classification_known = bool(state.get("candidate_classification"))

    target_project_id, project_reason = _pick_target_project(state)

    text_len = state.get("extracted_text_length")
    truncate, truncated_length = _truncation_decision(text_len, truncate_limit)

    # Reprocessing a failed document resets it; a fresh one begins extraction.
    next_status = "extracting"

    actions = _build_actions(
        method=method,
        classification=classification,
        target_project_id=target_project_id,
        project_reason=project_reason,
        truncate=truncate,
        was_failed=(status == "failed"),
    )

    overall = "reprocess" if status == "failed" else "ingest"
    rationale = _build_rationale(
        status, method, method_certain, classification,
        classification_known, project_reason, truncate,
    )
    confidence = _compute_confidence(
        state, method_certain, classification_known, project_reason
    )

    return {
        "output": {
            "next_status": next_status,
            "should_skip": False,
            "overall": overall,
            "extraction_method": method,
            "classification": classification,
            "target_project_id": target_project_id,
            "target_project_reason": project_reason,
            "truncate": truncate,
            "truncated_length": truncated_length,
            "actions": actions,
        },
        "rationale": rationale,
        "self_metric": {"confidence": confidence},
    }


# ---------------------------------------------------------------------------
# Extraction-method selection (pure mirror of _extraction_method_for)
# ---------------------------------------------------------------------------


def _extraction_method_for(
    content_type: Optional[str], filename: Optional[str]
) -> Tuple[str, bool]:
    """Choose an extraction method from content_type / filename extension.

    Returns ``(method, certain)`` where ``certain`` is True when the type was
    positively recognised (PDF / DOCX / image) and False when we fell back to
    the ``plain`` catch-all (an uncertain guess).
    """
    ct = (content_type or "").lower()
    name = (filename or "").lower()

    if ct == "application/pdf" or name.endswith(".pdf"):
        return "pdf_extract", True
    if ct in _DOCX_CONTENT_TYPES or name.endswith(_DOCX_EXTENSIONS):
        return "docx", True
    if ct.startswith("image/") or name.endswith(_IMAGE_EXTENSIONS):
        return "ocr", True
    # Plain is the catch-all: everything that isn't a positively-recognised
    # structured format (PDF / DOCX / image) lands here, including unknown
    # binary types. Treated as an uncertain route regardless of whether a
    # content_type was supplied.
    return "plain", False


# ---------------------------------------------------------------------------
# Classification normalisation (pure mirror of _classify's validation tail)
# ---------------------------------------------------------------------------


def _normalise_classification(
    candidate: Optional[str], allowed: Tuple[str, ...]
) -> str:
    """Validate the AI's proposed label against ``allowed``; fall back to
    ``other`` on missing / unknown values."""
    if not candidate:
        return CLASSIFICATION_FALLBACK
    label = str(candidate).strip().lower()
    if label in allowed:
        return label
    return CLASSIFICATION_FALLBACK


# ---------------------------------------------------------------------------
# Project routing (pure mirror of _pick_target_project)
# ---------------------------------------------------------------------------


def _pick_target_project(state: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Resolve the target project id + the reason it was chosen.

    Priority (matches the Flask service):
      1. explicit ``project_id`` if it is present among ``projects``.
      2. the first non-``default`` project (bespoke sub-project pattern).
      3. the ``default`` project.
      4. none available -> must create a default.
    """
    projects = state.get("projects") or []
    by_id = {p.get("id"): p for p in projects if isinstance(p, dict)}

    explicit = state.get("project_id")
    if explicit is not None and explicit in by_id:
        return explicit, "explicit"

    # First non-default project, ordered by id ascending (stable).
    non_default = sorted(
        (p for p in projects if isinstance(p, dict) and p.get("slug") != "default"),
        key=lambda p: (p.get("id") is None, p.get("id")),
    )
    if non_default:
        return non_default[0].get("id"), "first_non_default"

    default = next(
        (p for p in projects if isinstance(p, dict) and p.get("slug") == "default"),
        None,
    )
    if default:
        return default.get("id"), "default"

    return None, "must_create_default"


# ---------------------------------------------------------------------------
# Truncation (pure mirror of the 50k answer cap)
# ---------------------------------------------------------------------------


def _truncation_decision(
    text_len: Any, limit: int
) -> Tuple[bool, Optional[int]]:
    """Decide whether the extracted text must be truncated for storage."""
    if text_len is None:
        return False, None
    try:
        n = int(text_len)
    except (TypeError, ValueError):
        return False, None
    if n > limit:
        return True, limit
    return False, n


# ---------------------------------------------------------------------------
# Action list + rationale + confidence
# ---------------------------------------------------------------------------


def _build_actions(
    *,
    method: str,
    classification: str,
    target_project_id: Optional[int],
    project_reason: str,
    truncate: bool,
    was_failed: bool,
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    if was_failed:
        actions.append(
            {"step": "reset", "reason": "reprocessing a failed document", "urgency": "soon"}
        )
    actions.append(
        {"step": "extract_text", "method": method, "urgency": "immediate"}
    )
    actions.append(
        {"step": "classify", "expected_label": classification, "urgency": "immediate"}
    )
    actions.append(
        {
            "step": "synthesise_interview",
            "target_project_id": target_project_id,
            "target_project_reason": project_reason,
            "truncate": truncate,
            "urgency": "soon",
        }
    )
    actions.append(
        {"step": "extract_facts", "urgency": "soon"}
    )
    return actions


def _build_rationale(
    status: str,
    method: str,
    method_certain: bool,
    classification: str,
    classification_known: bool,
    project_reason: str,
    truncate: bool,
) -> str:
    lead = "Reprocessing failed document" if status == "failed" else "Ingesting document"
    method_note = (
        f"extract via '{method}'"
        if method_certain
        else f"unknown type — falling back to '{method}' extraction"
    )
    class_note = (
        f"classification '{classification}'"
        if classification_known
        else f"no candidate label — defaulting to '{classification}'"
    )
    trunc_note = " Text exceeds storage cap and will be truncated." if truncate else ""
    return (
        f"{lead}: {method_note}; {class_note}; route to project "
        f"({project_reason}).{trunc_note}"
    )


def _compute_confidence(
    state: Dict[str, Any],
    method_certain: bool,
    classification_known: bool,
    project_reason: str,
) -> float:
    """Confidence in the routing plan.

    Higher when the document type is positively recognised, the classifier
    has already proposed a label, and a concrete project exists. Lower when
    falling back to plain-text guessing or when a project must be minted.
    """
    confidence = 0.5

    # Recognised content type is the strongest signal.
    if method_certain:
        confidence += 0.25
    else:
        confidence -= 0.1

    # A known candidate classification firms up the downstream plan.
    if classification_known:
        confidence += 0.15

    # Routing certainty.
    if project_reason in ("explicit", "first_non_default", "default"):
        confidence += 0.1
    else:  # must_create_default — uncertain
        confidence -= 0.1

    return round(max(0.1, min(1.0, confidence)), 2)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "decide",
    "DEFAULT_ALLOWED_CLASSIFICATIONS",
    "CLASSIFICATION_FALLBACK",
    "DEFAULT_TRUNCATE_LIMIT",
]
