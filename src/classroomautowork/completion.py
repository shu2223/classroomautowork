"""Trusted one-click mode: deliver candidates, retain uncertainty outside the answer."""

MODE = "review-candidates-v1"


def candidate_mode(manifest):
    # This context is written by the Python launcher, never copied from course text.
    return manifest.get("generation_context", {}).get("completion_mode") == MODE


def review_cautions(package, receipt):
    import json

    review = json.loads((package / "review.json").read_text(encoding="utf-8"))
    generation = receipt.get("generation", {})
    return list(
        dict.fromkeys(
            [c["requirement"] for c in review["requirement_checks"] if c["status"] != "met"]
            + review.get("questions", [])
            + generation.get("missing_sources", [])
        )
    )


def document_content_gaps(receipt):
    """Actual unanswered work, distinct from identity or candidate-review cautions."""
    gaps = []
    for key in ("remaining_required_fields", "unrecognized_questions"):
        if receipt.get(key):
            gaps.extend(receipt[key])
        else:
            for document in receipt.get("documents", []):
                gaps.extend(document.get(key, []))
    return gaps


def document_answers_incomplete(item):
    receipt = item.get("document_fill") or {}
    return bool(
        item.get("requires_regeneration")
        or document_content_gaps(receipt)
        or receipt.get("status") == "partial"
        or any(document.get("status") == "partial" for document in receipt.get("documents", []))
    )


def document_fill_outcome(receipt):
    """Only completed academic fields allow a document to enter the skip list."""
    gaps = document_content_gaps(receipt)
    if document_answers_incomplete({"document_fill": receipt}):
        labels = [
            gap.get("label", gap.get("id", "")) if isinstance(gap, dict) else str(gap)
            for gap in gaps
        ]
        detail = "；".join(dict.fromkeys(label for label in labels if label))[:400]
        return {
            "status": "needs_document",
            "requires_regeneration": True,
            "error": (
                f"原文档仍有 {len(gaps)} 个题目或答案栏未完成。"
                if gaps
                else "原文档尚有未完成的题目或答案栏。"
            )
            + (detail + "。" if detail else "")
            + "重试会重新核对题目并补全答案。",
        }
    warnings = receipt.get("identity_warnings", [])
    return {
        "status": "document_needs_user" if warnings else "document_ready",
        "requires_regeneration": False,
        "error": "；".join(warnings) or None,
    }
