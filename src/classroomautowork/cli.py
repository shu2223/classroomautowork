import argparse
import importlib.metadata
import json
import sys
from pathlib import Path

from .claude_agent import claude_command
from .config import Settings, assignment_ids
from .document_fill import fill_review
from .errors import WorkflowError
from .form_capture import import_form_capture
from .form_fill import fill_response_forms, prepare_response_forms
from .form_read import form_references
from .kb_client import KnowledgeClient
from .local import prefer_ipv4, require_private_path, state_root
from .pending import discover_pending
from .review import finalize
from .school_forms import school_account
from .search import retrieve
from .store import Store
from .workflow import prepare


def doctor(settings: Settings | None) -> dict:
    packages = {}
    for name in ("pypdf", "pypdfium2", "Pillow", "defusedxml", "filelock", "python-docx"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    try:
        claude = claude_command()[0]
    except WorkflowError as exc:
        claude = str(exc)
    knowledge = "settings_missing"
    if settings:
        try:
            status = KnowledgeClient.from_settings(settings).status()
            knowledge = {"index_built": status["built"], "chunks": status["chunks"]}
        except WorkflowError as exc:
            knowledge = str(exc)
    return {
        "python": sys.version.split()[0],
        "packages": packages,
        "claude": claude,
        "knowledge_api": knowledge,
        "model": "not_verified_by_doctor",
    }


def main(argv=None) -> int:
    prefer_ipv4()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Classroom assistant over the course knowledge base"
    )
    parser.add_argument("--config", type=Path, help="Private settings JSON, outside Git")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Report dependencies, Claude and knowledge API reachability")
    initialize = commands.add_parser("init", help="Write private local settings")
    initialize.add_argument("--school-email", default="")
    initialize.add_argument("--client", type=Path, help="Google OAuth client, for document filling")
    initialize.add_argument("--timezone", default="Asia/Tokyo")
    initialize.add_argument("--data-dir", type=Path, default=state_root() / "data")
    fill = commands.add_parser("fill", help="Prefill the original form, or fill a personal Doc")
    fill.add_argument("--package", required=True, type=Path)
    questions = commands.add_parser(
        "import-form-questions",
        help="Import native questions read in the authorized school browser",
    )
    questions.add_argument("--package", required=True, type=Path)
    questions.add_argument("--url", required=True)
    questions.add_argument("--question-data", required=True, type=Path)
    pending = commands.add_parser("pending", help="List pending coursework from the knowledge base")
    pending.add_argument("--due-before", help="Inclusive local date, YYYY-MM-DD")
    pending.add_argument("--include-no-due", action="store_true")
    batch = commands.add_parser("prepare", help="Prepare private source packages for review")
    batch.add_argument("--due-before", help="Inclusive local date across all courses")
    batch.add_argument("--include-no-due", action="store_true")
    batch.add_argument(
        "--evidence-query",
        action="append",
        default=[],
        help="Include extra local retrieval evidence",
    )
    batch.add_argument("--assignment-url", help="Optional single pending assignment")
    commands.add_parser("tasks", help="Show persistent processing status")
    search = commands.add_parser(
        "search", help="Retrieve evidence from the latest private course cache"
    )
    search.add_argument("--course-id", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=16)
    finish = commands.add_parser("finalize", help="Validate a draft and review locally, then stop")
    finish.add_argument("--package", required=True, type=Path)
    finish.add_argument("--review", required=True, type=Path)
    finish.add_argument("--draft", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            try:
                settings = Settings.load(args.config)
            except WorkflowError:
                settings = None
            result = doctor(settings)
        elif args.command == "finalize":
            result = finalize(args.package, args.review, args.draft)
        elif args.command == "init":
            settings = Settings(
                school_email=args.school_email,
                client_json=args.client,
                data_dir=args.data_dir,
                timezone=args.timezone,
            )
            result = {"settings_path": str(settings.save(args.config))}
        else:
            settings = Settings.load(args.config)
            if args.command == "import-form-questions":
                package = require_private_path(args.package)
                manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
                expected = (
                    settings.data_dir
                    / "review-packages"
                    / manifest["course_id"]
                    / manifest["assignment_id"]
                )
                if package.parent != expected:
                    raise WorkflowError("题目导入必须使用当前私人数据目录中的真实作业包。")
                allowed = form_references(manifest["assignment"])
                allowed.extend(form_references(manifest.get("student_submission") or {}))
                for warning in manifest.get("warnings", []):
                    if warning.get("url") and any(
                        x.get("assignment_id") == manifest["assignment_id"]
                        for x in warning.get("origins", [])
                    ):
                        allowed.append(warning["url"])
                form = import_form_capture(
                    settings.data_dir,
                    # The account forms are read with: the settings' school email, else the
                    # one verified at the school sign-in (production leaves the first empty).
                    school_account(settings),
                    args.url,
                    require_private_path(args.question_data).read_text(encoding="utf-8"),
                    allowed,
                )
                result = {
                    "status": "questions_imported",
                    "page_count": form["page_count"],
                    "question_count": len(form["questions"]),
                    "read_complete": form["read_complete"],
                }
            elif args.command == "fill":
                result = (
                    fill_response_forms(settings, args.package)
                    if prepare_response_forms(args.package)
                    else fill_review(settings, args.package)
                )
            elif args.command == "pending":
                result = discover_pending(
                    KnowledgeClient.from_settings(settings),
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
                    progress=lambda message: print(message, file=sys.stderr, flush=True),
                    extra_queries=args.evidence_query,
                )
            else:
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
