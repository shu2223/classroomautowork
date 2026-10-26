"""Deterministically remap reviewed, explicitly labeled draft answers.

The caller must authenticate the draft against its existing review receipt first.
This module does not generate answers or change generation provenance. It only
accepts a unique numbered section and literal label shared by the draft and form.
"""

import re
import unicodedata
from collections import Counter, defaultdict

SECTION = re.compile(
    r"^(?:#{1,6}\s+)?(?:[■□●]\s*)?(設問|課題|問)\s*"
    r"([①-⑳]|[0-9０-９]+|[一二三四五六七八九十]+)"
)
LABEL_PUNCTUATION = str.maketrans({"（": "(", "）": ")", "：": ":"})
PERSONAL_LABELS = {
    "学籍番号",
    "学生番号",
    "学号",
    "學號",
    "氏名",
    "姓名",
    "名前",
    "studentid",
    "name",
}
RECOVERABLE_KINDS = {"labeled_answer", "number_selection"}
EVIDENCE = re.compile(r"\[E:[^\]\r\n]+\]")


def _section_key(text):
    if not isinstance(text, str):
        return None
    match = SECTION.match(text.strip())
    if not match:
        return None
    # The number identifies the section; its human-readable title need not occur
    # in the draft. Keep 設問/課題/問 distinct and do not infer equivalent wording.
    return match[1], unicodedata.normalize("NFKC", match[2])


def _label_key(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", "", text.translate(LABEL_PUNCTUATION)).rstrip(":")


def _personal_label(text):
    return _label_key(text).casefold() in PERSONAL_LABELS


def _draft_answers(draft):
    answers = defaultdict(list)
    sections = Counter()
    section = None
    fence = None
    for raw_line in draft.splitlines():
        line = raw_line.strip()
        fence_match = re.match(r"^(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        key = _section_key(line)
        if key:
            section = key
            sections[key] += 1
            continue
        # An unrelated Markdown heading ends the explicit numbered section.
        if re.match(r"^#{1,6}\s", line):
            section = None
        if section is None:
            continue
        match = re.fullmatch(r"([^:：\r\n]+)[:：]([^\r\n]*)", line)
        if not match or _personal_label(match[1]):
            continue
        label = _label_key(match[1])
        text = EVIDENCE.sub("", match[2]).strip()
        # A recovered answer is exactly one explicit label/value line. Do not
        # guess paragraphs, Markdown tables, bullets, or narrative continuations.
        if label and text and "[E:" not in text:
            answers[(section, label)].append(text)
        elif label:
            answers[(section, label)].append(None)
    return answers, sections


def recover_labeled_answers(form, draft, answers):
    """Return complete mappings, or ``None`` when exact recovery is ambiguous.

    Existing mappings survive unchanged only when their field ID and context hash
    still match. New/stale mappings must be explicit native labeled slots and
    match exactly one reviewed draft line in a unique numbered section. Identity
    continues through the trusted student-profile flow, never through this path.
    """
    from .document_fill import required_answer_fields

    if (
        not isinstance(form, dict)
        or not isinstance(draft, str)
        or not isinstance(answers, list)
        or not all(isinstance(answer, dict) for answer in answers)
        or form.get("unrecognized_questions")
    ):
        return None
    fields = form.get("fields", [])
    if not isinstance(fields, list) or not all(isinstance(field, dict) for field in fields):
        return None
    if any(not field.get("id") or not field.get("context_sha256") for field in fields):
        return None
    lookup = {field["id"]: field for field in fields}
    if len(lookup) != len(fields):
        return None

    recovered = {}
    seen = set()
    for answer in answers:
        field_id = answer.get("field_id")
        if not isinstance(field_id, str) or field_id in seen:
            return None
        seen.add(field_id)
        field = lookup.get(field_id)
        if field is None or answer.get("context_sha256") != field["context_sha256"]:
            continue
        text = answer.get("text")
        if (
            field.get("identity_key")
            or field.get("kind") == "identity"
            or _personal_label(field.get("label"))
            or answer.get("document_id", form.get("document_id")) != form.get("document_id")
            or not isinstance(text, str)
            or not text.strip()
            or any(char in text for char in ("\r", "\n"))
            or "[E:" in text
        ):
            return None
        recovered[field_id] = dict(answer)

    required = required_answer_fields(form, answers=list(recovered.values()))
    missing = [field for field in required if field["id"] not in recovered]
    if not missing:
        return list(recovered.values()) if recovered else None

    draft_answers, draft_sections = _draft_answers(draft)
    form_keys = Counter()
    form_sections = defaultdict(set)
    for field in fields:
        section = _section_key(field.get("section"))
        if section:
            form_keys[(section, _label_key(field.get("label")))] += 1
            form_sections[section].add(
                (field.get("tab_id"), field.get("section_id", field.get("section")))
            )
    for field in missing:
        section = _section_key(field.get("section"))
        key = section, _label_key(field.get("label"))
        candidates = draft_answers.get(key, [])
        if (
            field.get("kind") not in RECOVERABLE_KINDS
            or _personal_label(field.get("label"))
            or not section
            or draft_sections[section] != 1
            or len(form_sections[section]) != 1
            or form_keys[key] != 1
            or len(candidates) != 1
            or not candidates[0]
        ):
            return None
        recovered[field["id"]] = {
            "document_id": form.get("document_id"),
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": candidates[0],
        }
    return [recovered[field["id"]] for field in fields if field["id"] in recovered]
