"""Usage example for the document-ingestion organ.

Run: python samples/usage_example.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from organ import decide  # noqa: E402


def run(label, state, context=None):
    result = decide(state, context or {})
    out = result["output"]
    print(f"\n=== {label} ===")
    print(f"next_status     : {out['next_status']}")
    print(f"should_skip     : {out['should_skip']}")
    print(f"extraction      : {out.get('extraction_method')}")
    print(f"classification  : {out.get('classification')}")
    print(f"target_project  : {out.get('target_project_id')} ({out.get('target_project_reason')})")
    print(f"truncate        : {out.get('truncate')} -> {out.get('truncated_length')}")
    print(f"confidence      : {result['self_metric']['confidence']:.0%}")
    print(f"rationale       : {result['rationale']}")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    for fname, label in [
        ("pdf_spec_state.json", "PDF spec -> property-discovery"),
        ("image_scan_state.json", "Scanned image (OCR), no candidate label"),
        ("already_ingested_state.json", "Idempotent skip"),
    ]:
        with open(os.path.join(here, fname)) as f:
            run(label, json.load(f))

    run("Empty state (fail-safe)", {})
    run(
        "Failed doc -> reprocess",
        {"status": "failed", "content_type": "application/pdf", "original_filename": "a.pdf",
         "projects": [{"id": 1, "slug": "default"}]},
    )
