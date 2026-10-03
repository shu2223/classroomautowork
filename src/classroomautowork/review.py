"""Validate a Codex-produced review locally; never generate an answer from a template."""

import json
import re
from pathlib import Path

from .errors import WorkflowError
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .policy import CoursePolicy, confirmed_gate
from .store import Store


def finalize(package: Path, review_path: Path, draft_path: Path | None = None) -> dict:
    package = require_private_path(package)
    review_path = require_private_path(review_path)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    sources = {
        x["id"]: x
        for x in json.loads((package / "evidence.json").read_text(encoding="utf-8"))["sources"]
    }
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if package.parents[2].name != "review-packages":
        raise WorkflowError("Use an actual prepared review-package directory.")
    root = package.parents[3]
    policy = CoursePolicy.load(root, manifest["course_id"])
    gate = policy.draft_gate()
    if "ai_confirmation" in manifest:
        gate = confirmed_gate(gate, manifest["ai_confirmation"])
    if gate != manifest["policy"]:
        raise WorkflowError("Course policy changed since preparation; prepare the package again.")
    with Store(root) as store:
        current = {x["id"]: x for x in store.chunks(manifest["course_id"])}
        for source in sources.values():
            real = current.get(source["id"])
            if not real or any(
                source.get(k) != real.get(k)
                for k in ("text", "revision", "source_id", "locator", "url", "image_path")
            ):
                raise WorkflowError(
                    "Evidence changed or is no longer in the current readable cache; prepare again."
                )
    draft = require_private_path(draft_path).read_text(encoding="utf-8") if draft_path else ""
    if draft.strip() and not manifest["policy"]["can_draft"]:
        raise WorkflowError("This package's course AI policy blocks an answer draft.")
    checks = review.get("requirement_checks", [])
    questions = review.get("questions", [])
    if (
        not checks
        or not isinstance(questions, list)
        or not all(isinstance(q, str) for q in questions)
    ):
        raise WorkflowError("Review needs requirement checks and a list of unanswered questions.")
    for check in checks:
        if (
            not check.get("requirement")
            or check.get("requirement_source_id") not in manifest["requirement_source_ids"]
            or check.get("status") not in {"met", "partial", "unmet", "needs_user"}
        ):
            raise WorkflowError(
                "Each requirement must identify its actual assignment source and status."
            )
        if any(x not in sources for x in check.get("evidence_ids", [])):
            raise WorkflowError("Requirement check contains an unknown evidence ID.")
    referenced = set(re.findall(r"\[E:([^\]]+)\]", draft))
    if referenced - sources.keys():
        raise WorkflowError("Draft contains a citation outside this package's actual evidence.")
    claims = review.get("claim_checks", [])
    for claim in claims:
        ids = claim.get("evidence_ids", [])
        if claim.get("kind") not in {"sourced", "analysis", "user_fact"} or not claim.get("claim"):
            raise WorkflowError("Claim checks require an explicit kind and claim text.")
        if any(i not in sources for i in ids) or (claim["kind"] == "sourced" and not ids):
            raise WorkflowError("A sourced claim needs valid evidence IDs.")
        if draft.strip() and claim["kind"] == "sourced" and not set(ids).issubset(referenced):
            raise WorkflowError("Sourced draft claims must display their evidence citations.")
        if claim["kind"] == "user_fact":
            indices = claim.get("personal_fact_indices", [])
            if not indices or any(
                not isinstance(i, int) or i < 0 or i >= len(manifest["policy"]["personal_facts"])
                for i in indices
            ):
                raise WorkflowError(
                    "Personal-experience claims must refer to facts supplied by the user."
                )
        for quote in claim.get("quotes", []):
            source = sources.get(quote.get("evidence_id"))
            if not source or not quote.get("text") or quote["text"] not in source["text"]:
                raise WorkflowError("Quotation does not match the actual source text.")
    if draft.strip() and (not claims or not review.get("ai_policy_checked")):
        raise WorkflowError("An answer draft needs claim checks and explicit course-policy review.")
    if not draft.strip() and not questions:
        raise WorkflowError("A blocked review must explain what the user needs to confirm.")
    output = package / "review.json"
    atomic_json(output, review)
    if draft_path and draft_path.resolve() != (package / "draft.md").resolve():
        (package / "draft.md").write_text(draft, encoding="utf-8")
    (package / "checklist.md").write_text(
        "\n".join(
            f"- [{'x' if c['status'] == 'met' else ' '}] {c['requirement']} — {c['status']} ({c.get('draft_location', '待补充')}) [E:{c['requirement_source_id']}]"
            for c in checks
        )
        + "\n",
        encoding="utf-8",
    )
    (package / "questions.md").write_text(
        "\n".join("- " + q for q in questions) + "\n", encoding="utf-8"
    )
    receipt = {
        "status": "ready_for_human_review" if draft.strip() else "needs_user_input",
        "finalized_at": utc_now(),
        "package": str(package),
        "draft_sha256": sha256_file(package / "draft.md") if draft.strip() else None,
        "review_sha256": sha256_file(output),
        "submission": "manual_only",
        "citation_validation": "IDs and exact quotations checked; semantic claims require human review.",
    }
    atomic_json(package / "review-receipt.json", receipt)
    return receipt
