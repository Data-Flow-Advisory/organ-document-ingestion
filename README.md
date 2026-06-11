# organ-document-ingestion

A **pure decision organ** for the Data-Flow-Advisory orchestrator. It decides
how to ingest an unstructured uploaded document (PDF / Word / image / plain
text) into the discovery pipeline — without performing any of the actual
I/O.

It extracts the pure *routing* logic from discovery-engine's
[`app/services/document_ingestion.py`](https://github.com/Data-Flow-Advisory/discovery-engine/blob/main/app/services/document_ingestion.py).
The impure parts (reading the file, the AI classifier call, DB writes) stay in
the Flask service; this organ consumes a document **descriptor** and returns a
structured ingestion plan.

## Contract

```python
decide(state: dict, context: dict) -> dict
```

Returns `{output, rationale, self_metric}` where `self_metric.confidence` is a
float in `[0.0, 1.0]`. The organ is:

- **Pure** — no file I/O, no API hits, no global state.
- **Deterministic** — same input → same output.
- **Stdlib-only** — `typing` only; Python 3.8+.
- **Fail-safe** — empty/`None` state returns a valid `noop` plan, never raises.

## Input: a document descriptor

```json
{
  "content_type": "application/pdf",
  "original_filename": "YPZ_section13_spec.pdf",
  "status": "uploaded",
  "candidate_classification": "spec_doc",
  "extracted_text_length": 18400,
  "project_id": null,
  "projects": [
    {"id": 1, "slug": "default"},
    {"id": 2, "slug": "property-discovery"}
  ]
}
```

All keys are optional. `status` follows the Flask pipeline's state machine:
`uploaded → extracting → classified → facts_extracted | failed`.

### Context overrides

```json
{
  "allowed_classifications": ["spec_doc", "statement", "invoice",
                              "compliance_cert", "email_thread", "other"],
  "truncate_limit": 50000
}
```

## Output: an ingestion plan

```json
{
  "output": {
    "next_status": "extracting",
    "should_skip": false,
    "overall": "ingest",
    "extraction_method": "pdf_extract",
    "classification": "spec_doc",
    "target_project_id": 2,
    "target_project_reason": "first_non_default",
    "truncate": false,
    "truncated_length": 18400,
    "actions": [
      {"step": "extract_text", "method": "pdf_extract", "urgency": "immediate"},
      {"step": "classify", "expected_label": "spec_doc", "urgency": "immediate"},
      {"step": "synthesise_interview", "target_project_id": 2,
       "target_project_reason": "first_non_default", "truncate": false, "urgency": "soon"},
      {"step": "extract_facts", "urgency": "soon"}
    ]
  },
  "rationale": "Ingesting document: extract via 'pdf_extract'; classification 'spec_doc'; route to project (first_non_default).",
  "self_metric": {"confidence": 0.9}
}
```

## The decisions, extracted

| Decision | Source in `document_ingestion.py` | Organ logic |
|----------|-----------------------------------|-------------|
| **Extraction method** | `_extraction_method_for()` | content-type / extension → `pdf_extract` / `docx` / `ocr` / `plain` |
| **Classification** | `_classify()` validation tail | normalise candidate label against the allowed set; fall back to `other` |
| **Target project** | `_pick_target_project()` | explicit → first non-`default` → `default` → must-create |
| **Idempotency** | `ingest_uploaded_document()` top guard | `status == facts_extracted` → skip (no-op) |
| **Truncation** | the 50k answer cap in `_synthesise_into_interview()` | `extracted_text_length > truncate_limit` → truncate |

## Confidence

Starts at `0.5` and adjusts:

- `+0.25` recognised content type (PDF/DOCX/image); `-0.1` on a plain-text guess
- `+0.15` a candidate classification was already proposed
- `+0.1` a concrete target project exists; `-0.1` when one must be minted
- Terminal-status skip → `1.0` (idempotency is certain)

Clamped to `[0.1, 1.0]`.

## Usage

```python
from organ import decide

state = {
    "content_type": "application/pdf",
    "original_filename": "spec.pdf",
    "candidate_classification": "spec_doc",
    "projects": [{"id": 1, "slug": "default"}, {"id": 2, "slug": "property-discovery"}],
}
plan = decide(state, {})
# plan["output"]["extraction_method"] == "pdf_extract"
# plan["output"]["target_project_id"] == 2
```

See [`samples/usage_example.py`](samples/usage_example.py) for a full run.

## Orchestrator integration

1. The Flask upload route records an `UploadedDocument` and builds a descriptor.
2. Feed the descriptor to `decide(state, context)`.
3. The orchestrator follows the plan: run the named extractor, call the AI
   classifier (validating its label against the organ's `classification`),
   synthesise the Interview on `target_project_id`, truncating if asked.
4. High-confidence plans run unattended; low-confidence ones (`must_create_default`,
   plain-text guesses) can be flagged for review.

## Tests

```bash
pip install pytest
python -m pytest test_organ.py -v
```

CI runs the conformance suite on Python 3.8–3.12
(`.github/workflows/conformance.yml`).

## Related

- [`organ-api-health`](https://github.com/Data-Flow-Advisory/organ-api-health) — sibling organ (API health prioritisation)
- Orchestrator `CONTRACT.md` — the pure-organ contract this implements
