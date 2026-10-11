"""Small, deterministic batches: deliver one course before preparing the next."""


def course_batches(items):
    """Keep the caller's priority order and synchronize each course only once."""
    courses = {}
    seen = set()
    for item in items:
        key = (item["course_id"], item["assignment_id"])
        if key not in seen:
            courses.setdefault(key[0], []).append(item)
            seen.add(key)
    return list(courses.values())
