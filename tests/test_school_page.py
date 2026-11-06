"""The page's 学校账号 group and its sign-in view (static checks; the staging run drives them)."""

import re
from pathlib import Path

from classroomautowork import ui_jobs

WEB = Path(ui_jobs.__file__).parent / "web"
PAGE = (WEB / "index.html").read_text(encoding="utf-8")
SCRIPT = (WEB / "app.js").read_text(encoding="utf-8")
STYLE = (WEB / "style.css").read_text(encoding="utf-8")


def test_the_group_and_the_view_have_their_controls():
    for name in (
        "schoolSettings",
        "schoolLoginStatus",
        "schoolSignIn",
        "schoolLogout",
        "schoolDialog",
        "schoolHost",
        "schoolHint",
        "schoolHintText",
        "schoolView",
        "schoolFrame",
        "schoolViewNote",
        "schoolText",
        "schoolReveal",
        "schoolSend",
        "schoolBack",
        "schoolStatusLine",
        "schoolError",
        "schoolCancel",
        "schoolFinish",
        "schoolClose",
    ):
        assert f'id="{name}"' in PAGE and f'$("{name}")' in SCRIPT, name
    keys = re.findall(r'data-school-key="(\w+)"', PAGE)
    assert keys == ["Enter", "Backspace", "Tab"]
    for words in (
        "回车",
        "退格",
        "Tab",
        "返回",
        "发送",
        "显示",
        "完成",
        "取消",
        "在这里输入，发送到登录页",
    ):
        assert words in PAGE
    assert "登录只保存在小主机上，只用来读取表单的题目和图片；AI 和程序都不能提交任何内容。" in PAGE
    assert "style=" not in PAGE and "<style" not in PAGE and "<script>" not in PAGE


def test_nothing_typed_is_kept_or_filled_in_by_a_password_manager():
    field = PAGE.split('<input id="schoolText"', 1)[1].split(">", 1)[0]
    for attribute in (
        'autocomplete="off"',
        'spellcheck="false"',
        "data-1p-ignore",
        'enterkeyhint="send"',
    ):
        assert attribute in field
    view = PAGE.split('<dialog id="schoolDialog"', 1)[1].split("</dialog>", 1)[0]
    assert "<form" not in view  # nothing in the view can be submitted anywhere, not even here
    sender = SCRIPT.split("function sendSchoolText(enter) {", 1)[1].split("\n}", 1)[0]
    assert 'input.value = ""' in sender  # emptied as soon as its text is sent
    assert (
        "localStorage"
        not in SCRIPT.split("// ---- settings: the school account", 1)[1].split("// ---- jobs", 1)[
            0
        ]
    )
    # A password field on the sign-in page masks the text here, with 显示 to check it.
    assert 'school.password && !school.reveal ? "password" : "text"' in SCRIPT


def test_the_phone_gets_a_full_screen_above_the_keyboard_and_no_zoom():
    phone = STYLE.split("/* the sign-in view takes the whole screen;", 1)[1].split("\n}", 1)[0]
    assert (
        "height: var(--school-vh, 100dvh)" in phone
        and "inset: var(--school-top, 0px) 0 auto 0" in phone
    )
    assert "visualViewport" in SCRIPT and '"--school-vh"' in SCRIPT
    # 16px text in every input on touch (iOS zooms into anything smaller).
    assert re.search(r"pointer: coarse\) \{\s*:root \{[^}]*--ys-fs-input: 16px", STYLE)
    assert 'class="ys-input school-text"' in PAGE
    # The view never makes the page scroll sideways: the picture fits it.
    assert (
        ".school-frame {" in STYLE
        and "max-width: 100%" in STYLE.split(".school-frame {", 1)[1][:200]
    )


def test_reviewer_findings_on_the_page():
    """F2: the keyboard-following sheet also on an iPad (coarse pointer, wider than a phone).
    F1: typing on a touch screen keeps the picture at the view's width. F3: a window open
    elsewhere can be reopened here. F5/F6/F4: the group's and the panel's actions."""
    ipad = STYLE.split("@media (pointer: coarse) and (min-width: 641px) {", 1)[1].split("\n}", 1)[0]
    assert ".school-dialog.is-keyboard" in ipad and "var(--school-vh" in ipad
    assert "var(--school-top" in ipad
    fit = SCRIPT.split("function fitSchoolFrame() {", 1)[1].split("\n}", 1)[0]
    assert "typing ? Infinity" in fit and "translateY" in fit
    assert ".school-dialog.is-typing .school-frame { max-height: none; }" in STYLE
    assert 'id="schoolTakeover"' in PAGE and "在这里重新打开" in PAGE
    assert "takeover" in SCRIPT.split("async function startSchool(takeover) {", 1)[1][:400]
    assert '"清除登录资料"' in SCRIPT
    panel = SCRIPT.split("function renderPanel() {", 1)[1].split("\n}", 1)[0]
    assert 'button("登录学校账号", "ys-btn ys-btn--sm' in panel
    assert (
        'button("重试未完成", "ys-btn ys-btn--sm' in panel and 'jobAction(jobId, "retry")' in panel
    )
    # Two status lines kept, so a longer one does not shrink the picture (F9).
    assert "min-height: calc(2 * 1.45em)" in STYLE.split(".school-status {", 1)[1][:80]
