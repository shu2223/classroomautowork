"""Read-only client for the course knowledge API, the only source of course data.

The API belongs to the separate knowledge-base service on this machine. Method names mirror the
reader interface `pending` and `workflow` use, so those stay independent of where data comes from.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .errors import ConfigurationError, WorkflowError

TIMEOUT_SECONDS = 60
UNREACHABLE = "连不上课程资料接口，请确认小主机上的 knowledge-api 服务正在运行。"


class KnowledgeClient:
    def __init__(self, url: str, token_file: Path | str, *, opener=None):
        self.url = url.rstrip("/")
        try:
            self.token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError("读不到课程资料接口的访问凭证。") from exc
        # Loopback traffic must not follow a proxy configured in the environment.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._slugs = None
        self._posts = {}

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.kb_url, settings.kb_token_file)

    def _bytes(self, path, **query):
        query = {k: v for k, v in query.items() if v is not None}
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
        try:
            with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ConfigurationError("课程资料接口拒绝了访问凭证。") from None
            raise WorkflowError(f"课程资料接口返回错误（{exc.code}）：{path}") from None
        except OSError:
            raise WorkflowError(UNREACHABLE) from None

    def _json(self, path, **query):
        try:
            return json.loads(self._bytes(path, **query))
        except ValueError:
            raise WorkflowError("课程资料接口返回的内容无法解析。") from None

    def status(self):
        return self._json("/v1/status")

    def _slug(self, course_id):
        if self._slugs is None:
            self._slugs = {
                c["course_id"]: c["slug"] for c in self._json("/v1/courses") if c["course_id"]
            }
        if course_id not in self._slugs:
            raise WorkflowError("课程资料库里没有这门课程。")
        return self._slugs[course_id]

    def course_posts(self, course_id):
        """One read per client: a run sees a single consistent snapshot of the course."""
        if course_id not in self._posts:
            self._posts[course_id] = self._json(f"/v1/courses/{self._slug(course_id)}/posts")
        return self._posts[course_id]

    def _kind(self, course_id, kind):
        return [p for p in self.course_posts(course_id)["posts"] if p["_kind"] == kind]

    def courses(self):
        return [
            {"id": c["course_id"], "name": c["name"]}
            for c in self._json("/v1/courses")
            if c["course_id"]
        ]

    def course(self, course_id):
        data = self.course_posts(course_id)
        return {"id": data["course_id"], "name": data["name"]}

    def assignments(self, course_id):
        return self._kind(course_id, "coursework")

    def course_materials(self, course_id):
        return self._kind(course_id, "material")

    def announcements(self, course_id):
        return self._kind(course_id, "announcement")

    def assignment(self, course_id, assignment_id):
        for post in self.assignments(course_id):
            if post.get("id") == assignment_id:
                return post
        raise WorkflowError("课程资料库里没有这项作业。")

    def own_submissions(self, course_id):
        """The fetcher records only this student's state, not the submission's attachments."""
        return [
            {"courseWorkId": p["id"], "state": p["_submission"]}
            for p in self.assignments(course_id)
            if p.get("_submission")
        ]

    def file_document(self, course_id, file_id):
        return self.course_posts(course_id)["files"].get(file_id)

    def documents(self, course_id):
        return self._json(f"/v1/courses/{self._slug(course_id)}/documents")

    def document_chunks(self, doc_id):
        chunks, start = [], 1
        while True:
            page = self._json(f"/v1/documents/{doc_id}/chunks", **{"from": start})
            chunks.extend(page["chunks"])
            if not page["chunks"] or len(chunks) >= page["total"]:
                return chunks
            start = page["chunks"][-1]["ord"] + 1

    def document_source(self, doc_id):
        return self._bytes(f"/v1/documents/{doc_id}/source")
