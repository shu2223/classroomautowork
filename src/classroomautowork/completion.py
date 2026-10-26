"""Trusted one-click mode: deliver candidates, retain uncertainty outside the answer."""

from .form_fill import form_gap_reason

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


def _form_fields(receipt, key):
    if key in receipt:
        return receipt.get(key) or []
    return [field for form in receipt.get("forms", []) for field in form.get(key, [])]


def form_content_gaps(receipt):
    """Academic omissions; older prefill receipts only recorded all blank fields."""
    if "remaining_answer_fields" in receipt:
        gaps = list(receipt.get("remaining_answer_fields") or [])
    else:
        gaps = []
        for form in receipt.get("forms", []):
            if "remaining_answer_fields" in form:
                gaps.extend(form.get("remaining_answer_fields") or [])
            else:
                gaps.extend(
                    field for field in form.get("remaining_fields", []) if field.get("required")
                )
        if not receipt.get("forms"):
            gaps.extend(
                field for field in receipt.get("remaining_fields", []) if field.get("required")
            )
    # Old receipts mislabeled any needs_user blank as personal information. Fix
    # the retry decision immediately, even before cached model output is reread.
    gaps.extend(
        {**field, "gap_reason": _form_gap_reason(field)}
        for field in _form_fields(receipt, "user_input_fields")
        if field.get("required") and _form_gap_reason(field) != "personal_fact"
    )
    unique = {}
    for field in gaps:
        key = (field.get("form_url", ""), field.get("entry_id") or field.get("title"))
        unique[key] = field
    return list(unique.values())


def _form_gap_reason(field):
    return field.get("gap_reason") or form_gap_reason(
        field.get("title", ""), field.get("review_note", "")
    )


def form_user_input_gaps(receipt):
    return [
        field
        for key in ("user_input_fields", "unsupported_fields")
        for field in _form_fields(receipt, key)
        if field.get("required")
        and (key == "unsupported_fields" or _form_gap_reason(field) == "personal_fact")
    ]


def form_generation_incomplete(item):
    receipt = item.get("form_fill") or {}
    mapping_complete = receipt.get(
        "form_mapping_complete", (item.get("generation") or {}).get("form_mapping_complete")
    )
    return bool(
        (
            item.get("form_requires_regeneration")
            and receipt.get("form_mapping_complete") is not True
        )
        or mapping_complete is False
        or form_content_gaps(receipt)
    )


def form_answers_incomplete(item):
    receipt = item.get("form_fill") or {}
    return bool(form_generation_incomplete(item) or form_user_input_gaps(receipt))


def answers_incomplete(item):
    return document_answers_incomplete(item) or form_answers_incomplete(item)


def form_fill_outcome(receipt, generation=None):
    missing = form_content_gaps(receipt)
    missing_sources = [field for field in missing if _form_gap_reason(field) == "missing_source"]
    user_input = form_user_input_gaps(receipt)
    unsupported = [
        field for field in _form_fields(receipt, "unsupported_fields") if field.get("required")
    ]
    mapping_complete = receipt.get(
        "form_mapping_complete", (generation or {}).get("form_mapping_complete")
    )
    regenerate = bool(missing or mapping_complete is False)
    labels = [
        field.get("title") or field.get("label") or field.get("entry_id", "")
        for field in missing + user_input
        if _form_gap_reason(field) != "missing_source"
    ]
    source_notes = list(
        dict.fromkeys(
            field.get("review_note") or field.get("title", "") for field in missing_sources
        )
    )[:2]
    detail = "；".join(dict.fromkeys(label for label in source_notes + labels if label))[:400]
    if regenerate or user_input:
        message = (
            f"原表单仍有 {len(missing_sources)} 个必答题缺少作答资料"
            + (
                f"，另有 {len(missing) - len(missing_sources)} 个必答题未填写。"
                if len(missing) > len(missing_sources)
                else "。"
            )
            if missing_sources
            else f"原表单仍有 {len(missing)} 个必答题未填写。"
            if missing
            else "原表单的必答题映射尚未完成。"
            if regenerate
            else f"原表单仍有 {len(user_input)} 栏必填内容需要在原页面完成。"
            if unsupported
            else f"原表单仍有 {len(user_input)} 栏必填信息需要补充。"
        )
        return {
            "status": "needs_form",
            "form_requires_regeneration": regenerate,
            "error": message
            + (detail + "。" if detail else "")
            + (
                "重试会重新查找资料并补全答案，已有初稿会保留。"
                if missing_sources
                else "重试会重新核对题目并补全答案，已有初稿会保留。"
                if regenerate
                else "请在原表单操作这些原生控件；已有答案会保留。"
                if unsupported
                else "请补充后重新填入；已有答案会保留。"
            ),
        }
    return {"status": "form_opened", "form_requires_regeneration": False, "error": None}
