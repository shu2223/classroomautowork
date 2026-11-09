"""A new Google Doc with the verified answer, in the student's own Drive.

The grant is Drive's drive.file scope: the program can open and change only the files it created
itself, never any other file in the account (a teacher's material, a classmate's shared copy).
The answer goes up as the reviewed Word file, which Drive converts to a Google Doc; its text is
read back before the link is shown, and a copy that does not read back goes to the trash.

Nothing here calls Classroom. Its API would refuse anyway: a program may change a submission's
attachments only for coursework that the same program created. The student attaches the file in
Classroom and hands it in.

Authorization is a loopback OAuth with PKCE for the installed-app client in data_dir. The mini PC
cannot receive the redirect, so the page asks for the address Google redirected to (a page that
does not open): its code is exchanged here once and never stored or logged. Forgetting the grant
only deletes the token here. Revoking it at Google could also cut off the course knowledge base,
which uses the same client."""

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .answer_document import create_answer_document
from .errors import ConfigurationError, PermissionDenied, WorkflowError
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .student import StudentProfile

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
REDIRECT_PORT = 8766
REDIRECT = f"http://localhost:{REDIRECT_PORT}/"
SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
)
SCOPE_ALIASES = {"email": "https://www.googleapis.com/auth/userinfo.email"}
CLIENT_FILE = "google-oauth-client.json"
GRANT_FILE = "google-drive-grant.json"
PENDING_FILE = "google-drive-pending.json"
RECORD_FILE = "google-doc.json"
PENDING_SECONDS = 15 * 60
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC = "application/vnd.google-apps.document"
FILE_ID = re.compile(r"[A-Za-z0-9_-]{20,200}")
EXPIRED = "Google 文档授权已失效，请在 设置 → Google 文档 重新授权。"

# Replaced by a fake in tests; nothing in the test suite reaches Google.
http = requests


@contextmanager
def _network():
    try:
        yield
    except requests.RequestException as exc:
        raise WorkflowError("连不上 Google，请稍后再试。") from exc


def _client(data_dir):
    path = Path(data_dir) / CLIENT_FILE
    if not path.is_file():
        raise ConfigurationError("小主机上还没有 Google 授权用的客户端配置，暂时不能新建文档。")
    try:
        client = json.loads(path.read_text(encoding="utf-8-sig")).get("installed") or {}
    except ValueError as exc:
        raise ConfigurationError("Google 授权用的客户端配置无效。") from exc
    if (
        not client.get("client_id")
        or not client.get("client_secret")
        or client.get("token_uri") != TOKEN_URL
    ):
        raise ConfigurationError("Google 授权用的客户端配置不是 Google 的桌面应用客户端。")
    return client


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _scopes(value):
    return {SCOPE_ALIASES.get(x, x) for x in (value or "").split()}


def status(data_dir, account=""):
    """What the settings page shows; reads only local files."""
    data_dir = Path(data_dir)
    try:
        _client(data_dir)
        client = True
    except ConfigurationError:
        client = False
    grant = _read(data_dir / GRANT_FILE) or {}
    email = grant.get("email") if grant.get("refresh_token") else None
    return {
        "client": client,
        "authorized": bool(email),
        "email": email,
        "granted_at": grant.get("granted_at") if email else None,
        "account": account or None,
        "mismatch": bool(email and account and email.lower() != account.lower()),
    }


def start(data_dir, account=""):
    """A consent address for the school account; the verifier waits here for 15 minutes."""
    client = _client(data_dir)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(24)
    atomic_json(
        Path(data_dir) / PENDING_FILE,
        {"state": state, "verifier": verifier, "created": time.time()},
    )
    params = {
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent select_account",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if account:
        params["login_hint"] = account
    return {"url": AUTH_URL + "?" + urlencode(params), "account": account or None}


def _identity(access):
    with _network():
        response = http.get(USERINFO_URL, headers={"Authorization": "Bearer " + access}, timeout=30)
    if response.status_code != 200:
        raise PermissionDenied("读不到授权账号，请重新授权。")
    identity = response.json()
    if not identity.get("email_verified") or not identity.get("email"):
        raise PermissionDenied("授权账号的邮箱没有经过 Google 验证，没有保存。")
    return identity["email"].lower()


def finish(data_dir, pasted, account=""):
    """Exchange the code in the address Google redirected to. Errors never repeat the address."""
    data_dir = Path(data_dir)
    pending_path = data_dir / PENDING_FILE
    pending = _read(pending_path)
    if not pending or time.time() - pending.get("created", 0) > PENDING_SECONDS:
        pending_path.unlink(missing_ok=True)
        raise WorkflowError("这次授权已经超时，请重新点「授权」。")
    if not isinstance(pasted, str) or not pasted.strip() or len(pasted) > 4096:
        raise WorkflowError("请粘贴授权后浏览器地址栏里的完整网址。")
    parts = urlsplit(pasted.strip())
    try:
        port = parts.port
    except ValueError:
        port = None
    if (
        parts.scheme != "http"
        or parts.hostname not in {"localhost", "127.0.0.1"}
        or port != REDIRECT_PORT
    ):
        raise WorkflowError(
            f"这不是授权完成后的网址。它应该以 {REDIRECT} 开头（那个页面打不开是正常的）。"
        )
    query = parse_qs(parts.query)
    if query.get("error"):
        pending_path.unlink(missing_ok=True)
        raise PermissionDenied("Google 页面上没有同意授权，没有保存任何权限。")
    state, code = query.get("state", [""])[0], query.get("code", [""])[0]
    if not code or not secrets.compare_digest(state, pending.get("state", "")):
        raise WorkflowError("这个网址不属于这次授权，请用刚打开的授权页重新操作。")
    client = _client(data_dir)
    pending_path.unlink(missing_ok=True)
    with _network():
        response = http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "redirect_uri": REDIRECT,
                "code_verifier": pending["verifier"],
            },
            timeout=30,
        )
    if response.status_code != 200:
        raise PermissionDenied("Google 没有发放权限（授权码可能已用过或过期），请重新授权。")
    got = response.json()
    granted = _scopes(got.get("scope"))
    if granted != set(SCOPES):
        raise PermissionDenied("Google 给的权限和请求的不一致，没有保存。请重新授权并全部同意。")
    if not got.get("refresh_token") or not got.get("access_token"):
        raise PermissionDenied("Google 没有发放可长期使用的权限，请重新授权。")
    email = _identity(got["access_token"])
    if account and email != account.lower():
        raise PermissionDenied(
            f"授权的是 {email}，不是学校账号 {account}。请重新授权，在 Google 页面选择学校账号。"
        )
    atomic_json(
        data_dir / GRANT_FILE,
        {
            "refresh_token": got["refresh_token"],
            "scopes": sorted(granted),
            "email": email,
            "client_id": client["client_id"],
            "granted_at": utc_now(),
        },
    )
    return status(data_dir, account)


def forget(data_dir, account=""):
    data_dir = Path(data_dir)
    (data_dir / GRANT_FILE).unlink(missing_ok=True)
    (data_dir / PENDING_FILE).unlink(missing_ok=True)
    return status(data_dir, account)


def _access(data_dir, account):
    """A fresh access token for the stored grant, and the account it belongs to."""
    data_dir = Path(data_dir)
    grant = _read(data_dir / GRANT_FILE)
    if not grant or not grant.get("refresh_token"):
        raise PermissionDenied("还没有授权新建 Google 文档，请先在 设置 → Google 文档 授权。")
    client = _client(data_dir)
    if grant.get("client_id") != client["client_id"]:
        raise PermissionDenied(EXPIRED)
    if account and grant.get("email") != account.lower():
        raise PermissionDenied(
            f"授权的账号 {grant.get('email')} 不是学校账号 {account}，请重新授权。"
        )
    with _network():
        response = http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": grant["refresh_token"],
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
            },
            timeout=30,
        )
    if response.status_code != 200:
        raise PermissionDenied(EXPIRED)
    got = response.json()
    if got.get("scope") and _scopes(got["scope"]) != set(SCOPES):
        raise PermissionDenied(EXPIRED)
    access = got.get("access_token")
    if not access:
        raise PermissionDenied(EXPIRED)
    email = _identity(access)
    if email != grant["email"]:
        raise PermissionDenied(EXPIRED)
    return access, email


def needs_own_document(manifest, response_forms):
    """A written answer with nowhere to write it: no form to prefill, no copy of the teacher's
    file made for each student, and not handed in yet. The page suggests a new document then."""
    if response_forms:
        return False
    submission = manifest.get("student_submission") or {}
    if submission.get("state") in {"TURNED_IN", "RETURNED"}:
        return False
    assignment = manifest.get("assignment") or {}
    if assignment.get("workType", "ASSIGNMENT") != "ASSIGNMENT":
        return False
    return not any(
        (material.get("driveFile") or {}).get("shareMode") == "STUDENT_COPY"
        for material in assignment.get("materials") or []
    )


def _pieces(blocks):
    """The answer's text, in order, as the Doc must show it."""
    for block in blocks:
        if block["kind"] == "table":
            yield from (cell for row in block["rows"] for cell in row)
        elif block["kind"] == "list":
            yield from (item["text"] for item in block["items"])
        else:
            yield block["text"]


def _squeeze(text):
    return re.sub(r"[\s​‌‍⁠﻿]+", "", text)


def reads_back(exported, blocks):
    """Every piece of the answer, whitespace aside, appears in the Doc in order."""
    text, at = _squeeze(exported), 0
    for piece in _pieces(blocks):
        piece = _squeeze(piece)
        if not piece:
            continue
        found = text.find(piece, at)
        if found < 0:
            return False
        at = found + len(piece)
    return True


def document_name(document, profile):
    blocks = document["blocks"]
    title = (
        blocks[0]["text"]
        if blocks and blocks[0]["kind"] == "heading" and blocks[0]["level"] == 1
        else document.get("title") or "作业答案"
    )
    name = (getattr(profile, "name", "") or "").strip()
    if name and name not in title:
        title = f"{title}_{name}"
    return re.sub(r"\s+", " ", title).strip()[:120] or "作业答案"


def _exists(access, file_id):
    with _network():
        response = http.get(
            f"{FILES_URL}/{file_id}",
            params={"fields": "id,trashed,webViewLink"},
            headers={"Authorization": "Bearer " + access},
            timeout=30,
        )
    return response.status_code == 200 and not response.json().get("trashed")


def create(data_dir, package, account=""):
    """The answer as a new Google Doc; the same answer reuses the Doc made for it before."""
    package = require_private_path(Path(package))
    document = create_answer_document(package)
    record_path = package / RECORD_FILE
    record = _read(record_path)
    access, email = _access(data_dir, account)
    if (
        record
        and record.get("inputs") == document["inputs"]
        and record.get("email") == email
        and FILE_ID.fullmatch(record.get("file_id", ""))
        and _exists(access, record["file_id"])
    ):
        return {**record, "reused": True}
    name = document_name(document, StudentProfile.load(Path(data_dir)))
    boundary = "classroomautowork-" + uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps({"name": name, "mimeType": GOOGLE_DOC}, ensure_ascii=False)
            + f"\r\n--{boundary}\r\nContent-Type: {DOCX}\r\n\r\n"
        ).encode("utf-8")
        + (package / "answer.docx").read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    with _network():
        response = http.post(
            UPLOAD_URL,
            params={"uploadType": "multipart", "fields": "id,name,mimeType,webViewLink"},
            data=body,
            headers={
                "Authorization": "Bearer " + access,
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            timeout=120,
        )
    if response.status_code != 200:
        raise WorkflowError(
            f"Google 云端硬盘没有新建文档（HTTP {response.status_code}），请稍后再试或下载 Word。"
        )
    created = response.json()
    file_id = created.get("id", "")
    if not FILE_ID.fullmatch(file_id) or created.get("mimeType") != GOOGLE_DOC:
        raise WorkflowError("Google 云端硬盘返回的不是 Google 文档，请下载 Word 自己上传。")
    try:
        with _network():
            exported = http.get(
                f"{FILES_URL}/{file_id}/export",
                params={"mimeType": "text/plain"},
                headers={"Authorization": "Bearer " + access},
                timeout=60,
            )
        exported.encoding = "utf-8"
        same = exported.status_code == 200 and reads_back(exported.text, document["blocks"])
    except WorkflowError:
        same = False
    if not same:
        # Only the file just made, which is all drive.file reaches; to the trash, not deleted.
        try:
            with _network():
                trashed = http.patch(
                    f"{FILES_URL}/{file_id}",
                    json={"trashed": True},
                    headers={"Authorization": "Bearer " + access},
                    timeout=30,
                )
            moved = trashed.status_code == 200
        except WorkflowError:
            moved = False
        raise WorkflowError(
            "新建的文档读回来和答案对不上，"
            + ("已经把它移到云端硬盘的回收站。" if moved else f"请在云端硬盘里删掉「{name}」。")
            + "请下载 Word 自己上传。"
        )
    record = {
        "file_id": file_id,
        "name": created.get("name") or name,
        "url": f"https://docs.google.com/document/d/{file_id}/edit",
        "email": email,
        "created_at": utc_now(),
        "inputs": document["inputs"],
        "docx_sha256": sha256_file(package / "answer.docx"),
        "submission": "manual_only",
    }
    atomic_json(record_path, record)
    return record


def current(package):
    """The Doc made for the answer as it is now, or None."""
    package = Path(package)
    record = _read(package / RECORD_FILE)
    document = _read(package / "answer-document.json")
    if not record or not document or record.get("inputs") != document.get("inputs"):
        return None
    return record
