"""Login-only Google Forms: read with the school-login browser, never submitted.

When the anonymous read of a form meets Google's login wall, and the user has signed in in
设置 → 学校账号, the school-login browser opens the canonical viewform page in read mode (GET
only; no click, no typing). Its original HTML goes through the same `parse_form` as a public
form and is recorded by form_capture as "school_login_browser", bound to the verified account.
Pictures of the first section are kept only where it is certain which native image item they
belong to (the item's own DOM attribute, or document order together with the picture sizes);
every other picture stays the usual gap. Without a login, or when it fails, a capture of the
last 24 hours (also the questions the user pasted) is used; otherwise the form is unread and the
error says how to sign in.

Only the forms that matter are opened: the selected assignments' own forms (workflow passes
use_browser) and the fill step's re-check of them, and a form read with the login in the last
few minutes is not opened again. A form some other post links to uses a capture, if any.
"""

import secrets
import shutil
from datetime import timedelta
from pathlib import Path

from . import school_browser
from .errors import PermissionDenied, RunCancelled, WorkflowError
from .form_capture import (
    SCHOOL_BROWSER,
    authenticated_form_cache,
    import_form_capture,
    import_form_images,
    needs_login,
    recent_capture,
)
from .form_read import form_url
from .local import require_private_path
from .progress import report

MIN_PICTURE = 48
ASPECT_TOLERANCE = 0.02
SIZE_TOLERANCE = 0.03
# A form read with the school login this recently is not opened again (one job reads its form
# when preparing and re-checks it before the prefill link is made).
FRESH = timedelta(minutes=10)
# Shown with the item (a 资料缺口 under 待核对) when the questions come from an earlier capture
# because the login could not read the form this time.
STALE_NOTE = (
    "这份表单的题目来自 24 小时内保存的一次读取，这次没能用学校账号重新读取"
    "（登录已过期或暂时读不到）；请在原表单核对题目有没有变化，需要时在 设置 → 学校账号 重新登录。"
)


class FormUnread(PermissionDenied):
    """A login-only form could not be read. `reason`: login_required, login_expired,
    account_mismatch or read_failed."""

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


def school_account(settings) -> str:
    """The account forms are read and prefilled with: the configured school email, else the
    email verified at sign-in (production leaves school_email empty)."""
    configured = (getattr(settings, "school_email", "") or "").lower()
    return configured or school_browser.verified_email(Path(settings.data_dir))


def restricted_form(settings, url, error, progress=None, *, browser=None, use_browser=True) -> dict:
    """The questions of a form whose anonymous read raised `error` (PermissionDenied).

    `use_browser` False (a form that is not a selected assignment's own) never opens the
    school-login browser: only a capture is used. A form that comes from an earlier capture
    because the login could not read it carries "stale_capture": True (workflow notes it).
    """
    if not needs_login(error):
        raise error
    data_dir = Path(settings.data_dir)
    canonical = form_url(url)
    school_email = (getattr(settings, "school_email", "") or "").lower()
    account = school_account(settings)
    state = school_browser.login_state(data_dir, school_email)
    if state["state"] == "signed_in" and use_browser:
        recent = recent_capture(data_dir, account, canonical, FRESH, SCHOOL_BROWSER)
        if recent is not None:
            return recent
        report(progress, "正在用学校账号读取需要登录的表单（只读）", "form_read")
        try:
            form = read_with_login(settings, canonical, account, browser=browser, progress=progress)
        except RunCancelled:
            raise
        except WorkflowError as exc:
            cached = _cached(data_dir, account, canonical, error)
            reason = (
                "login_expired"
                if isinstance(exc, school_browser.LoginExpired)
                else (getattr(exc, "reason", "read_failed"))
            )
            if cached is None:
                report(progress, "用学校账号读取表单失败：" + str(exc).split("\n")[0], "form_read")
                raise FormUnread(str(exc), reason) from exc
            report(
                progress,
                "用学校账号读取表单失败（"
                + str(exc).split("\n")[0]
                + "），改用 24 小时内读取的题目",
                "form_read",
            )
            return {**cached, "stale_capture": True}
        pictures = [x for x in form.get("media", []) if x.get("image_path")]
        report(
            progress,
            f"已用学校账号读取表单：{form['page_count']} 页、{len(form['questions'])} 个题目"
            + (f"、{len(pictures)} 张图片" if pictures else "")
            + ("；仍有内容需要在原页面核对" if not form["read_complete"] else ""),
            "form_read",
        )
        return form
    cached = _cached(data_dir, account, canonical, error)
    if cached is not None:
        return {**cached, "stale_capture": True} if state["state"] == "expired" else cached
    if state["state"] == "signed_in":
        raise FormUnread(
            f"这份 Google 表单需要学校账号登录才能读取：{canonical}。它不是这次处理的作业自己的表单，"
            "所以没有为它打开学校账号浏览器。",
            "not_selected",
        ) from error
    if state["state"] == "expired":
        raise FormUnread(
            f"学校账号的登录已过期，表单题目没有读到：{canonical}。"
            "请在 设置 → 学校账号 重新登录后重试。",
            "login_expired",
        ) from error
    if state["state"] == "mismatch":
        raise FormUnread(
            f"已登录的学校账号（{state['email']}）与设置里的学校邮箱（{school_email}）不一致，"
            f"没有用它读取表单：{canonical}。请在 设置 → 学校账号 退出后用 {school_email} 登录。",
            "account_mismatch",
        ) from error
    raise FormUnread(
        f"这份 Google 表单需要学校账号登录才能读取题目：{canonical}。"
        "请在 设置 → 学校账号 登录后重试，或在“补充后重做”里导入题目。",
        "login_required",
    ) from error


def _cached(data_dir, account, url, error):
    try:
        return authenticated_form_cache(data_dir, account, url, error)
    except WorkflowError:
        return None


def read_with_login(settings, url, account, *, browser=None, progress=None) -> dict:
    """Read mode in the school-login browser; the questions and certain pictures are recorded.

    The questions count once they are recorded: a picture that cannot be kept after that (an
    unsupported format, say) stays a gap and is said so, the read itself did not fail."""
    data_dir = Path(settings.data_dir)
    browser = browser or school_browser.manager(settings)
    capture = require_private_path(data_dir / "school-browser-capture" / secrets.token_hex(8))
    try:
        page = browser.read_page(url, capture)
        try:
            form = import_form_capture(
                data_dir, account, url, page["html"], [url], source=SCHOOL_BROWSER
            )
        except PermissionDenied as exc:
            raise FormUnread(
                "用学校账号打开了表单，但页面里没有题目数据：可能这个账号没有权限、表单已关闭，"
                "或页面格式变了。请在浏览器里打开原表单确认。",
                "read_failed",
            ) from exc
        mapping = match_images(form, page.get("images") or [])
        if mapping:
            try:
                form = import_form_images(
                    data_dir,
                    account,
                    url,
                    [{"item_id": item_id, "path": path} for item_id, path in mapping.items()],
                    [url],
                )
            except WorkflowError as exc:
                report(
                    progress,
                    "表单的题目已读到，图片没能保存（" + str(exc).split("\n")[0] + "），仍列为缺口",
                    "form_read",
                )
        return form
    finally:
        shutil.rmtree(capture, ignore_errors=True)


def _aspect_matches(rendered: dict, picture: dict) -> bool:
    expected = picture["width"] / picture["height"]
    for width, height in (
        (rendered.get("attr_width"), rendered.get("attr_height")),
        (rendered.get("width"), rendered.get("height")),
        (rendered.get("natural_width"), rendered.get("natural_height")),
    ):
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            if abs(width / height - expected) <= ASPECT_TOLERANCE * expected:
                return True
    return False


def _size_matches(rendered: dict, picture: dict) -> bool:
    """The picture is shown at the item's own width (its width attribute, its rendered width or
    the pixels Google served for it), or scaled down to the form's column from a wider item."""
    expected = picture["width"]
    close = max(2, SIZE_TOLERANCE * expected)
    widths = [
        rendered.get(key)
        for key in ("attr_width", "width", "natural_width")
        if isinstance(rendered.get(key), int) and rendered.get(key) > 0
    ]
    if any(abs(width - expected) <= close for width in widths):
        return True
    shown = rendered.get("width")
    return isinstance(shown, int) and expected / 2 <= shown < expected


def _big_enough(rendered: dict) -> bool:
    sizes = [
        rendered.get(key) or 0
        for key in ("natural_width", "natural_height", "width", "height")
        if isinstance(rendered.get(key), int)
    ]
    return bool(sizes) and max(sizes) >= MIN_PICTURE


def match_images(form: dict, rendered: list[dict]) -> dict:
    """{native image item ID: saved picture path}, only where the pairing is certain.

    1. A picture inside the element that carries one image item's ID (data-item-id, or the
       data-params of the item) belongs to that item, if its shape fits.
    2. Otherwise, when the remaining Google-hosted pictures inside the form's list of items
       (role=list: never the header or theme picture) that carry no item ID at all are exactly
       as many as the remaining image items of the first section (the only section rendered
       without clicking 次へ), and each pair in document order has the same shape and size,
       they pair in that order.
    Anything else (more or fewer pictures, a shape that does not fit, two pictures for one item)
    pairs nothing: the item keeps its gap warning rather than a guessed picture.
    """
    media = {picture["item_id"]: picture for picture in form.get("media", [])}
    if not media:
        return {}
    known = set(media) | {question["item_id"] for question in form.get("questions", [])}
    candidates = [r for r in rendered if isinstance(r, dict) and r.get("google") and _big_enough(r)]
    mapping, used = {}, set()
    marked = {}
    for index, picture in enumerate(candidates):
        ids = {x for x in picture.get("item_ids") or [] if x in media}
        if len(ids) == 1:
            marked.setdefault(ids.pop(), []).append(index)
    for item_id, indexes in marked.items():
        if len(indexes) != 1:
            used.update(indexes)
            continue
        picture = candidates[indexes[0]]
        used.add(indexes[0])
        if picture.get("path") and _aspect_matches(picture, media[item_id]):
            mapping[item_id] = picture["path"]
    remaining = [
        picture
        for picture in media.values()
        if picture["item_id"] not in mapping
        and picture["item_id"] not in marked
        and picture["page"] == 1
    ]
    unmarked = [
        candidate
        for index, candidate in enumerate(candidates)
        if index not in used
        and candidate.get("in_list") is True
        and not (set(candidate.get("item_ids") or []) & known)
    ]
    if (
        remaining
        and len(unmarked) == len(remaining)
        and all(
            candidate.get("path")
            and _aspect_matches(candidate, picture)
            and _size_matches(candidate, picture)
            for candidate, picture in zip(unmarked, remaining, strict=True)
        )
    ):
        for candidate, picture in zip(unmarked, remaining, strict=True):
            mapping[picture["item_id"]] = candidate["path"]
    return mapping
