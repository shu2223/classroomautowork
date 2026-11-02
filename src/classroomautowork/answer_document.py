"""Readable local report files from verified answers, with no model or remote writes."""

import html
import json
import os
import re
import uuid
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .completion import candidate_mode
from .errors import WorkflowError
from .local import atomic_json, require_private_path, sha256_file, utc_now

VERSION = "answer-document-v1.7-plain-text"


def personal_templates_requested(manifest):
    """Only the trusted user's supplement can request personal template examples."""
    if candidate_mode(manifest):
        return True
    text = manifest.get("supplement", {}).get("text", "")
    for clause in re.split(r"[。.!！?？\n]", text):
        if re.search(r"(?:不要|不需要|别|禁止|しない)", clause):
            continue
        if re.search(r"(?:生成|作成).*?(?:模板|テンプレート|饮食习惯)", clause):
            return True
    return False


def template_presentation(draft, requested):
    """Present existing conditional candidates as templates; never promote them to facts."""
    if not requested:
        return draft, 0
    pattern = re.compile(
        r"^([①-⑳\d]+)「([^」]+)」：未入力。仮に、(.+?)なら([◎〇○△×])と考える。[ \t]*$",
        re.MULTILINE,
    )

    def render(match):
        number, title, reason, rating = match.groups()
        return f"{number}{title.replace('・', '、')}\n{rating}。{reason}。"

    return pattern.subn(render, draft)


_ESCAPED = re.compile(r"\\([\\`*_#\[\]()>+\-.!|])")
_STRONG = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_EMPHASIS = re.compile(r"(?<![A-Za-z0-9*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![A-Za-z0-9*])")
_HEADING = re.compile(r"(#{1,6})[ \t]*([^#\s].*?)(?:[ \t]+#+)?")
_LABEL = re.compile(r"(?:\*\*|__)([^*_\n]+?)(?:\*\*|__)([:：]?)")
_BULLET = re.compile(r"(?:[-*+•]\s+|・\s*)(.+)")
_NUMBERED = re.compile(r"(\d{1,3})[.)]\s+(.+)")
_IDENTITY = re.compile(r"学籍番号|学生番号|氏名|学科|クラス|学年")
_NO_INDENT = re.compile(r"[①-⑳【〔(（\[]|問|Q\d|[A-Za-z]\)|\d+[)）．.]")
_HTML_BOLD = re.compile(r"<(b|strong)>(.+?)</\1>", re.IGNORECASE)
_HTML_STYLE = re.compile(
    r"</?(?:i|em|u|s|del|ins|span|sup|sub|mark|small|font|p|div)(?:\s[^<>]*)?>", re.IGNORECASE
)


def inline_runs(text):
    """Markdown inline markup to plain runs; only bold survives as formatting."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_BOLD.sub(r"**\2**", _HTML_STYLE.sub("", text))
    text = re.sub(r"^\[\^[^\]]+\]:\s*", "", text)
    text = re.sub(r"\[\^[^\]]+\]", "", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)\s]*\)", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = _ESCAPED.sub(lambda m: "\0" + str(ord(m[1])) + "\0", text)
    pieces, position = [], 0
    for match in _STRONG.finditer(text):
        pieces.append((text[position : match.start()], False))
        pieces.append((match[1] or match[2], True))
        position = match.end()
    pieces.append((text[position:], False))
    runs = []
    for value, bold in pieces:
        value = _EMPHASIS.sub(r"\1", value)
        value = re.sub(r"\*\*|__|~~|`", "", value)
        value = re.sub(r"\0(\d+)\0", lambda m: chr(int(m[1])), value)
        if not value:
            continue
        if runs and runs[-1]["bold"] == bold:
            runs[-1]["text"] += value
        else:
            runs.append({"text": value, "bold": bold})
    return runs


def plain_text(text):
    return "".join(run["text"] for run in inline_runs(text))


def plain_answer_text(text):
    """Plain text for native document and form fields; no Markdown or HTML survives."""
    lines = []
    for raw in str(text).replace("\r\n", "\n").split("\n"):
        line = raw.strip(" \t")
        if re.fullmatch(r"[-*_=]{3,}", line):
            continue
        line = re.sub(r"^(?:#{1,6}[ \t]*(?=[^#\s])|>[ \t]?|[-*+•][ \t]+)", "", line)
        lines.append(plain_text(line).strip(" \t"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip(" \t\n")


def _text_block(kind, text, level=0):
    runs = inline_runs(text)
    if kind == "heading":
        runs = [{"text": "".join(run["text"] for run in runs), "bold": False}]
    return {
        "kind": kind,
        "level": level,
        "text": "".join(run["text"] for run in runs),
        "runs": runs,
    }


def _flush(blocks, paragraph):
    if paragraph:
        blocks.append(_text_block("paragraph", "\n".join(paragraph)))
        paragraph.clear()


def answer_blocks(draft, *, personal_templates=False):
    """Remove internal audit notes and turn the model's Markdown into report blocks."""
    lines = []
    for line in draft.splitlines():
        if re.match(r"^#{1,6}\s+本[机機].*(证据|證據|审核|審核|監査).*不[进入進入]", line):
            break
        lines.append(re.sub(r"\[E:[a-f0-9]{24}\]", "", line).rstrip())
    text = "\n".join(lines)
    text, _ = template_presentation(text, personal_templates)
    if personal_templates and ("未入力" in text or re.search(r"仮に[、，]", text)):
        raise WorkflowError("个人模板仍含占位或假设提示，需要按你的正文格式重新整理。")
    blocks, heading_level = [], 1
    for chunk in re.split(r"\n\s*\n", text):
        chunk = chunk.strip()
        if (
            not chunk
            or re.fullmatch(r"[-*_]{3,}", chunk)
            or chunk.startswith(
                ("【本机审核说明】", "【本機審核說明】", "本机分析依据：", "本機分析依據：")
            )
        ):
            continue
        if chunk.startswith("|"):
            rows = [
                [plain_text(cell.strip()) for cell in row.strip().strip("|").split("|")]
                for row in chunk.splitlines()
                if not re.fullmatch(r"[| :\-]+", row.strip())
            ]
            if rows and all(len(row) == len(rows[0]) for row in rows):
                blocks.append({"kind": "table", "rows": rows})
                continue
        paragraph, current_list = [], None
        for raw in chunk.splitlines():
            line = raw.strip()
            heading = _HEADING.fullmatch(line)
            label = _LABEL.fullmatch(line)
            bullet = _BULLET.fullmatch(line)
            numbered = _NUMBERED.fullmatch(line)
            setext = re.fullmatch(r"=+|-{2,}", line)
            if setext and len(paragraph) == 1:
                level = 2 if line[0] == "=" else 3
                heading_level = level
                blocks.append(_text_block("heading", paragraph.pop(), level))
                current_list = None
            elif not line or re.fullmatch(r"[-*_=]{3,}", line):
                _flush(blocks, paragraph)
                current_list = None
            elif heading or label:
                _flush(blocks, paragraph)
                current_list = None
                if heading:
                    heading_level = len(heading[1])
                    blocks.append(_text_block("heading", heading[2], heading_level))
                else:
                    level = min(heading_level + 1, 6)
                    blocks.append(_text_block("heading", label[1] + label[2], level))
            elif bullet or numbered:
                _flush(blocks, paragraph)
                ordered = bool(numbered)
                if current_list is None or current_list["ordered"] != ordered:
                    current_list = {"kind": "list", "ordered": ordered, "items": []}
                    blocks.append(current_list)
                item = _text_block("item", numbered[2] if numbered else bullet[1])
                item["number"] = int(numbered[1]) if numbered else None
                current_list["items"].append(item)
            elif current_list is not None and raw[:1].isspace():
                item = current_list["items"][-1]
                extra = inline_runs(line)
                item["runs"] += [{"text": "\n", "bold": False}, *extra]
                item["text"] += "\n" + "".join(run["text"] for run in extra)
            else:
                current_list = None
                paragraph.append(line[1:].lstrip() if line.startswith(">") else line)
        _flush(blocks, paragraph)
    for block in blocks:
        if block["kind"] == "list":
            block["text"] = "\n".join(
                (f"{item['number']}. " if item["number"] is not None else "") + item["text"]
                for item in block["items"]
            )
    if not blocks:
        raise WorkflowError("没有可导出的实际答案。")
    return blocks


def _inputs(package):
    receipt = json.loads((package / "review-receipt.json").read_text(encoding="utf-8"))
    generation = json.loads((package / "generation.json").read_text(encoding="utf-8"))
    if (
        generation.get("status") != "completed"
        or not receipt.get("draft_sha256")
        or sha256_file(package / "draft.md") != receipt["draft_sha256"]
        or sha256_file(package / "review.json") != receipt.get("review_sha256")
    ):
        raise WorkflowError("答案尚未完成核验或内容已变化，不能生成审阅文件。")
    return {
        name: sha256_file(package / name)
        for name in ("draft.md", "review.json", "generation.json", "manifest.json")
    }


def valid_answer_document(package):
    package = require_private_path(Path(package))
    try:
        result = json.loads((package / "answer-document.json").read_text(encoding="utf-8"))
        if result.get("version") != VERSION or result["inputs"] != _inputs(package):
            return None
        for name in ("answer.docx", "answer.html"):
            path = (package / name).resolve()
            if path.parent != package or sha256_file(path) != result["files"][name]:
                return None
        return result
    except (OSError, ValueError, KeyError, TypeError, WorkflowError):
        return None


BODY_FONT = ("Yu Mincho", "游明朝")
HEADING_FONT = ("Yu Gothic", "游ゴシック")


def _set_fonts(style, fonts):
    """Explicit Japanese fonts; theme attributes would override them in Word."""
    _explicit_fonts(style.element.get_or_add_rPr().get_or_add_rFonts(), fonts)


def _explicit_fonts(rfonts, fonts):
    for key in list(rfonts.attrib):
        if key.lower().endswith("theme"):
            del rfonts.attrib[key]
    for key in ("ascii", "hAnsi", "eastAsia"):
        rfonts.set(qn("w:" + key), fonts[1] if key == "eastAsia" else fonts[0])


def _report_styles(document):
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.0)
    section.left_margin = section.right_margin = Cm(2.0)
    section.footer_distance = Cm(1.0)
    defaults = document.styles.element.find(qn("w:docDefaults"))
    for rfonts in defaults.iter(qn("w:rFonts")) if defaults is not None else ():
        _explicit_fonts(rfonts, BODY_FONT)
    sizes = {"Normal": 11, "Title": 14, "Heading 1": 12, "Heading 2": 11, "Heading 3": 11}
    before = {"Title": 0, "Heading 1": 10, "Heading 2": 6, "Heading 3": 3}
    for name, size in sizes.items():
        style = document.styles[name]
        _set_fonts(style, BODY_FONT if name == "Normal" else HEADING_FONT)
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        rpr = style.element.get_or_add_rPr()
        for lang in list(rpr.findall(qn("w:lang"))):
            rpr.remove(lang)
        lang = OxmlElement("w:lang")
        lang.set(qn("w:val"), "en-US")
        lang.set(qn("w:eastAsia"), "ja-JP")
        rpr.append(lang)
        props = style.element.get_or_add_pPr()
        for border in list(props.findall(qn("w:pBdr"))):
            props.remove(border)
        snap = OxmlElement("w:snapToGrid")
        snap.set(qn("w:val"), "0")
        props.append(snap)
        if name != "Normal":
            style.font.bold = True
            style.paragraph_format.line_spacing = 1.0
            style.paragraph_format.space_before = Pt(before[name])
            style.paragraph_format.space_after = Pt(2)
    normal = document.styles["Normal"].paragraph_format
    normal.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    normal.line_spacing = Pt(16)
    normal.space_after = Pt(0)
    document.styles["Title"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.styles["Title"].paragraph_format.space_after = Pt(4)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for kind, value in (("begin", None), (None, "PAGE"), ("end", None)):
        run = footer.add_run()
        if kind:
            element = OxmlElement("w:fldChar")
            element.set(qn("w:fldCharType"), kind)
        else:
            element = OxmlElement("w:instrText")
            element.set(qn("xml:space"), "preserve")
            element.text = value
        run._r.append(element)
        run.font.size = Pt(9)


def _add_runs(paragraph, runs):
    for run in runs:
        added = paragraph.add_run(run["text"])
        if run["bold"]:
            added.bold = True


def _html_runs(runs):
    return "".join(
        f"<strong>{html.escape(run['text'])}</strong>" if run["bold"] else html.escape(run["text"])
        for run in runs
    )


def _indent_body(block):
    return "\n" not in block["text"] and not _NO_INDENT.match(block["text"])


def _table(document, rows):
    table = document.add_table(rows=0, cols=len(rows[0]))
    table.style = "Table Grid"
    table.autofit = False
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement("w:" + edge)
        for key, value in (("val", "single"), ("sz", "4"), ("color", "BFBFBF")):
            border.set(qn("w:" + key), value)
        borders.append(border)
    table._tbl.tblPr.append(borders)
    width = Cm(17.0 / len(rows[0]))
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values, strict=True):
            cell.width = width
            cell.text = value
            for p in cell.paragraphs:
                p.paragraph_format.line_spacing = 1.0
                p.paragraph_format.space_after = Pt(2)
                for run in p.runs:
                    run.font.size = Pt(9.5)
        table.rows[-1]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    for cell in table.rows[0].cells:
        shading = OxmlElement("w:shd")
        shading.set(qn("w:val"), "clear")
        shading.set(qn("w:fill"), "EEEEEE")
        cell._tc.get_or_add_tcPr().append(shading)
    document.add_paragraph()
    return (
        "<table>"
        + "".join(
            "<tr>" + "".join("<td>" + html.escape(value) + "</td>" for value in row) + "</tr>"
            for row in rows
        )
        + "</table>"
    )


def render_report(blocks, title):
    """Word and HTML from the same blocks, laid out as a Japanese A4 report."""
    document = Document()
    _report_styles(document)
    first_is_title = blocks[0]["kind"] == "heading" and blocks[0]["level"] == 1
    displayed_title = blocks[0]["text"] if first_is_title else title
    document.add_paragraph(displayed_title, "Title")
    # Completion and personal-confirmation notices belong in the app, outside the answer.
    rendered = [f"<h1>{html.escape(displayed_title)}</h1>"]
    body = blocks[1:] if first_is_title else blocks
    for index, block in enumerate(body):
        if block["kind"] == "table":
            rendered.append(_table(document, block["rows"]))
        elif block["kind"] == "list":
            tag = "ol" if block["ordered"] else "ul"
            items = []
            for item in block["items"]:
                if item["number"] is None:
                    paragraph = document.add_paragraph(style="List Bullet")
                else:
                    paragraph = document.add_paragraph()
                    paragraph.paragraph_format.left_indent = Pt(22)
                    paragraph.paragraph_format.first_line_indent = Pt(-16.5)
                    paragraph.add_run(f"{item['number']}. ")
                _add_runs(paragraph, item["runs"])
                value = f" value='{item['number']}'" if item["number"] is not None else ""
                items.append(f"<li{value}>{_html_runs(item['runs'])}</li>")
            rendered.append(f"<{tag}>{''.join(items)}</{tag}>")
        elif block["kind"] == "heading":
            level = block["level"]
            style = "Title" if level == 1 else f"Heading {min(level - 1, 3)}"
            document.add_paragraph(block["text"], style)
            rendered.append(f"<h{min(level, 4)}>{html.escape(block['text'])}</h{min(level, 4)}>")
        else:
            paragraph = document.add_paragraph()
            _add_runs(paragraph, block.get("runs") or inline_runs(block["text"]))
            if index == 0 and len(block["text"]) < 120 and _IDENTITY.search(block["text"]):
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                paragraph.paragraph_format.space_after = Pt(6)
                css = "identity"
            elif _indent_body(block):
                paragraph.paragraph_format.first_line_indent = Pt(11)
                css = "body"
            else:
                paragraph.paragraph_format.space_after = Pt(4)
                css = "plain"
            rendered.append(f"<p class='{css}'>{_html_runs(block.get('runs') or [])}</p>")
    return document, rendered


def create_answer_document(package):
    """Idempotent export; preserve source/model provenance and mark unfinished work."""
    package = require_private_path(Path(package))
    cached = valid_answer_document(package)
    if cached:
        return cached
    inputs = _inputs(package)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    review = json.loads((package / "review.json").read_text(encoding="utf-8"))
    pending = [x["requirement"] for x in review["requirement_checks"] if x["status"] != "met"]
    draft = (package / "draft.md").read_text(encoding="utf-8")
    personal_templates = personal_templates_requested(manifest)
    plain = re.sub(r"\[E:[a-f0-9]{24}\]", "", draft)
    _, template_count = template_presentation(plain, personal_templates)
    blocks = answer_blocks(draft, personal_templates=personal_templates)
    title = manifest["assignment"].get("title", "作业答案")
    document, rendered = render_report(blocks, title)
    destination = package / "answer.docx"
    if (
        destination.resolve().parent != package
        or (package / "answer.html").resolve().parent != package
    ):
        raise WorkflowError("答案文件不能链接到其他私人文件。")
    temporary = package / ("answer-" + uuid.uuid4().hex + ".docx")
    try:
        document.save(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    (package / "answer.html").write_text(
        "<!doctype html><html lang='ja'><meta charset='utf-8'><title>"
        + html.escape(title)
        + "</title><style>body{max-width:760px;margin:32px auto;padding:24px;"
        "font:16px/1.9 'Yu Mincho','YuMincho','Hiragino Mincho ProN','Noto Serif JP',serif}"
        "h1,h2,h3,h4{font-family:'Yu Gothic','YuGothic','Hiragino Sans','Noto Sans JP',sans-serif}"
        "h1{font-size:22px;text-align:center}h2{font-size:18px}h3,h4{font-size:16px}"
        "p{margin:0 0 4px;white-space:pre-line}p.body{text-indent:1em}p.identity{text-align:right}"
        "table{width:100%;border-collapse:collapse}td{border:1px solid #ccc;padding:8px}</style>"
        + "".join(rendered)
        + "</html>",
        encoding="utf-8",
    )
    result = {
        "version": VERSION,
        "created_at": utc_now(),
        "inputs": inputs,
        "title": title,
        "status": "ready",
        "pending_requirements": pending,
        "candidate_mode": candidate_mode(manifest),
        "review_cautions": list(dict.fromkeys(pending + review.get("questions", []))),
        "personal_templates": personal_templates,
        "template_transform_count": template_count,
        "presentation_note": (
            "个人内容为你要求的通用模板，请按本人情况核对；模板没有加入真实个人事实。"
            if personal_templates
            else ""
        ),
        "blocks": blocks,
        "files": {name: sha256_file(package / name) for name in ("answer.docx", "answer.html")},
        "submission": "manual_only",
    }
    atomic_json(package / "answer-document.json", result)
    return result
