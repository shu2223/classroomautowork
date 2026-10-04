"""Document extraction with page/paragraph locators and retained PDF page images.

Documents are data: no macros, embedded programs, external links or XML entities are executed.
Scanned pages are retained and flagged for Codex's visual inspection; text is never invented.
"""

import importlib.metadata
import re
import zipfile
from pathlib import Path

import pypdfium2 as pdfium
from defusedxml import ElementTree
from pypdf import PdfReader

from .errors import WorkflowError
from .local import atomic_json
from .progress import report
from .store import artifact

EXTRACTOR_VERSION = "documents-v1.2"


def processor_identity(policy) -> dict:
    return {
        "version": EXTRACTOR_VERSION,
        "pypdf": importlib.metadata.version("pypdf"),
        "pdfium": importlib.metadata.version("pypdfium2"),
        "dpi": policy.render_dpi,
        "max_pages": policy.max_pdf_pages,
    }


def _ooxml(path: Path, kind: str) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(x.file_size for x in entries) > 100_000_000:
            raise WorkflowError("Document archive exceeds the extraction budget.")
        if any(x.file_size / max(x.compress_size, 1) > 1000 for x in entries):
            raise WorkflowError("Suspicious document compression ratio; extraction stopped.")
        if kind == ".docx":
            names = ["word/document.xml"]
        else:
            names = sorted(
                (
                    x.filename
                    for x in entries
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", x.filename)
                ),
                key=lambda x: int(re.search(r"slide(\d+)", x)[1]),
            )
        chunks = []
        for name in names:
            root = ElementTree.fromstring(archive.read(name))
            if kind == ".docx":
                paragraphs = [x for x in root.iter() if x.tag.endswith("}p")]
                for number, paragraph in enumerate(paragraphs, 1):
                    text = "".join(x.text or "" for x in paragraph.iter() if x.tag.endswith("}t"))
                    if text.strip():
                        chunks.append({"locator": f"paragraph {number}", "text": text})
            else:
                text = "\n".join(x.text or "" for x in root.iter() if x.tag.endswith("}t"))
                chunks.append(
                    {"locator": "slide " + re.search(r"slide(\d+)", name)[1], "text": text}
                )
        return chunks


def extract_document(path: Path, destination: Path, policy, *, progress=None) -> dict:
    report(progress, "正在打开文档并提取内容", "extract")
    destination.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    images, warnings = [], []
    if suffix == ".pdf":
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise WorkflowError("Encrypted PDF needs a user-provided readable copy.")
        if len(reader.pages) > policy.max_pdf_pages:
            raise WorkflowError(
                "PDF exceeds this course's page budget; raise the private setting to retry."
            )
        chunks = []
        with pdfium.PdfDocument(str(path)) as document:
            for number, page in enumerate(reader.pages, 1):
                report(
                    progress,
                    f"正在提取第 {number}/{len(reader.pages)} 页文字和页图",
                    "extract",
                    current=number - 1,
                    total=len(reader.pages),
                    unit="pages",
                )
                text = (
                    (page.extract_text(extraction_mode="layout") or "")
                    if "/Contents" in page
                    else ""
                )
                image = destination / f"page-{number:04d}.png"
                render_page = document[number - 1]
                try:
                    width, height = render_page.get_size()
                    scale = min(policy.render_dpi / 72, 2400 / max(width, height))
                    bitmap = render_page.render(scale=scale)
                    try:
                        bitmap.to_pil().save(image)
                    finally:
                        bitmap.close()
                finally:
                    render_page.close()
                images.append(image)
                chunks.append({"locator": f"page {number}", "text": text, "image_path": str(image)})
                if len(text.strip()) < 30:
                    warnings.append(
                        f"page {number}: little extractable text; inspect the retained image."
                    )
                report(
                    progress,
                    f"已保留 {number}/{len(reader.pages)} 页文字和页图",
                    "extract",
                    current=number,
                    total=len(reader.pages),
                    unit="pages",
                )
    elif suffix in {".docx", ".pptx"}:
        chunks = _ooxml(path, suffix)
        warnings.append("OOXML text extracted; diagrams/layout remain in the original attachment.")
    elif suffix in {".txt", ".md", ".csv", ".srt", ".vtt"}:
        if path.stat().st_size > 20_000_000:
            raise WorkflowError("Text attachment exceeds extraction budget.")
        text = path.read_text(encoding="utf-8-sig")
        chunks = [
            {
                "locator": f"lines {start + 1}-{min(start + 40, len(text.splitlines()))}",
                "text": "\n".join(text.splitlines()[start : start + 40]),
            }
            for start in range(0, len(text.splitlines()), 40)
        ]
    else:
        raise WorkflowError("Unsupported document format; original retained for manual review.")
    result = {"chunks": chunks, "warnings": warnings, "processor": EXTRACTOR_VERSION}
    output = destination / "extraction.json"
    atomic_json(output, result)
    return {**result, "artifacts": [artifact(output), *(artifact(p) for p in images)]}
