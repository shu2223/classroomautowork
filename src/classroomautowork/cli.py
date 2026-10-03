import argparse
import importlib.metadata
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from .auth import credentials_for, secure_keyring
from .buzz import BuzzConfig, setup_buzz
from .config import Settings, assignment_ids
from .document_fill import fill_review
from .errors import WorkflowError
from .google_read import GoogleReader
from .local import state_root
from .pending import discover_pending
from .review import finalize
from .search import retrieve
from .store import Store
from .verify import verify_connection
from .workflow import prepare


def doctor() -> dict:
    packages = {}
    for name in (
        "google-api-python-client",
        "google-auth-oauthlib",
        "keyring",
        "pypdf",
        "pypdfium2",
        "Pillow",
        "defusedxml",
        "filelock",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    try:
        secure_keyring()
        keyring_status = "available"
    except WorkflowError as exc:
        keyring_status = str(exc)
    try:
        backend = BuzzConfig.load()
        buzz = {
            "status": "configured",
            "engine": str(backend.engine),
            "model": backend.model_path,
            "inference": "doctor_does_not_run_inference",
        }
    except WorkflowError as exc:
        buzz = {"status": "unavailable", "reason": str(exc)}
    return {
        "python": sys.version.split()[0],
        "packages": packages,
        "os_credential_store": keyring_status,
        "ffmpeg": shutil.which("ffmpeg"),
        "buzz_executable": shutil.which("buzz"),
        "buzz_backend": buzz,
        "oauth": "not_verified_by_doctor",
        "classroom": "not_verified_by_doctor",
    }


def main(argv=None) -> int:
    # Windows redirected output must remain valid UTF-8, including OAuth prompts.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Read-only local Classroom workflow")
    parser.add_argument("--config", type=Path, help="Private settings JSON, outside Git")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "doctor", help="Report actual dependency availability; does not claim API verification"
    )
    initialize = commands.add_parser("init", help="Write private local course settings")
    initialize.add_argument("--client", required=True, type=Path)
    initialize.add_argument("--school-email", required=True)
    initialize.add_argument(
        "--assignment-url", help="Optional; otherwise verify selects a real pending assignment"
    )
    initialize.add_argument("--timezone", default="Asia/Tokyo")
    initialize.add_argument("--data-dir", type=Path, default=state_root() / "data")
    commands.add_parser("auth", help="Open genuine Google installed-app OAuth in your browser")
    commands.add_parser(
        "auth-documents", help="Authorize Google Docs read/write separately; no Classroom writes"
    )
    fill = commands.add_parser(
        "fill", help="Fill a verified Codex draft into its existing personal submission Doc"
    )
    fill.add_argument("--package", required=True, type=Path)
    verify = commands.add_parser(
        "verify", help="Read the real assignment and download a permitted attachment"
    )
    verify.add_argument("--attachment-id")
    verify.add_argument("--assignment-url", help="Optional explicit verification target")
    pending = commands.add_parser("pending", help="Read pending coursework across active courses")
    pending.add_argument("--due-before", help="Inclusive local date, YYYY-MM-DD")
    pending.add_argument("--include-no-due", action="store_true")
    batch = commands.add_parser("prepare", help="Prepare private source packages for Codex review")
    batch.add_argument("--due-before", help="Inclusive local date across all courses")
    batch.add_argument("--include-no-due", action="store_true")
    batch.add_argument(
        "--evidence-query",
        action="append",
        default=[],
        help="Include extra local retrieval evidence",
    )
    batch.add_argument("--assignment-url", help="Optional single pending assignment")
    batch.add_argument(
        "--defer-media",
        action="store_true",
        help="Explicitly defer media, reporting missing transcripts",
    )
    setup = commands.add_parser(
        "buzz-setup", help="Configure installed Buzz and download a verified local model"
    )
    setup.add_argument("--buzz-root", required=True, type=Path)
    setup.add_argument(
        "--model-size", choices=["tiny", "base", "small", "medium", "large-v3"], default="small"
    )
    setup.add_argument("--language", default="ja")
    commands.add_parser("buzz-status", help="Check actual local engine/model integrity")
    commands.add_parser("tasks", help="Show persistent processing status")
    search = commands.add_parser(
        "search", help="Retrieve evidence from the latest private course cache"
    )
    search.add_argument("--course-id", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=16)
    finish = commands.add_parser(
        "finalize", help="Validate a Codex draft and review locally, then stop"
    )
    finish.add_argument("--package", required=True, type=Path)
    finish.add_argument("--review", required=True, type=Path)
    finish.add_argument("--draft", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "buzz-setup":
            result = setup_buzz(args.buzz_root, args.model_size, args.language)
        elif args.command == "buzz-status":
            config = BuzzConfig.load()
            result = {
                "status": "configured",
                "engine": str(config.engine),
                "model": config.model_path,
                "sha256": config.model_sha256,
                "inference": "Use an actual transcription to verify inference.",
            }
        elif args.command == "finalize":
            result = finalize(args.package, args.review, args.draft)
        elif args.command == "init":
            course, assignment = (
                assignment_ids(args.assignment_url) if args.assignment_url else ("", "")
            )
            settings = Settings(
                school_email=args.school_email,
                client_json=args.client,
                course_id=course,
                assignment_id=assignment,
                data_dir=args.data_dir,
                timezone=args.timezone,
            )
            result = {
                "settings_path": str(settings.save(args.config)),
                "connection_status": "not_verified",
            }
        else:
            settings = Settings.load(args.config)
            if args.command in {"auth", "auth-documents"}:
                _, identity = credentials_for(
                    settings, authorize=True, documents=args.command == "auth-documents"
                )
                result = {
                    "oauth": "authorized",
                    "identity": identity,
                    "connection": "Run verify to validate assignment and download.",
                }
            elif args.command == "fill":
                result = fill_review(settings, args.package)
            elif args.command == "pending":
                credentials, _ = credentials_for(settings)
                result = discover_pending(
                    GoogleReader(credentials),
                    due_before=args.due_before,
                    timezone=settings.timezone,
                    include_no_due=args.include_no_due,
                )
            elif args.command == "prepare":
                result = prepare(
                    settings,
                    due_before=args.due_before,
                    include_no_due=args.include_no_due,
                    target=assignment_ids(args.assignment_url) if args.assignment_url else None,
                    defer_media=args.defer_media,
                    progress=lambda message: print(message, file=sys.stderr, flush=True),
                    extra_queries=args.evidence_query,
                )
            elif args.command in {"tasks", "search"}:
                with Store(settings.data_dir) as store:
                    result = (
                        {"tasks": store.tasks()}
                        if args.command == "tasks"
                        else {
                            "sources": retrieve(
                                store.chunks(args.course_id),
                                args.query,
                                max(1, min(args.limit, 100)),
                            )
                        }
                    )
            else:
                if args.assignment_url:
                    course, assignment = assignment_ids(args.assignment_url)
                    settings = replace(settings, course_id=course, assignment_id=assignment)
                result = verify_connection(settings, attachment_id=args.attachment_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print(
            json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            '{"status":"error","message":"Unexpected local error. No credentials logged."}',
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
