"""Login-only forms read with the school-login browser (a stand-in here), and the certain-only
pairing of rendered pictures with native image items."""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from test_workflow import ReaderFixture

from classroomautowork import school_browser, school_forms, workflow
from classroomautowork.errors import PermissionDenied, WorkflowError
from classroomautowork.form_capture import _path, import_form_capture
from classroomautowork.store import Store

URL = "https://docs.google.com/forms/d/e/unit-login-only-form/viewform"
ACCOUNT = "student@school.example"
LOGIN_WALL = PermissionDenied("表单要求浏览器登录或重定向到非题目页面；未读取题目，也未扩大授权。")


def login_page(*, second_picture=True):
    items = [
        [100, "Student ID", None, 0, [[200, None, 1]]],
        [110, "Look at the photo", None, 11, None, None, ["unit-media-page-one", 0, [740, 416, 0]]],
        [101, "Describe it", None, 1, [[201, None, 1]]],
        [102, "Part 2", None, 8, None],
        [103, "Choose", None, 2, [[203, [["A"], ["B"], ["", None, None, None, 1]], 1]]],
    ]
    if second_picture:
        items.append(
            [111, "Reference", None, 11, None, None, ["unit-media-page-two", 0, [600, 400, 0]]]
        )
    body = [None] * 26
    body[0], body[1], body[8] = "Worksheet", items, "Unit login form"
    return "<script>var FB_PUBLIC_LOAD_DATA_ = " + json.dumps([None, body]) + ";</script>"


def picture_file(directory: Path, name="a.img", size=(740, 416)):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    Image.new("RGB", size, "teal").save(path, format="PNG")
    return str(path)


def rendered(path=None, ids=("110",), size=(740, 416), google=True, in_list=True):
    return {
        "index": 0,
        "natural_width": size[0],
        "natural_height": size[1],
        "width": size[0],
        "height": size[1],
        "attr_width": size[0],
        "attr_height": size[1],
        "item_ids": list(ids),
        "google": google,
        "in_list": in_list,
        **({"path": path} if path else {}),
    }


class FakeBrowser:
    """read_page as the school browser's; walk_pages turns no page unless `walk` says what the
    later pages rendered ({page: build(capture_dir)}) or `walk_error` is raised."""

    def __init__(self, html=None, images=None, error=None, walk=None, walk_error=None):
        self.html = html or login_page()
        self.images = images
        self.error = error
        self.walk = walk
        self.walk_error = walk_error
        self.draft = "cleared"
        self.calls = []
        self.walks = []

    def read_page(self, url, capture_dir):
        self.calls.append((url, capture_dir))
        if self.error:
            raise self.error
        images = self.images(capture_dir) if callable(self.images) else (self.images or [])
        return {"html": self.html, "images": images, "blocked": 0}

    def walk_pages(self, url, link, item_pages, target_page, capture_dir):
        self.walks.append(
            {"url": url, "link": link, "item_pages": item_pages, "target": target_page}
        )
        if self.walk_error:
            raise self.walk_error
        if self.walk is None:
            return {"pages": {}, "reached": 1, "stopped": "第 1 页没有唯一的「次へ」按钮"}
        pages = {page: build(capture_dir) for page, build in self.walk.items()}
        return {
            "pages": pages,
            "reached": max(pages, default=1),
            "stopped": "",
            "draft": self.draft,
        }


def signed_in(data_dir, state="signed_in", email=ACCOUNT):
    school_browser._write_record(
        data_dir, {"email": email, "verified_at": "2026-11-06T00:00:00+00:00", "state": state}
    )


def settings(tmp_path, school_email=""):
    return SimpleNamespace(data_dir=tmp_path, school_email=school_email, timezone="Asia/Tokyo")


def test_signed_in_reads_the_form_and_pairs_the_marked_first_page_picture(tmp_path):
    signed_in(tmp_path)
    browser = FakeBrowser(images=lambda d: [rendered(picture_file(d))])
    events = []
    form = school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, events.append, browser=browser
    )
    assert browser.calls[0][0] == URL
    record = json.loads(_path(tmp_path, URL).read_text(encoding="utf-8"))
    assert record["source"] == "school_login_browser" and record["account"] == ACCOUNT
    media = {m["item_id"]: m for m in form["media"]}
    assert Path(media["110"]["image_path"]).is_file() and not media["111"].get("image_path")
    assert any("第 2 页图片材料尚未读取" in w for w in form["warnings"])
    assert not any("第 1 页图片" in w for w in form["warnings"])
    assert events[0] == "正在用学校账号读取需要登录的表单（只读）"
    assert events[-1].startswith("已用学校账号读取表单：2 页、3 个题目、1 张图片")
    assert events[0].details["stage"] == "form_read"
    # The temporary capture directory is gone; only the processed, metadata-free PNG stays.
    assert not any(browser.calls[0][1].glob("*"))


@pytest.mark.parametrize(
    "build, paired",
    [
        # 1. marked by its item: paired.
        (lambda d: [rendered(picture_file(d))], {"110"}),
        # 2. no marks at all: document order and shape pair the one first-page picture.
        (lambda d: [rendered(picture_file(d), ids=())], {"110"}),
        # A question's own picture (marked with the question) does not count against it.
        (
            lambda d: [
                rendered(picture_file(d, "q.img"), ids=("101",)),
                rendered(picture_file(d), ids=()),
            ],
            {"110"},
        ),
        # An extra unmarked picture of the same shape: not certain, nothing paired.
        (
            lambda d: [
                rendered(picture_file(d), ids=()),
                rendered(picture_file(d, "b.img"), ids=()),
            ],
            set(),
        ),
        # Two pictures inside the one item: not certain.
        (lambda d: [rendered(picture_file(d)), rendered(picture_file(d, "b.img"))], set()),
        # The marked picture has another shape: not paired, and order is not tried instead.
        (lambda d: [rendered(picture_file(d, size=(300, 600)), size=(300, 600))], set()),
        # Bytes not available: nothing to pair.
        (lambda d: [rendered(None)], set()),
        # Not served by Google: ignored.
        (lambda d: [rendered(picture_file(d), ids=(), google=False)], set()),
        # Icons are not pictures.
        (lambda d: [rendered(picture_file(d, size=(24, 24)), ids=(), size=(24, 24))], set()),
        # A picture marked with a page-2 item but shown on page 1 does not fit the page data:
        # not paired here (it pairs when page 2 itself shows it, after 次へ).
        (
            lambda d: [rendered(picture_file(d, size=(600, 400)), ids=("111",), size=(600, 400))],
            set(),
        ),
    ],
)
def test_pictures_pair_only_when_certain(tmp_path, build, paired):
    signed_in(tmp_path)
    form = school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, browser=FakeBrowser(images=build)
    )
    assert {m["item_id"] for m in form["media"] if m.get("image_path")} == paired


def test_order_pairing_needs_every_first_page_picture(tmp_path):
    form = {
        "media": [
            {"item_id": "1", "page": 1, "width": 740, "height": 416},
            {"item_id": "2", "page": 1, "width": 600, "height": 400},
        ],
        "questions": [],
    }
    one = [rendered("/x/a", ids=())]
    assert school_forms.match_images(form, one) == {}
    swapped = [
        rendered("/x/a", ids=(), size=(600, 400)),
        rendered("/x/b", ids=(), size=(740, 416)),
    ]
    assert school_forms.match_images(form, swapped) == {}
    in_order = list(reversed(swapped))
    assert school_forms.match_images(form, in_order) == {"1": "/x/b", "2": "/x/a"}


def test_without_login_the_form_is_unread_and_says_how_to_sign_in(tmp_path):
    browser = FakeBrowser()
    with pytest.raises(school_forms.FormUnread, match="设置 → 学校账号 登录") as caught:
        school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=browser)
    assert caught.value.reason == "login_required" and not browser.calls


def test_expired_login_is_not_tried_again(tmp_path):
    signed_in(tmp_path, state="expired")
    browser = FakeBrowser()
    with pytest.raises(school_forms.FormUnread, match="已过期") as caught:
        school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=browser)
    assert caught.value.reason == "login_expired" and not browser.calls


def test_login_of_another_account_than_the_settings_is_not_used(tmp_path):
    signed_in(tmp_path, email="other@school.example")
    browser = FakeBrowser()
    with pytest.raises(school_forms.FormUnread, match="不一致") as caught:
        school_forms.restricted_form(settings(tmp_path, ACCOUNT), URL, LOGIN_WALL, browser=browser)
    assert caught.value.reason == "account_mismatch" and not browser.calls


def test_pasted_questions_remain_the_fallback(tmp_path):
    pasted = import_form_capture(tmp_path, "", URL, login_page(), [URL])
    assert school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL) == pasted
    signed_in(tmp_path, state="expired")
    # Once signed in, captures belong to the verified account; this one was pasted before.
    with pytest.raises(school_forms.FormUnread):
        school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=FakeBrowser())


def test_expiry_during_a_read_fails_unless_a_recent_capture_exists(tmp_path):
    signed_in(tmp_path)
    expired = school_browser.LoginExpired("学校账号的登录已过期，表单题目没有读到。")
    events = []
    with pytest.raises(school_forms.FormUnread) as caught:
        school_forms.restricted_form(
            settings(tmp_path), URL, LOGIN_WALL, events.append, browser=FakeBrowser(error=expired)
        )
    assert caught.value.reason == "login_expired"
    assert events[-1].startswith("用学校账号读取表单失败：学校账号的登录已过期")
    captured = import_form_capture(tmp_path, ACCOUNT, URL, login_page(), [URL])
    events = []
    form = school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, events.append, browser=FakeBrowser(error=expired)
    )
    # The earlier capture is used, marked so the item says where its questions came from (F12).
    assert form == {**captured, "stale_capture": True}
    assert "改用 24 小时内读取的题目" in events[-1]


def test_a_page_without_question_data_is_a_clear_read_failure(tmp_path):
    signed_in(tmp_path)
    with pytest.raises(school_forms.FormUnread, match="没有题目数据") as caught:
        school_forms.restricted_form(
            settings(tmp_path), URL, LOGIN_WALL, browser=FakeBrowser(html="<html>no</html>")
        )
    assert caught.value.reason == "read_failed"


def test_other_denials_are_not_a_login_question(tmp_path):
    signed_in(tmp_path)
    browser = FakeBrowser()
    forbidden = PermissionDenied("表单只读获取失败（HTTP 403）；请检查原表单的学校账户访问权限。")
    with pytest.raises(PermissionDenied) as caught:
        school_forms.restricted_form(settings(tmp_path), URL, forbidden, browser=browser)
    assert caught.value is forbidden and not browser.calls


def test_school_account_prefers_the_settings_then_the_verified_login(tmp_path):
    assert school_forms.school_account(settings(tmp_path)) == ""
    signed_in(tmp_path)
    assert school_forms.school_account(settings(tmp_path)) == ACCOUNT
    assert (
        school_forms.school_account(settings(tmp_path, "Set@School.example"))
        == "set@school.example"
    )


class FormReader(ReaderFixture):
    def assignments(self, cid):
        assignment = super().assignments(cid)[0]
        assignment["materials"].append({"form": {"formUrl": URL}})
        return [assignment]


def raise_login(_url):
    raise LOGIN_WALL


def test_workflow_reads_a_login_only_form_with_the_school_login(tmp_path, monkeypatch):
    signed_in(tmp_path)
    browser = FakeBrowser(images=lambda d: [rendered(picture_file(d))])
    monkeypatch.setattr(workflow, "read_form", raise_login)
    monkeypatch.setattr(school_browser, "manager", lambda _settings: browser)
    events = []
    with Store(tmp_path) as store:
        course = workflow.sync_course(
            FormReader(), settings(tmp_path), store, "1", progress=events.append, selected_ids={"2"}
        )
        package = Path(
            workflow.prepare_assignment(settings(tmp_path), store, course, "2")["package"]
        )
    assert "正在用学校账号读取需要登录的表单（只读）" in events
    assert any(str(e).startswith("已用学校账号读取表单") for e in events)
    forms = list(course["response_forms"].values())
    assert forms and forms[0]["question_count"] == 3 and forms[0]["page_count"] == 2
    evidence = json.loads((package / "evidence.json").read_text(encoding="utf-8"))
    assert any(source.get("image_path") for source in evidence["sources"])
    unread = [w for w in course["warnings"] if w.get("form_unread")]
    assert not unread


def test_workflow_marks_an_unread_login_only_form(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "read_form", raise_login)
    with Store(tmp_path) as store:
        course = workflow.sync_course(FormReader(), settings(tmp_path), store, "1")
    warning = next(w for w in course["warnings"] if w.get("url") == URL)
    assert warning["form_unread"] == "login_required"
    assert "设置 → 学校账号" in warning["error"]
    assert warning["origins"][0]["role"] == "coursework"
    assert not course["response_forms"]


def test_workflow_form_errors_other_than_login_are_marked_read_failed(tmp_path, monkeypatch):
    def broken(_url):
        raise WorkflowError("Google Forms 只读请求重试后仍失败；题目尚未读取，请稍后重试。")

    monkeypatch.setattr(workflow, "read_form", broken)
    with Store(tmp_path) as store:
        course = workflow.sync_course(FormReader(), settings(tmp_path), store, "1")
    warning = next(w for w in course["warnings"] if w.get("url") == URL)
    assert warning["form_unread"] == "read_failed"


# --------------------------------------------------------------------------- reviewer findings


class ManyForms(FormReader):
    """The assignment's own login-only form, plus weekly attendance forms in announcements."""

    def announcements(self, _):
        return [
            {
                "id": f"a{n}",
                "text": f"出席 https://docs.google.com/forms/d/e/unit-attend-{n:04d}/viewform",
            }
            for n in range(5)
        ]


def counting_browser():
    browser = FakeBrowser()

    def read_page(url, capture_dir):
        browser.calls.append((url, capture_dir))
        html = login_page().replace("Unit login form", "Form " + url[-20:])
        return {"html": html, "images": [], "blocked": 0}

    browser.read_page = read_page
    return browser


def test_only_the_selected_assignments_own_forms_open_the_school_browser(tmp_path, monkeypatch):
    """C2: the attendance forms of other posts are never opened (they use a capture if any);
    the own form is opened once, and a second sync minutes later reuses that read."""
    signed_in(tmp_path)
    browser = counting_browser()
    monkeypatch.setattr(workflow, "read_form", raise_login)
    monkeypatch.setattr(school_browser, "manager", lambda _s: browser)
    with Store(tmp_path) as store:
        course = workflow.sync_course(
            ManyForms(), settings(tmp_path), store, "1", submissions=[{"courseWorkId": "2"}]
        )
        assert [url for url, _ in browser.calls] == [URL]
        workflow.sync_course(
            ManyForms(), settings(tmp_path), store, "1", submissions=[{"courseWorkId": "2"}]
        )
    assert len(browser.calls) == 1
    attendance = [w for w in course["warnings"] if "unit-attend" in (w.get("url") or "")]
    assert len(attendance) == 5 and {w["form_unread"] for w in attendance} == {"not_selected"}
    assert "不是这次处理的作业自己的表单" in attendance[0]["error"]
    # Nothing selected (a sync for another purpose): no browser at all.
    browser.calls.clear()
    school_forms_capture = tmp_path / "browser-form-questions"
    for path in school_forms_capture.glob("*.json"):
        path.unlink()
    with Store(tmp_path) as store:
        workflow.sync_course(ManyForms(), settings(tmp_path), store, "1")
    assert browser.calls == []


def test_a_fresh_read_is_reused_but_an_older_one_is_read_again(tmp_path, monkeypatch):
    signed_in(tmp_path)
    browser = counting_browser()
    school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=browser)
    school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=browser)
    assert len(browser.calls) == 1
    monkeypatch.setattr(school_forms, "FRESH", school_forms.timedelta(0))
    school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=browser)
    assert len(browser.calls) == 2


def test_a_stale_capture_is_noted_with_the_item(tmp_path, monkeypatch):
    """F12: questions from an earlier capture because the login could not read the form now
    come with a 资料缺口 note under 待核对."""
    signed_in(tmp_path)
    import_form_capture(tmp_path, ACCOUNT, URL, login_page(), [URL])
    signed_in(tmp_path, state="expired")
    monkeypatch.setattr(workflow, "read_form", raise_login)
    with Store(tmp_path) as store:
        course = workflow.sync_course(FormReader(), settings(tmp_path), store, "1")
    notes = [w for w in course["warnings"] if w.get("error") == school_forms.STALE_NOTE]
    assert len(notes) == 1 and notes[0]["url"] == URL and not notes[0].get("form_unread")
    assert course["response_forms"]


def test_a_picture_that_cannot_be_kept_does_not_fail_the_read(tmp_path):
    """C9: the questions were read; only the picture stays a gap, and the log says so."""
    signed_in(tmp_path)

    def images(directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "x.img"
        path.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        return [rendered(str(path))]

    events = []
    form = school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, events.append, browser=FakeBrowser(images=images)
    )
    assert len(form["questions"]) == 3 and "stale_capture" not in form
    assert not any("读取表单失败" in str(e) for e in events)
    assert any("图片没能保存" in str(e) for e in events)
    assert str(events[-1]).startswith("已用学校账号读取表单：2 页、3 个题目；")


def test_the_login_required_message_is_neutral_about_whose_form_it_is(tmp_path):
    """C10: the same message is used for forms linked from materials or announcements."""
    with pytest.raises(school_forms.FormUnread) as caught:
        school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL)
    assert str(caught.value).startswith("这份 Google 表单需要学校账号登录才能读取题目")


def test_order_pairing_ignores_header_pictures_and_other_sizes(tmp_path):
    """C7: only pictures inside the form's list of items take part, at the item's own size (or
    scaled down to the column from a wider item)."""
    form = {
        "media": [{"item_id": "110", "page": 1, "width": 740, "height": 416}],
        "questions": [],
    }
    header = rendered("/x/h", ids=(), size=(1600, 900), in_list=False)
    assert school_forms.match_images(form, [header]) == {}
    thumb = rendered("/x/t", ids=(), size=(89, 50))
    assert school_forms.match_images(form, [thumb]) == {}
    column = {**rendered("/x/c", ids=(), size=(740, 416)), "width": 640, "height": 360}
    column.update(attr_width=0, attr_height=0, natural_width=1480, natural_height=832)
    assert school_forms.match_images(form, [column]) == {"110": "/x/c"}
    assert school_forms.match_images(form, [header, rendered("/x/a", ids=())]) == {"110": "/x/a"}


def test_a_signed_in_forms_picture_host_is_googles():
    """2026-11-06, live: signed in, Google serves form pictures from docs.google.com."""
    from classroomautowork.school_browser import google_picture

    assert google_picture("https://docs.google.com/forms-images-rt/ANbbkiiAPdBFpdd8mVHrHOUiq")
    assert google_picture("https://lh7-rt.googleusercontent.com/formsz/AN7BsVA=w740")
    assert not google_picture("https://docs.google.com/forms/d/e/x/viewform")
    assert not google_picture("https://evilgoogleusercontent.com/x.png")
    assert not google_picture("https://docs.google.com.evil.example/forms-images-rt/x")


def test_the_question_picture_pairs_with_the_picture_inside_that_question():
    """The rendered picture inside question 1124768656 (its data-params) is that question's."""
    from test_form_types import LOOK, page

    from classroomautowork.form_read import parse_form

    form = parse_form(page([LOOK]), "https://docs.google.com/forms/d/e/unit-look/viewform")
    rendered = [
        {
            "index": 0,
            "natural_width": 740,
            "natural_height": 1110,
            "width": 590,
            "height": 895,
            "attr_width": 0,
            "attr_height": 0,
            "item_ids": ["1124768656"],
            "in_list": True,
            "google": True,
            "path": "/capture/000.img",
        },
        {
            "index": 1,
            "natural_width": 74,
            "natural_height": 24,
            "width": 74,
            "height": 24,
            "attr_width": 74,
            "attr_height": 24,
            "item_ids": [],
            "in_list": False,
            "google": False,
        },
    ]
    assert school_forms.match_images(form, rendered) == {"1124768656": "/capture/000.img"}


# -- later pages: Google's 次へ in walk mode (school_browser.walk_pages, browser_guard)


def page_two_picture(d):
    return [rendered(picture_file(d, "p2.img", size=(600, 400)), ids=("111",), size=(600, 400))]


def test_a_later_page_picture_is_read_by_turning_to_its_page(tmp_path):
    from classroomautowork.student import StudentProfile

    signed_in(tmp_path)
    StudentProfile(student_id="S1234", name="Test Student").save(tmp_path)
    browser = FakeBrowser(images=lambda d: [rendered(picture_file(d))], walk={2: page_two_picture})
    events = []
    form = school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, events.append, browser=browser
    )
    media = {m["item_id"]: m for m in form["media"]}
    assert Path(media["110"]["image_path"]).is_file()
    assert Path(media["111"]["image_path"]).is_file()
    assert form["read_complete"] and not form["warnings"]
    (walk,) = browser.walks
    assert walk["url"] == URL and walk["target"] == 2
    assert walk["item_pages"] == {"100": 1, "101": 1, "103": 2, "110": 1, "111": 2}
    # The required questions before page 2 get the student's own ID and a marked placeholder;
    # nothing of page 2 itself, and the link is a viewform GET of the same form.
    query = parse_qs(urlsplit(walk["link"]).query)
    assert walk["link"].startswith(URL + "?")
    assert query["entry.200"] == ["S1234"] and query["entry.201"] == ["（翻页用）"]
    assert query["emailAddress"] == [ACCOUNT] and "entry.203" not in query
    assert "正在用学校账号翻到第 2 页（只翻页，不提交）" in "".join(map(str, events))
    assert "已翻到第 2 页，读到 1/1 张后面几页的图片；没有提交，翻页留下的草稿已删除" in map(
        str, events
    )
    assert events[-1].startswith("已用学校账号读取表单：2 页、3 个题目、2 张图片")


def test_a_picture_already_kept_is_not_walked_for_again(tmp_path):
    signed_in(tmp_path)
    browser = FakeBrowser(walk={2: page_two_picture})
    school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=browser)
    _path(tmp_path, URL).unlink()  # read again (no fresh capture), the pictures are kept
    school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=browser)
    assert len(browser.calls) == 2 and len(browser.walks) == 1


def branching_page():
    page = login_page()
    # "Choose" sends option B to another section: the page order depends on the answer.
    return page.replace('[\\"B\\"]', '[\\"B\\", null, 102]').replace('["B"]', '["B", null, 102]')


@pytest.mark.parametrize(
    "html, why",
    [
        (branching_page(), "会按答案跳到不同的页"),
        (
            login_page().replace(
                '[101, "Describe it", null, 1, [[201, null, 1]]]',
                '[101, "Upload it", null, 13, [[201, null, 1]]]',
            ),
            "无法占位的必答题",
        ),
    ],
)
def test_no_walk_for_a_branching_form_or_an_answer_that_cannot_be_prefilled(tmp_path, html, why):
    signed_in(tmp_path)
    browser = FakeBrowser(html=html, walk={2: page_two_picture})
    events = []
    form = school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, events.append, browser=browser
    )
    assert not browser.walks
    assert any("第 2 页图片材料尚未读取" in w for w in form["warnings"])
    assert any(why in str(e) for e in events), events


def test_a_walk_that_fails_leaves_the_gap_and_the_read_stands(tmp_path):
    signed_in(tmp_path)
    browser = FakeBrowser(walk_error=WorkflowError("表单页面加载超时。"))
    events = []
    form = school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, events.append, browser=browser
    )
    assert len(form["questions"]) == 3 and browser.walks
    assert any("第 2 页图片材料尚未读取" in w for w in form["warnings"])
    assert any("翻页读取图片没有完成（表单页面加载超时。）" in str(e) for e in events)


def test_a_walk_that_stops_early_says_where(tmp_path):
    signed_in(tmp_path)
    browser = FakeBrowser()
    events = []
    school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, events.append, browser=browser
    )
    assert any(
        str(e)
        == "已翻到第 1 页，读到 0/1 张后面几页的图片；没有提交（第 1 页没有唯一的「次へ」按钮）"
        for e in events
    ), events


def test_a_later_page_picture_pairs_only_on_its_own_page(tmp_path):
    signed_in(tmp_path)
    import_form_capture(tmp_path, ACCOUNT, URL, login_page(), [URL])
    from classroomautowork.form_read import parse_form

    form = parse_form(login_page(), URL)
    # The page-2 picture rendered on page 2 pairs; the same rendering does not pair on page 1.
    shown = [rendered(picture_file(tmp_path / "c", size=(600, 400)), ids=(), size=(600, 400))]
    assert school_forms.match_images(form, shown, page=2) == {"111": shown[0]["path"]}
    assert school_forms.match_images(form, shown, page=1) == {}


def test_a_draft_the_walk_could_not_delete_is_said_with_the_item(tmp_path):
    signed_in(tmp_path)
    browser = FakeBrowser(walk={2: page_two_picture})
    browser.draft = "left"
    events = []
    form = school_forms.restricted_form(
        settings(tmp_path), URL, LOGIN_WALL, events.append, browser=browser
    )
    assert school_forms.DRAFT_LEFT in form["warnings"] and not form["read_complete"]
    assert school_forms.DRAFT_LEFT in map(str, events)
    assert "フォームをクリア" in school_forms.DRAFT_LEFT


def test_a_login_that_expires_after_a_page_turn_says_the_draft_may_be_left(tmp_path):
    signed_in(tmp_path)
    expired = school_browser.LoginExpired("学校账号的登录已过期，表单后面几页的图片没有读到。")
    expired.draft_left = True
    browser = FakeBrowser(walk_error=expired)
    form = school_forms.restricted_form(settings(tmp_path), URL, LOGIN_WALL, browser=browser)
    assert school_forms.DRAFT_LEFT in form["warnings"]
    assert any("第 2 页图片材料尚未读取" in w for w in form["warnings"])
