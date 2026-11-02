"""Offline page-image fixtures; real LibreOffice conversion is checked against course files by hand."""

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from test_workflow import ReaderFixture, prepared_fixture

from classroomautowork import drafting, pages
from classroomautowork.errors import WorkflowError
from classroomautowork.store import Store
from classroomautowork.workflow import sync_course

PIC = "<p:sld><p:cSld><p:spTree><p:pic/></p:spTree></p:cSld></p:sld>"
TEXT = "<p:sld><p:cSld><p:spTree><a:t>{}</a:t></p:spTree></p:cSld></p:sld>"


def deck(order, big_picture_on=(), chart_on=(), small_picture_on=()):
    """A minimal slide zip: files slide1..N, presented in `order`."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        rels = "".join(f'<Relationship Id="rId{n}" Target="slides/slide{n}.xml"/>' for n in order)
        z.writestr("ppt/_rels/presentation.xml.rels", f"<Relationships>{rels}</Relationships>")
        ids = "".join(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in order)
        z.writestr(
            "ppt/presentation.xml",
            f"<p:presentation><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>",
        )
        for n in order:
            body = PIC if n in {*big_picture_on, *small_picture_on} else TEXT.format(f"slide {n}")
            if n in chart_on:
                body += (
                    '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"/>'
                )
            z.writestr(f"ppt/slides/slide{n}.xml", body)
            z.writestr(
                f"ppt/slides/_rels/slide{n}.xml.rels",
                f'<Relationships><Relationship Target="../media/image{n}.png"/></Relationships>',
            )
            z.writestr(
                f"ppt/media/image{n}.png",
                b"x" * (pages.MIN_PICTURE_BYTES + 1 if n in big_picture_on else 100),
            )
    return buffer.getvalue()


def test_slide_plan_follows_presentation_order_and_numbers_like_the_knowledge_base():
    plan = pages.slide_plan(deck([1, 2, 10, 3], big_picture_on={10}, chart_on={3}))
    # Exported page 3 is slide10.xml, which the knowledge base calls slide 4 (1, 2, 3, 10).
    assert [x["number"] for x in plan] == [1, 2, 4, 3]
    assert [x["visual"] for x in plan] == [False, False, True, True]


def test_small_pictures_such_as_logos_do_not_make_a_slide_visual():
    assert pages.slide_plan(deck([1], small_picture_on={1}))[0]["visual"] is False


def pdf_with_scan_then_blank():
    picture = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(picture, "PDF")
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(picture.getvalue())))
    writer.add_blank_page(width=600, height=800)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_pages_covered_by_an_image_are_visual():
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(pdf_with_scan_then_blank()) as document:
        assert pages.visual_pdf_pages(document) == [1]


def test_missing_libreoffice_is_a_reported_gap_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(pages.shutil, "which", lambda _: None)
    with pytest.raises(WorkflowError, match="LibreOffice"):
        pages.slides_to_pdf(b"deck", ".pptx", tmp_path)


class LectureReader(ReaderFixture):
    """A course whose unattached lecture PDF has a scanned page with no text."""

    def documents(self, course):
        return [
            *super().documents(course),
            {
                "id": "dddd0004",
                "kind": "document",
                "status": "ready",
                "title": "Scanned lecture",
                "source": "/kb/lecture.pdf",
                "source_bytes": 1,
                "source_mtime": 1,
            },
        ]

    def document_chunks(self, doc):
        if doc == "dddd0004":
            return [{"ord": 1, "locator": "第2页", "text": "only page 2 has text"}]
        return super().document_chunks(doc)

    def document_source(self, doc):
        return pdf_with_scan_then_blank() if doc == "dddd0004" else super().document_source(doc)


def test_unattached_lecture_gets_images_only_for_picture_pages(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, school_email="")
    with Store(tmp_path) as store:
        sync_course(LectureReader(), settings, store, "1")
        lecture = [c for c in store.chunks("1") if c["source_id"] == "kb:dddd0004"]
    by_locator = {c["locator"].split(" · ")[0]: c for c in lecture}
    # Page 1 is a scan with no text: it gets its own evidence entry carrying the image.
    assert Path(by_locator["第1页"]["image_path"]).is_file()
    assert "页面图片" in by_locator["第1页"]["text"]
    assert by_locator["第2页"]["image_path"] is None


def test_model_input_labels_each_image_and_caps_by_priority(tmp_path, monkeypatch):
    package, _, _, _ = prepared_fixture(tmp_path)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    evidence_path = package / "evidence.json"
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    processed = tmp_path / "processed" / "unit"
    processed.mkdir(parents=True)
    extra = []
    for source_id, count in (("kb:other", 3), ("file:unit-file-01", 2)):
        for n in range(1, count + 1):
            image = processed / f"{source_id.replace(':', '_')}-{n}.png"
            Image.new("RGB", (8, 8)).save(image)
            extra.append(
                {
                    "id": f"{source_id}-{n}",
                    "source_id": source_id,
                    "source_kind": "document",
                    "title": source_id,
                    "url": None,
                    "locator": f"第{n}页",
                    "text": "x",
                    "image_path": str(image),
                }
            )
    data["sources"] += extra
    evidence_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    manifest["source_index"].append({"source_id": "file:unit-file-01", "required": True})
    monkeypatch.setattr(drafting, "MAX_MODEL_IMAGES", 3)
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("s", encoding="utf-8")
    (skill / "references/review-format.md").write_text("f", encoding="utf-8")
    inputs, record = drafting.build_input(package, skill, manifest)
    # The assignment's own file comes first, in page order; the cap leaves out the rest and says so.
    assert [x["evidence_id"] for x in record["images"]] == [
        "file:unit-file-01-1",
        "file:unit-file-01-2",
        "kb:other-1",
    ]
    assert record["omitted_image_count"] == 2
    assert "因数量上限没有附上" in inputs[0]["text"]
    labels = [x["text"] for x in inputs[1:] if x["type"] == "text"]
    assert labels[0].startswith("页面图片 1：file:unit-file-01 · 第1页 [E:file:unit-file-01-1]")
    assert [x["type"] for x in inputs[1:]] == ["text", "image"] * 3
