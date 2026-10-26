"""Bounded inline worksheet slots. Only their contents may change, never labels."""

import re


def paragraph_options(text):
    """Read an explicit numbered option line without making its labels editable."""
    text = text.strip()
    if re.search(r"[?？]", text):
        return []
    markers = list(re.finditer(r"[①-⑳]|(?:^|[ \t　]+)[（(]?[1-9][)）.．]", text))
    if len(markers) < 2:
        return []
    options = []
    for i, marker in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        label = text[marker.start() : end].strip()
        if not text[marker.end() : end].strip():
            return []
        options.append(label)
    if text[: markers[0].start()].strip():
        return []
    return options


def identity_slots(text):
    """Recognize explicit personal labels, never infer identity from course text."""
    match = re.fullmatch(
        r"(?P<label>[ \t　]*(?P<name>学籍\s*番号|氏\s*名|姓名|名前|student\s*id|name)"
        r"[ \t　]*[:：])(?P<answer>[^\n]*)\n",
        text,
        re.I,
    )
    if not match:
        return []
    name = re.sub(r"\s+", "", match["name"]).lower()
    return [
        {
            "kind": "identity",
            "identity_key": "student_id" if name in {"学籍番号", "studentid"} else "name",
            "label": match["label"].strip(),
            "span": match.span("answer"),
            "current": match["answer"].strip(),
            "insert_prefix": " ",
        }
    ]


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def inline_slots(text):
    """Return character spans; native Google indices are UTF-16, not Python offsets."""
    choice = re.fullmatch(
        r"(?P<mark>[□☑])(?P<label>.+?)(?P<gap>\s+)[(（](?P<answer>[^\n]*)[)）][ \t　]*\n",
        text,
    )
    if choice:
        return [
            {
                "kind": "choice",
                "label": choice["label"].strip() + "（選択する場合のみ☑）",
                "span": choice.span("mark"),
                "current": "" if choice["mark"] == "□" else choice["mark"],
                "choices": ["☑"],
            },
            {
                "kind": "reason",
                "label": choice["label"].strip() + "（理由）",
                "span": choice.span("answer"),
                "current": choice["answer"].strip(),
            },
        ]
    keyword = re.fullmatch(r"(?P<label>\s*キーワード[0-9０-９]+)(?P<answer>[^\n]*)\n", text)
    if keyword:
        return [
            {
                "kind": "keyword",
                "label": keyword["label"].strip(),
                "span": keyword.span("answer"),
                "current": keyword["answer"].strip(),
                "insert_prefix": "\t",
            }
        ]
    return []


def explicit_answer_slots(text):
    """Separate an explicit answer label from its contents within one paragraph.

    A reason label or a request for selected numbers is part of the worksheet, not
    an existing answer. Keep its colon, indentation and formatting protected.
    """
    match = re.fullmatch(
        r"(?P<label>[ \t　]*(?P<name>"
        r"(?:選択した|選んだ)番号(?:[ \t　]*[（(][^()（）\n:：]{1,30}[)）])?"
        r"|[^\n:：?？]{0,60}理由|回答|答え|解答)"
        r"[ \t　]*[:：])(?P<answer>[^\n]*)\n",
        text,
    )
    if not match:
        return []
    return [
        {
            "kind": "number_selection" if "番号" in match["name"] else "labeled_answer",
            "label": match["label"].strip(),
            "span": match.span("answer"),
            "current": match["answer"].strip(),
            "insert_prefix": " ",
        }
    ]


def slot_template(text, slots):
    for slot in sorted(slots, key=lambda x: x["span"][0], reverse=True):
        start, end = slot["span"]
        text = text[:start] + "<" + slot["kind"] + ">" + text[end:]
    return text


def protected_inline_runs(elements, spans):
    """Compare all text/style outside answer spans even when Google splits text runs."""
    runs, offset = [], 0
    for element in elements:
        run = element["textRun"]
        content = run["content"]
        kept = "".join(
            char
            for i, char in enumerate(content, offset)
            if not any(start <= i < end for start, end in spans)
        )
        offset += len(content)
        metadata = {
            **{k: v for k, v in element.items() if k not in {"startIndex", "endIndex", "textRun"}},
            "textRun": {k: v for k, v in run.items() if k != "content"},
        }
        if kept:
            if runs and runs[-1]["metadata"] == metadata:
                runs[-1]["text"] += kept
            else:
                runs.append({"text": kept, "metadata": metadata})
    return runs
