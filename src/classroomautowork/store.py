"""Restartable tasks and integrity-checked cache; one writer per private data directory."""

import hashlib
import json
import sqlite3
from pathlib import Path

from filelock import FileLock, Timeout

from .errors import WorkflowError
from .local import require_private_path, sha256_file, utc_now


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def artifact(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": sha256_file(path)}


class Store:
    """Hold this context while running a pipeline. Crashes release the OS file lock.

    Completed stages survive restart. Interrupted/failed stages retry on the next invocation.
    Cache keys include source revision and processor identity, not a display filename.
    """

    def __init__(self, root: Path):
        self.root = require_private_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.root / "pipeline.lock"))
        self.stats = {"reused": 0, "processed": 0, "failed": 0}

    def __enter__(self):
        try:
            self.lock.acquire(timeout=0)
        except Timeout as exc:
            raise WorkflowError("Another local run is active; wait for it to finish.") from exc
        self.db = sqlite3.connect(self.root / "tasks.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT, updated_at TEXT NOT NULL
        )""")
        self.db.execute("UPDATE tasks SET status='interrupted' WHERE status='running'")
        self.db.execute("""CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY, course_id TEXT NOT NULL, source_id TEXT NOT NULL,
            source_kind TEXT NOT NULL, title TEXT NOT NULL, url TEXT, locator TEXT NOT NULL,
            text TEXT NOT NULL, image_path TEXT, revision TEXT NOT NULL
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS chunks_course ON chunks(course_id)")
        self.db.commit()
        return self

    def __exit__(self, *_):
        self.db.close()
        self.lock.release()

    def _valid(self, result: dict) -> bool:
        for file in result.get("artifacts", []):
            path = Path(file["path"])
            if not path.is_relative_to(self.root) or not path.is_file():
                return False
            if sha256_file(path) != file["sha256"]:
                return False
        return True

    def memo(self, kind: str, identity, operation) -> dict:
        key = fingerprint([kind, identity])
        row = self.db.execute("SELECT * FROM tasks WHERE key=?", (key,)).fetchone()
        if row and row["status"] == "done":
            result = json.loads(row["result"])
            if self._valid(result):
                self.stats["reused"] += 1
                return result
        self.db.execute(
            """INSERT INTO tasks(key,kind,status,attempts,updated_at) VALUES(?,?,'running',1,?)
            ON CONFLICT(key) DO UPDATE SET status='running',attempts=attempts+1,
                error=NULL,updated_at=excluded.updated_at""",
            (key, kind, utc_now()),
        )
        self.db.commit()
        try:
            result = operation(key)
            if not self._valid(result):
                raise WorkflowError("A processing stage did not produce valid private artifacts.")
            self.db.execute(
                "UPDATE tasks SET status='done',result=?,updated_at=? WHERE key=?",
                (json.dumps(result, ensure_ascii=False), utc_now(), key),
            )
            self.db.commit()
            self.stats["processed"] += 1
            return result
        except Exception as exc:
            # Third-party exception strings can contain credentials or private command output.
            error = str(exc) if isinstance(exc, WorkflowError) else "Local processing failed."
            self.db.execute(
                "UPDATE tasks SET status='failed',error=?,updated_at=? WHERE key=?",
                (error, utc_now(), key),
            )
            self.db.commit()
            self.stats["failed"] += 1
            if isinstance(exc, WorkflowError):
                raise
            raise WorkflowError(error) from exc

    def replace_chunks(self, course_id: str, source_id: str, revision: str, chunks: list[dict]):
        self.db.execute(
            "DELETE FROM chunks WHERE course_id=? AND source_id=?", (course_id, source_id)
        )
        for chunk in chunks:
            cid = fingerprint([course_id, source_id, revision, chunk["locator"]])[:24]
            self.db.execute(
                "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    course_id,
                    source_id,
                    chunk["source_kind"],
                    chunk["title"],
                    chunk.get("url"),
                    chunk["locator"],
                    chunk["text"],
                    chunk.get("image_path"),
                    revision,
                ),
            )
        self.db.commit()

    def prune(self, course_id: str, readable_sources: set[str]):
        """Only sources observed in this live sync remain retrievable, including on partial failure."""
        for row in self.db.execute(
            "SELECT DISTINCT source_id FROM chunks WHERE course_id=?", (course_id,)
        ):
            if row[0] not in readable_sources:
                self.db.execute(
                    "DELETE FROM chunks WHERE course_id=? AND source_id=?", (course_id, row[0])
                )
        self.db.commit()

    def chunks(self, course_id: str) -> list[dict]:
        return [
            dict(x)
            for x in self.db.execute(
                "SELECT * FROM chunks WHERE course_id=? ORDER BY id", (course_id,)
            )
        ]

    def tasks(self) -> list[dict]:
        return [
            dict(x)
            for x in self.db.execute(
                "SELECT key,kind,status,attempts,error,updated_at FROM tasks ORDER BY updated_at DESC"
            )
        ]
