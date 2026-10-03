"""Read-only account-wide pending discovery and local-date cutoff selection."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .errors import ConfigurationError, WorkflowError

PENDING_STATES = {"NEW", "CREATED", "RECLAIMED_BY_STUDENT"}


def due_at(item: dict) -> datetime | None:
    day = item.get("dueDate")
    if not day:
        return None
    clock = item.get("dueTime", {})
    return datetime(
        day["year"],
        day["month"],
        day["day"],
        clock.get("hours", 0),
        clock.get("minutes", 0),
        clock.get("seconds", 0),
        tzinfo=UTC,
    )


def cutoff_end(due_before: str, timezone: str) -> datetime:
    try:
        day = date.fromisoformat(due_before)
        return datetime.combine(day + timedelta(days=1), time.min, ZoneInfo(timezone)).astimezone(
            UTC
        )
    except (ValueError, OverflowError) as exc:
        raise ConfigurationError("Cutoff must be a calendar date in YYYY-MM-DD form.") from exc


def discover_pending(
    reader,
    *,
    due_before: str | None = None,
    timezone: str = "Asia/Tokyo",
    include_no_due: bool = False,
) -> dict:
    cutoff = cutoff_end(due_before, timezone) if due_before else None
    selected, ambiguous, inaccessible = [], [], []
    for course in reader.courses():
        try:
            assignments = reader.assignments(course["id"])
            submissions = {x["courseWorkId"]: x for x in reader.own_submissions(course["id"])}
        except WorkflowError as exc:
            inaccessible.append({"course_id": course["id"], "error": str(exc)})
            continue
        for item in assignments:
            own = submissions.get(item["id"])
            status = (own or {}).get("state")
            entry = {
                "course_id": course["id"],
                "course_name": course.get("name"),
                "assignment_id": item["id"],
                "title": item.get("title"),
                "url": item.get("alternateLink"),
                "submission_state": status,
            }
            if status in {"RETURNED", None, "SUBMISSION_STATE_UNSPECIFIED"}:
                # A returned/graded assignment is not automatically a new pending task.
                ambiguous.append(
                    {**entry, "reason": "Submission status needs user interpretation."}
                )
                continue
            if status not in PENDING_STATES:
                continue
            due = due_at(item)
            entry["due_utc"] = due.isoformat() if due else None
            entry["due_local"] = due.astimezone(ZoneInfo(timezone)).isoformat() if due else None
            if (due is None and not include_no_due) or (cutoff and due and due >= cutoff):
                continue
            selected.append(entry)
    selected.sort(key=lambda x: (x["due_utc"] or "9999", x["course_id"], x["assignment_id"]))
    return {
        "timezone": timezone,
        "due_before_inclusive": due_before,
        "assignments": selected,
        "needs_confirmation": ambiguous,
        "inaccessible_courses": inaccessible,
        "complete_discovery": not inaccessible,
    }
