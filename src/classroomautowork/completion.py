"""Trusted one-click mode: deliver candidates, retain uncertainty outside the answer."""

MODE = "review-candidates-v1"


def candidate_mode(manifest):
    # This context is written by the Python launcher, never copied from course text.
    return manifest.get("generation_context", {}).get("completion_mode") == MODE


def review_cautions(package, receipt):
    import json

    review = json.loads((package / "review.json").read_text(encoding="utf-8"))
    generation = receipt.get("generation", {})
    return list(
        dict.fromkeys(
            [c["requirement"] for c in review["requirement_checks"] if c["status"] != "met"]
            + review.get("questions", [])
            + generation.get("missing_sources", [])
        )
    )
