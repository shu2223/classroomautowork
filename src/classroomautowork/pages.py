"""Page images of course PDFs and slide decks, so the model reads pictures, charts and scans itself.

Extracted text misses whatever is inside an image. Decks go through headless LibreOffice to PDF;
PDF pages are rendered with pdfium. Course files are data: they never reach a shell.
"""

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .errors import WorkflowError

SLIDE_TYPES = {".pptx", ".ppt", ".odp"}
PDF_TYPES = {".pdf"}
LONG_EDGE = 1280  # About 1,200 visual tokens for a 16:9 slide.
MAX_PAGES_PER_DOCUMENT = 40
# Pictures smaller than this in a deck are logos and icons, not content.
MIN_PICTURE_BYTES = 30_000
# An image covering this share of a PDF page makes it a scan or a figure page.
MIN_IMAGE_AREA = 0.25
CONVERT_SECONDS = 300
RENDER_VERSION = 1


def renderable(name: str) -> bool:
    return Path(name).suffix.lower() in SLIDE_TYPES | PDF_TYPES


def slide_plan(data: bytes) -> list[dict]:
    """Slides in presentation order, which is the order LibreOffice exports them in.

    `number` is the slide's position when the files are sorted by their number, which is how
    the knowledge base labels slides (第N张). `visual` marks slides with a real picture or chart.
    """
    with zipfile.ZipFile(BytesIO(data)) as deck:
        names = set(deck.namelist())
        files = sorted(
            (n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)\.xml$", n)[1]),
        )
        number = {name: index for index, name in enumerate(files, 1)}
        order = files
        if "ppt/presentation.xml" in names and "ppt/_rels/presentation.xml.rels" in names:
            targets = {}
            rels = deck.read("ppt/_rels/presentation.xml.rels").decode("utf-8", "ignore")
            for tag in re.findall(r"<Relationship\b[^>]*>", rels):
                rid, target = re.search(r'Id="([^"]+)"', tag), re.search(r'Target="([^"]+)"', tag)
                if rid and target:
                    targets[rid[1]] = "ppt/" + target[1].lstrip("/").removeprefix("ppt/")
            presentation = deck.read("ppt/presentation.xml").decode("utf-8", "ignore")
            listed = [
                targets.get(rid)
                for rid in re.findall(r"<p:sldId\b[^>]*r:id=\"([^\"]+)\"", presentation)
            ]
            if listed and all(name in number for name in listed):
                order = listed
        plan = []
        for name in order:
            xml = deck.read(name).decode("utf-8", "ignore")
            rels_name = name.replace("slides/", "slides/_rels/") + ".rels"
            media = []
            if rels_name in names:
                rels = deck.read(rels_name).decode("utf-8", "ignore")
                for target in re.findall(r'Target="\.\./media/([^"]+)"', rels):
                    info = (
                        deck.getinfo("ppt/media/" + target)
                        if "ppt/media/" + target in names
                        else None
                    )
                    if info:
                        media.append(info.file_size)
            visual = ("<p:pic" in xml and any(size >= MIN_PICTURE_BYTES for size in media)) or (
                "drawingml/2006/chart" in xml
            )
            plan.append({"number": number[name], "visual": visual})
        return plan


def visual_pdf_pages(document) -> list[int]:
    """1-based pages where one image covers a large part of the page."""
    found = []
    for index in range(len(document)):
        page = document[index]
        try:
            width, height = page.get_size()
            area = width * height or 1
            for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=2):
                left, bottom, right, top = obj.get_bounds()
                if (right - left) * (top - bottom) / area >= MIN_IMAGE_AREA:
                    found.append(index + 1)
                    break
        finally:
            page.close()
    return found


def slides_to_pdf(data: bytes, suffix: str, workdir: Path) -> bytes:
    """Headless LibreOffice, with hidden slides kept so page N stays slide N."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise WorkflowError("小主机上没有 LibreOffice，无法把课件渲染成页面图片。")
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workdir) as temporary:
        folder = Path(temporary)
        source = folder / ("deck" + suffix)
        source.write_bytes(data)
        profile = workdir / "profile"
        options = '{"ExportHiddenSlides":{"type":"boolean","value":"true"},"ExportNotesPages":{"type":"boolean","value":"false"}}'
        try:
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile.resolve().as_uri()}",
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "pdf:impress_pdf_Export:" + options,
                    "--outdir",
                    str(folder),
                    str(source),
                ],
                env={**os.environ, "HOME": str(workdir)},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CONVERT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise WorkflowError("课件转换超时，未生成页面图片。") from None
        output = folder / "deck.pdf"
        if not output.is_file():
            raise WorkflowError("课件转换失败，未生成页面图片。")
        return output.read_bytes()


def render(document, pages: list[int], destination: Path) -> dict[int, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    rendered = {}
    for number in pages[:MAX_PAGES_PER_DOCUMENT]:
        if not 1 <= number <= len(document):
            continue
        page = document[number - 1]
        try:
            width, height = page.get_size()
            bitmap = page.render(scale=LONG_EDGE / max(width, height, 1))
            try:
                path = destination / f"page-{number:04d}.png"
                bitmap.to_pil().save(path)
                rendered[number] = path
            finally:
                bitmap.close()
        finally:
            page.close()
    return rendered


def page_images(
    data: bytes, name: str, destination: Path, workdir: Path, *, every_page: bool
) -> dict:
    """Render a document's pages; returns {knowledge-base page or slide number: image path}.

    Every page for a file attached to the assignment being answered; otherwise only pages
    whose content is mostly pictures, charts or scans.
    """
    suffix = Path(name).suffix.lower()
    plan = slide_plan(data) if suffix == ".pptx" else None
    pdf = slides_to_pdf(data, suffix, workdir) if suffix in SLIDE_TYPES else data
    try:
        document = pdfium.PdfDocument(pdf)
    except pdfium.PdfiumError:
        raise WorkflowError("无法打开这份文件来渲染页面图片。") from None
    try:
        count = len(document)
        if plan is not None and len(plan) != count:
            raise WorkflowError("课件页数和转换结果不一致，未生成页面图片。")
        if every_page:
            pages = list(range(1, count + 1))
        elif plan is not None:
            pages = [index for index, slide in enumerate(plan, 1) if slide["visual"]]
        elif suffix in PDF_TYPES:
            pages = visual_pdf_pages(document)
        else:
            pages = []  # Other decks: no reliable way to tell picture slides apart.
        rendered = render(document, pages, destination)
    finally:
        document.close()
    # PDF page N is presentation slide N; the knowledge base numbers slides by file name.
    label = {index: slide["number"] for index, slide in enumerate(plan, 1)} if plan else {}
    return {label.get(page, page): path for page, path in rendered.items()}
