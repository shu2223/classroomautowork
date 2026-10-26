"""Offline tests for passage images; fixtures do not contain actual classroom content."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from test_forms_student import URL, published_page
from test_workflow import ReaderFixture

from classroomautowork import browser_questions, drafting, form_fill, workflow
from classroomautowork.errors import PermissionDenied, WorkflowError
from classroomautowork.form_capture import (
    attach_form_images,
    authenticated_form_cache,
    import_form_capture,
    import_form_images,
)
from classroomautowork.form_read import form_chunks, parse_form
from classroomautowork.local import atomic_json
from classroomautowork.store import Store

ACCOUNT = "student@example.invalid"


def image_page(media_id="native-image-id-0001"):
    raw = published_page().split(" = ", 1)[1].removesuffix(";</script>")
    payload = json.loads(raw)
    payload[1][1].insert(
        2,
        [120, "Reading passage", None, 11, None, None, [media_id, 0, [564, 552, 0]]],
    )
    return "var FB_PUBLIC_LOAD_DATA_ = " + json.dumps(payload) + ";"


def imported(tmp_path):
    form = import_form_capture(tmp_path, ACCOUNT, URL, image_page(), [URL])
    path = tmp_path / "browser-download.png"
    Image.new("RGB", (12, 8), "white").save(path)
    return form, path


def test_native_image_id_page_and_dimensions_survive_without_guessing_a_url():
    form = parse_form(image_page(), URL)
    assert form["media"] == [
        {
            "item_id": "120",
            "page": 2,
            "title": "Reading passage",
            "description": "",
            "type": "image",
            "media_id": "native-image-id-0001",
            "width": 564,
            "height": 552,
        }
    ]
    assert not form["read_complete"]
    assert "第 2 页" in form["warnings"][0] and "item 120" in form["warnings"][0]
    assert form_fill.form_gap_reason("Reading question", form["warnings"][0]) == "missing_source"
    chunks = form_chunks(form)
    assert chunks[-1]["locator"] == "Google Forms page 2 image 120"
    assert "image_path" not in chunks[-1]
    assert json.loads(chunks[0]["text"])["media"][0]["media_id"] == "native-image-id-0001"


def test_imported_browser_pixels_become_integrity_checked_evidence(tmp_path):
    original, path = imported(tmp_path)
    captured = import_form_images(
        tmp_path, ACCOUNT, URL, [{"item_id": "120", "path": str(path)}], [URL]
    )
    assert captured["read_complete"] and not captured["warnings"]
    picture = captured["media"][0]
    retained = Path(picture["image_path"])
    assert retained.is_relative_to(tmp_path / "processed" / "form-images")
    assert retained.suffix == ".png" and retained.is_file()
    assert form_chunks(captured)[-1]["image_path"] == str(retained)
    # Question capture retains native context, without browser URLs or local paths.
    assert (
        authenticated_form_cache(tmp_path, ACCOUNT, URL, PermissionDenied("HTTP 401")) == original
    )
    assert "image_path" not in json.loads(form_chunks(captured)[0]["text"])["media"][0]
    assert attach_form_images(tmp_path, ACCOUNT, original) == captured
    assert "image_path" not in original["media"][0]


def test_passage_replacement_invalidates_answers_but_local_capture_location_does_not(tmp_path):
    original, path = imported(tmp_path)
    captured = import_form_images(
        tmp_path, ACCOUNT, URL, [{"item_id": "120", "path": str(path)}], [URL]
    )
    assert form_fill.context_digest(original) == form_fill.context_digest(captured)
    old_answer = {
        "form_url": URL,
        "entry_id": "202",
        "values": ["A"],
        "context_sha256": form_fill.context_digest(original),
    }
    replacement = parse_form(image_page("replaced-native-image"), URL)
    replacement["context_sha256"] = form_fill.context_digest(replacement)
    with pytest.raises(WorkflowError, match="原题已变化"):
        form_fill.validate_answers([replacement], [old_answer])
    plain = parse_form(published_page(), URL)
    plain_without_media = {key: value for key, value in plain.items() if key != "media"}
    assert form_fill.context_digest(plain) == form_fill.context_digest(plain_without_media)


def test_workflow_carries_verified_form_pixels_into_required_evidence_and_model_input(
    tmp_path, monkeypatch
):
    original, path = imported(tmp_path)
    captured = import_form_images(
        tmp_path, ACCOUNT, URL, [{"item_id": "120", "path": str(path)}], [URL]
    )
    image_path = captured["media"][0]["image_path"]

    class FormReader(ReaderFixture):
        def assignments(self, cid):
            assignment = super().assignments(cid)[0]
            assignment["materials"].append({"form": {"formUrl": URL}})
            return [assignment]

    settings = SimpleNamespace(data_dir=tmp_path, school_email=ACCOUNT)
    monkeypatch.setattr(workflow, "read_form", lambda _: original)
    with Store(tmp_path) as store:
        course = workflow.sync_course(FormReader(), settings, store, "1")
        package = Path(workflow.prepare_assignment(settings, store, course, "2")["package"])
        row = store.db.execute("SELECT result FROM tasks WHERE kind='form-questions'").fetchone()
        cached = json.loads(row[0])
        assert any(artifact["path"] == image_path for artifact in cached["artifacts"])
        assert store._valid(cached)
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        evidence = json.loads((package / "evidence.json").read_text(encoding="utf-8"))
        media_source = next(
            source for source in evidence["sources"] if source.get("image_path") == image_path
        )
        assert media_source["id"] in manifest["requirement_source_ids"]
        skill = tmp_path / "skill"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text("Offline test skill", encoding="utf-8")
        (skill / "references/review-format.md").write_text("Offline format", encoding="utf-8")
        inputs, metadata = drafting.build_input(package, skill, manifest)
        assert {"type": "localImage", "path": image_path} in inputs
        assert media_source["id"] in metadata["source_ids"]
        assert not any("图片材料尚未读取" in warning["error"] for warning in manifest["warnings"])
        Path(image_path).write_bytes(b"corrupted after processing")
        assert not store._valid(cached)


@pytest.mark.parametrize("changed", ["pixels", "media_id", "account"])
def test_changed_image_or_account_cannot_reuse_passage_pixels(tmp_path, changed):
    original, path = imported(tmp_path)
    captured = import_form_images(
        tmp_path, ACCOUNT, URL, [{"item_id": "120", "path": str(path)}], [URL]
    )
    account = ACCOUNT
    if changed == "pixels":
        Path(captured["media"][0]["image_path"]).write_bytes(b"corrupted")
    elif changed == "media_id":
        original = parse_form(image_page("replaced-native-image"), URL)
    else:
        account = "another@example.invalid"
    result = attach_form_images(tmp_path, account, original)
    assert not result["read_complete"] and result["warnings"]
    assert "image_path" not in result["media"][0]


def test_unknown_duplicate_or_nonimage_captures_are_rejected(tmp_path):
    _, path = imported(tmp_path)
    with pytest.raises(WorkflowError, match="未知或重复"):
        import_form_images(tmp_path, ACCOUNT, URL, [{"item_id": "999", "path": str(path)}], [URL])
    with pytest.raises(WorkflowError):
        import_form_images(
            tmp_path, ACCOUNT, URL, [{"item_id": "120", "path": str(path)}] * 2, [URL]
        )
    path.write_text("This is not image evidence", encoding="utf-8")
    with pytest.raises(WorkflowError, match="PNG/JPEG"):
        import_form_images(tmp_path, ACCOUNT, URL, [{"item_id": "120", "path": str(path)}], [URL])
    with pytest.raises(WorkflowError, match="原始表单"):
        import_form_images(tmp_path, ACCOUNT, URL, [], [])


def test_browser_recovery_handles_current_visual_gap_and_reports_unread_images(
    tmp_path, monkeypatch
):
    package = tmp_path / "package"
    package.mkdir()
    atomic_json(
        package / "manifest.json",
        {
            "assignment_id": "2",
            "assignment": {"title": "Unit task"},
            "source_index": [{"url": URL, "required": True}],
            "warnings": [{"url": URL, "error": "Google Forms 第 2 页图片材料尚未读取"}],
        },
    )
    requests = []

    class Client:
        approval_wait_seconds = 0

        def __init__(self, *args, **kwargs):
            self.events = iter(
                [
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "agentMessage",
                                "text": json.dumps(
                                    {
                                        "readable": True,
                                        "question_script": image_page(),
                                        "images": [],
                                        "message": "The next page requires existing answers before it can be opened.",
                                    }
                                ),
                            }
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "unit-turn", "status": "completed"}},
                    },
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, params):
            requests.append((method, params))
            if method == "thread/start":
                return {"model": params["model"], "thread": {"id": "unit-thread"}}
            return {"turn": {"id": "unit-turn"}}

        def next_event(self, **kwargs):
            return next(self.events)

    monkeypatch.setattr(browser_questions, "CodexClient", Client)
    monkeypatch.setattr(browser_questions, "codex_command", lambda: ["offline"])
    assert browser_questions.recover_questions(
        SimpleNamespace(data_dir=tmp_path, school_email=ACCOUNT), package, model="unit-model"
    )
    record = json.loads(next(package.glob("browser-read-*.json")).read_text(encoding="utf-8"))
    assert record["read_complete"] is False and record["image_count"] == 0
    assert record["missing_images"] == [{"item_id": "120", "page": 2, "title": "Reading passage"}]
    prompt = next(params for method, params in requests if method == "turn/start")["input"][0][
        "text"
    ]
    assert "type=11" in prompt and "不能猜答案" in prompt
