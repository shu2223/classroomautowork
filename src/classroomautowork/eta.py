"""Measured, local ETA profiles; reading an estimate never manufactures progress.

Only successful observations enter the history file. Its keys are hashes of a
small allowlist of execution settings, and its values contain numbers only.
Unknown work deliberately prevents an overall ETA without hiding a useful ETA
for the operation currently running.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import tempfile
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

_MAX_PROFILES = 256
_MAX_SAMPLES = 24
_IDENTITY_FIELDS = (
    "model",
    "model_sha256",
    "engine",
    "language",
    "rate_key",
    "effort",
    "backend",
)


def _number(value):
    """Accept finite nonnegative measurements, never bools or numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


def _counter(context):
    if context.get("phase") in {"prepare", "infer"} and (
        "phase_current_seconds" in context or "phase_total_seconds" in context
    ):
        return (
            _number(context.get("phase_current_seconds")),
            _number(context.get("phase_total_seconds")),
            "media_seconds",
        )
    if "media_current_seconds" in context or "media_total_seconds" in context:
        return (
            _number(context.get("media_current_seconds")),
            _number(context.get("media_total_seconds")),
            "media_seconds",
        )
    for prefix, unit in (("bytes", "bytes"), ("pages", "pages")):
        if f"{prefix}_current" in context or f"{prefix}_total" in context:
            return (
                _number(context.get(f"{prefix}_current")),
                _number(context.get(f"{prefix}_total")),
                unit,
            )
    return (
        _number(context.get("current")),
        _number(context.get("total")),
        str(context.get("unit") or "tasks")[:40],
    )


def _key(stage, context, *, unit=None, duration=False, phase=None):
    # Hash every label too: file paths/names accidentally supplied as a model or
    # stage can never become plaintext private data in the performance history.
    identity = {
        "stage": str(stage),
        "unit": "tasks" if duration else (unit or _counter(context)[2]),
        "phase": "" if duration else str(context.get("phase", "") if phase is None else phase),
        **{field: str(context.get(field, "")) for field in _IDENTITY_FIELDS},
    }
    raw = json.dumps(identity, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _empty_profile():
    return {"rates": [], "durations": [], "overheads": []}


def _load_profiles(path):
    try:
        if path.stat().st_size > 1_000_000:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
            return {}
        result = {}
        for key, profile in list(data["profiles"].items())[-_MAX_PROFILES:]:
            if (
                not isinstance(key, str)
                or len(key) != 64
                or any(x not in "0123456789abcdef" for x in key)
                or not isinstance(profile, dict)
            ):
                continue
            clean = _empty_profile()
            for field in clean:
                values = profile.get(field, [])
                if isinstance(values, list):
                    clean[field] = [
                        number
                        for x in values[-_MAX_SAMPLES:]
                        if (number := _number(x)) is not None
                        and number < 1e15
                        and (number > 0 or field == "overheads")
                    ]
            result[key] = clean
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _spread(values, minimum=0.2):
    if len(values) < 2:
        return max(minimum, 0.6)
    median = statistics.median(values)
    if median <= 0:
        return minimum
    deviation = statistics.median(abs(value - median) for value in values) / median
    # Intervals describe practical observed variability, not a statistical CI.
    return min(0.9, max(minimum, deviation * 2, 0.5 / math.sqrt(len(values))))


def _confidence(samples, seconds=0, spread=0):
    if spread >= 0.65:
        return "low"
    if samples >= 5 and seconds >= 15:
        return "high" if spread <= 0.35 else "medium"
    return "medium" if samples >= 3 else "low"


def _seconds(amount, rate):
    if rate is None:
        return None
    seconds = amount / rate["speed"]
    spread = rate["spread"]
    return {
        "remaining_seconds": seconds,
        "lower_seconds": seconds / (1 + spread),
        "upper_seconds": seconds / max(0.1, 1 - spread),
        "source": rate["source"],
        "confidence": rate["confidence"],
    }


class _Measurement:
    def __init__(self, key, now, current, *, origin=True):
        self.key = key
        self.started = now
        self.baseline_time = now
        self.baseline_current = current
        self.current = current
        self.last_time = now
        self.units = 0.0
        self.samples = deque(maxlen=12)
        self.ewma = None
        self.origin = origin
        self.committed = False

    def observe(self, now, current):
        if current is None:
            return False
        if self.current is None:
            self.current = self.baseline_current = current
            self.last_time = self.baseline_time = now
            self.origin = current == 0
            return False
        if current < self.current:
            # A restarted file/segment is a new observation, not negative work.
            self.__init__(self.key, now, current, origin=current == 0)
            return False
        delta, elapsed = current - self.current, now - self.last_time
        if delta <= 0 or elapsed <= 0:
            return False
        speed = delta / elapsed
        if not math.isfinite(speed) or speed <= 0:
            return False
        self.samples.append((delta, elapsed))
        self.ewma = speed if self.ewma is None else 0.35 * speed + 0.65 * self.ewma
        self.units += delta
        self.current, self.last_time = current, now
        return True

    def live_rate(self, now):
        if not self.samples or not self.ewma:
            return None
        units = sum(delta for delta, _ in self.samples)
        elapsed = sum(seconds for _, seconds in self.samples)
        speed = 0.6 * self.ewma + 0.4 * units / elapsed
        # A lack of new output lowers the effective rate; an old rate must never
        # count down to zero while its counter has stopped moving.
        since_window = elapsed + max(0, now - self.last_time)
        if since_window > 0:
            speed = min(speed, units / since_window)
        rates = [delta / seconds for delta, seconds in self.samples]
        spread = _spread(rates, 0.2)
        return {
            "speed": speed,
            "source": "live",
            "confidence": _confidence(len(self.samples), elapsed, spread),
            "spread": spread,
            "samples": len(self.samples),
            "seconds": elapsed,
        }


class ProgressEstimator:
    """Estimate active and known future work from actual local measurements.

    ``set_pending`` takes the current task first, followed by future tasks. A
    placeholder such as ``{"stage": "__unknown__"}``, or ``scope_complete=False``,
    prevents an incomplete task inventory from masquerading as a whole-job ETA.
    ``update({"failed": True})`` discards the active observation before a later
    transition. ``phase_complete`` confirms a Buzz phase; ``completed`` confirms
    a whole stage. A normal stage/phase transition also confirms its predecessor.
    """

    def __init__(self, history_path: Path, clock=time.monotonic):
        self.history_path = Path(history_path)
        self._clock = clock
        self._lock = threading.RLock()
        self._profiles = _load_profiles(self.history_path)
        self._started = clock()
        self._paused_at = None
        self._paused_total = 0.0
        self._ended_at = None
        self._terminal = None
        self._stage = None
        self._context = {}
        self._stage_started = self._started
        self._phase_started = self._started
        self._activity = self._started
        self._measurement = None
        self._stage_failed = False
        self._stage_cached = False
        self._stage_resumed = False
        self._stage_committed = False
        self._stage_completed = False
        self._pending = []
        self._scope_complete = False
        self._stopped_percent = None

    def _now(self):
        if self._ended_at is not None:
            return self._ended_at
        raw = self._paused_at if self._paused_at is not None else self._clock()
        return max(self._started, raw - self._paused_total)

    def _record(self, key, **values):
        temporary = None
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(self.history_path) + ".lock", timeout=0.25):
                profiles = _load_profiles(self.history_path)
                # In-memory observations remain usable if a previous disk write
                # failed, but disk state is authoritative for concurrent jobs.
                for old_key, profile in self._profiles.items():
                    profiles.setdefault(old_key, profile)
                profile = profiles.pop(key, _empty_profile())
                for field, value in values.items():
                    number = _number(value)
                    if (
                        field in profile
                        and number is not None
                        and (number > 0 or field == "overheads")
                    ):
                        profile[field] = (profile[field] + [number])[-_MAX_SAMPLES:]
                profiles[key] = profile
                profiles = dict(list(profiles.items())[-_MAX_PROFILES:])
                self._profiles = profiles
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.history_path.parent,
                    prefix=self.history_path.name + ".",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = stream.name
                    json.dump({"version": 1, "profiles": profiles}, stream, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.history_path)
                temporary = None
        except (OSError, ValueError, Timeout):
            # Performance history must never interrupt the operation measured.
            pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def _commit_measurement(self, now):
        measurement = self._measurement
        if not measurement or measurement.committed or self._stage_failed:
            return
        measurement.committed = True
        live = measurement.live_rate(now)
        if live and measurement.units > 0:
            speed, overhead = live["speed"], None
            if self._context.get("phase") in {"infer", "prepare"} and measurement.origin:
                overhead = 0.0
                if measurement.origin and len(measurement.samples) >= 3:
                    later = list(measurement.samples)[1:]
                    steady = sum(x[0] for x in later) / sum(x[1] for x in later)
                    if steady > speed:
                        speed = steady
                        overhead = max(0, now - measurement.started - measurement.units / speed)
            self._record(
                measurement.key,
                rates=speed,
                **({"overheads": overhead} if overhead is not None else {}),
            )

    def _commit_stage(self, now):
        if self._stage_committed or self._stage_failed or not self._stage:
            return
        self._stage_committed = True
        self._commit_measurement(now)
        elapsed = now - self._stage_started
        if elapsed > 0 and not self._stage_cached and not self._stage_resumed:
            self._record(_key(self._stage, self._context, duration=True), durations=elapsed)

    def observe_duration(self, stage, seconds, context=None):
        """Record an externally timed, successfully completed uncached operation."""
        seconds = _positive(seconds)
        if seconds is None or seconds > 604800:
            return
        with self._lock:
            self._record(_key(stage, context or {}, duration=True), durations=seconds)

    def transition(self, stage: str, context: dict | None = None):
        self.update({**(context or {}), "stage": stage})

    def set_pending(self, tasks: list[dict], scope_complete: bool = True):
        with self._lock:
            self._pending = [dict(task) for task in tasks if isinstance(task, dict)][:5000]
            self._scope_complete = bool(scope_complete)

    def update(self, details: dict):
        if not isinstance(details, dict):
            return
        with self._lock:
            if self._terminal:
                return
            now = self._now()
            details = dict(details)
            for field in (
                "current",
                "total",
                "bytes_current",
                "bytes_total",
                "pages_current",
                "pages_total",
                "phase_current_seconds",
                "phase_total_seconds",
                "media_current_seconds",
                "media_total_seconds",
                "phase_elapsed_seconds",
            ):
                if (
                    field in details
                    and details[field] is not None
                    and _number(details[field]) is None
                ):
                    details.pop(field)
            if details.get("failed"):
                self._stage_failed = True
                self._measurement = None
            stage = str(details.get("stage") or self._stage or "")
            if not stage:
                return
            operation_changed = any(
                details.get(field) is not None
                and self._context.get(field) is not None
                and details[field] != self._context[field]
                for field in ("file_id", "item_id", "course_id")
            )
            changed = stage != self._stage or bool(details.get("reset")) or operation_changed
            if changed:
                self._commit_stage(now)
                self._stage = stage
                self._context = {}
                self._stage_started = self._activity = now
                self._stage_failed = bool(details.get("failed"))
                self._stage_cached = self._stage_resumed = False
                self._stage_committed = self._stage_completed = False
                self._measurement = None
            phase_changed = "phase" in details and details["phase"] != self._context.get("phase")
            if phase_changed:
                self._commit_measurement(now)
                self._measurement = None
                self._activity = now
                self._phase_started = now - (_number(details.get("phase_elapsed_seconds")) or 0)
                for field in ("phase_current_seconds", "phase_total_seconds", "phase_complete"):
                    self._context.pop(field, None)
            for field in ("cached", "completed", "failed", "phase_complete", "reset"):
                self._context.pop(field, None)
            self._context.update(details)
            self._activity = now
            current, _, unit = _counter(self._context)
            key = _key(stage, self._context, unit=unit)
            cached = bool(details.get("cached"))
            if cached:
                self._stage_cached = True
                # Cache offsets are already completed work, not extraordinary
                # processing speed; the next real sample starts a new baseline.
                self._measurement = _Measurement(key, now, current, origin=False)
                self._activity = now
            elif not self._stage_failed:
                if self._measurement is None or self._measurement.key != key:
                    self._measurement = _Measurement(key, now, current, origin=current == 0)
                    if current is not None and current > 0:
                        self._stage_resumed = True
                else:
                    if (
                        current is not None
                        and self._measurement.current is not None
                        and current < self._measurement.current
                    ):
                        self._stage_resumed = True
                    self._measurement.observe(now, current)
                if details.get("phase_complete"):
                    self._commit_measurement(now)
                if details.get("completed"):
                    self._stage_completed = True
                    self._commit_stage(now)

    def pause(self, paused: bool):
        with self._lock:
            if self._terminal:
                return
            if paused and self._paused_at is None:
                self._paused_at = self._clock()
            elif not paused and self._paused_at is not None:
                self._paused_total += max(0, self._clock() - self._paused_at)
                self._paused_at = None
                # Human wait is excluded, and the first later progress sample
                # uses the current counter as its baseline.
                if self._measurement:
                    self._measurement = _Measurement(
                        self._measurement.key,
                        self._now(),
                        self._measurement.current,
                        origin=False,
                    )
                self._activity = self._now()

    def finish(self, success: bool):
        with self._lock:
            if self._terminal:
                return
            now = self._now()
            if success:
                self._commit_stage(now)
            else:
                self._stopped_percent = self.snapshot()["overall"]["percent"]
            self._ended_at = now
            self._terminal = "complete" if success else "stopped"

    def _history_rate(self, key):
        rates = self._profiles.get(key, {}).get("rates", [])
        if not rates:
            return None
        spread = _spread(rates)
        return {
            "speed": statistics.median(rates),
            "source": "history",
            "confidence": _confidence(len(rates), 15 if len(rates) >= 5 else 0, spread),
            "spread": spread,
            "samples": len(rates),
            "seconds": 0,
        }

    def _rate(self, key, now, *, active=False):
        history = self._history_rate(key)
        live = (
            self._measurement.live_rate(now)
            if active and self._measurement and self._measurement.key == key
            else None
        )
        if not live:
            return history
        if not history:
            return live
        weight = min(0.95, 0.25 + live["seconds"] / 40 + live["samples"] * 0.1)
        return {
            **live,
            "speed": 1 / (weight / live["speed"] + (1 - weight) / history["speed"]),
            "source": "mixed",
            "spread": max(
                live["spread"],
                history["spread"],
                abs(live["speed"] - history["speed"]) / max(live["speed"], history["speed"]),
            ),
        }

    def _duration_prediction(self, stage, context, elapsed):
        values = self._profiles.get(_key(stage, context, duration=True), {}).get("durations", [])
        if not values:
            return None
        typical = statistics.median(values)
        if elapsed >= typical:
            # Once a historical prediction is overdue there is no defensible
            # countdown; wait for new observations instead of displaying 0 s.
            return None
        spread = _spread(values)
        return {
            "remaining_seconds": typical - elapsed,
            "lower_seconds": max(0, typical * (1 - spread) - elapsed),
            "upper_seconds": max(typical - elapsed, typical * (1 + spread) - elapsed),
            "source": "history",
            "confidence": _confidence(len(values), 15 if len(values) >= 5 else 0, spread),
        }

    def _buzz_prediction(self, context, now, *, active=False):
        total = _number(context.get("media_total_seconds"))
        current = _number(context.get("media_current_seconds"))
        if total is None or current is None or current > total:
            return None
        remaining = total - current
        if remaining == 0:
            return _seconds(0, {"speed": 1, "source": "live", "confidence": "high", "spread": 0})
        phase = context.get("phase")
        phase_current = _number(context.get("phase_current_seconds")) or 0
        phase_total = _number(context.get("phase_total_seconds"))
        phase_remaining = max(0, (phase_total or 0) - phase_current)
        segment_start = _number(context.get("segment_start_seconds"))
        if segment_start is None:
            segment_start = max(0, current - phase_current) if phase == "infer" else current
        segment_duration = _number(context.get("segment_seconds")) or phase_total
        future_audio = remaining
        if phase in {"prepare", "infer"} and segment_duration is not None:
            future_audio = max(0, total - segment_start - segment_duration)
        segment_total = _number(context.get("segment_total"))
        segment_index = _number(context.get("segment_index"))
        future_segments = None
        if segment_total is not None and segment_index is not None:
            future_segments = max(
                0,
                segment_total
                - segment_index
                - (1 if phase in {"prepare", "infer", "complete"} else 0),
            )
        if future_segments is None and segment_duration:
            future_segments = math.ceil(future_audio / segment_duration)
        components = []
        for target in ("prepare", "infer"):
            key = _key("transcribe", context, unit="media_seconds", phase=target)
            rate = self._rate(key, now, active=active and phase == target)
            history = self._history_rate(key)
            active_amount = phase_remaining if phase == target else 0
            future_amount = future_audio
            if phase == "prepare" and target == "infer":
                future_amount += phase_total or segment_duration or 0
            elif phase not in {"prepare", "infer"}:
                future_amount = remaining
            for amount, selected_rate in ((active_amount, rate), (future_amount, history or rate)):
                if amount > 0:
                    component = _seconds(amount, selected_rate)
                    if component is None:
                        return None
                    components.append(component)
            overheads = self._profiles.get(key, {}).get("overheads", [])
            if overheads:
                overhead = statistics.median(overheads)
                count = future_segments or 0
                if phase == "prepare" and target == "infer":
                    count += 1
                seconds = overhead * count
                if phase == target and phase_current == 0:
                    # Startup can count down only its calibrated fixed cost;
                    # actual unprocessed audio remains until output confirms it.
                    seconds += max(0, overhead - max(0, now - self._phase_started))
                if seconds:
                    spread = _spread(overheads)
                    components.append(
                        {
                            "remaining_seconds": seconds,
                            "lower_seconds": seconds * (1 - spread),
                            "upper_seconds": seconds * (1 + spread),
                            "source": "history",
                            "confidence": "low",
                        }
                    )
        return self._sum_predictions(components)

    @staticmethod
    def _sum_predictions(predictions):
        if not predictions:
            return {
                "remaining_seconds": 0.0,
                "lower_seconds": 0.0,
                "upper_seconds": 0.0,
                "source": None,
                "confidence": "high",
            }
        sources = {x["source"] for x in predictions if x["source"]}
        confidences = {x["confidence"] for x in predictions}
        return {
            **{
                field: sum(x[field] for x in predictions)
                for field in (
                    "remaining_seconds",
                    "lower_seconds",
                    "upper_seconds",
                )
            },
            "source": "mixed" if len(sources) > 1 else next(iter(sources), None),
            "confidence": "low"
            if "low" in confidences
            else ("medium" if "medium" in confidences else "high"),
        }

    def _prediction(self, stage, context, now, *, active=False):
        if stage == "__unknown__" or context.get("unknown"):
            return None
        if stage == "transcribe" and "media_total_seconds" in context:
            return self._buzz_prediction(context, now, active=active)
        if context.get("cached") or context.get("completed"):
            return self._sum_predictions([])
        current, total, unit = _counter(context)
        if total is not None and current is not None and current <= total:
            if total == current:
                return self._sum_predictions([])
            key = _key(stage, context, unit=unit)
            rate = self._rate(key, now, active=active)
            return _seconds(total - current, rate)
        elapsed = max(0, now - self._stage_started) if active else 0
        return self._duration_prediction(stage, context, elapsed)

    def snapshot(self) -> dict:
        with self._lock:
            now = self._now()
            elapsed = max(0, now - self._stage_started) if self._stage else 0
            current, total, unit = _counter(self._context)
            rate = (
                self._rate(
                    _key(self._stage, self._context, unit=unit),
                    now,
                    active=True,
                )
                if self._stage
                else None
            )
            prediction = (
                self._prediction(
                    self._stage,
                    self._context,
                    now,
                    active=True,
                )
                if self._stage and not self._stage_failed
                else None
            )
            state = "estimating" if prediction is not None else "calibrating"
            reason = "measured_rate" if prediction else "awaiting_measurements"
            if rate is not None:
                state = "estimating"
                if prediction is None:
                    reason = "remaining_phases_uncalibrated"
            stale_after = _positive(self._context.get("stale_after_seconds")) or 45
            if self._measurement and self._measurement.samples:
                cadence = statistics.median(x[1] for x in self._measurement.samples)
                stale_after = max(stale_after, cadence * 4)
                if now - self._activity > stale_after and not self._stage_completed:
                    prediction = None
                    state, reason = "stale", "progress_not_updated"
            elif (
                rate
                and current is not None
                and total is not None
                and current < total
                and now - self._activity > stale_after
                and not self._stage_completed
            ):
                prediction = None
                state, reason = "stale", "progress_not_updated"
            if self._terminal:
                state, reason = self._terminal, self._terminal
                prediction = self._sum_predictions([]) if state == "complete" else None
            elif self._paused_at is not None:
                state, reason = "paused", "waiting_for_resume"

            stage_result = {
                "remaining_seconds": None,
                "lower_seconds": None,
                "upper_seconds": None,
                "speed": rate["speed"] if rate else None,
                "unit": unit,
                "source": rate["source"] if rate else None,
                "confidence": rate["confidence"] if rate else "low",
                "elapsed_seconds": elapsed,
                "phase": self._context.get("phase"),
                "phase_remaining_seconds": None,
            }
            if prediction is not None:
                stage_result.update(prediction)
            if total is not None and current is not None and current <= total and rate:
                phase = _seconds(total - current, rate)
                stage_result["phase_remaining_seconds"] = phase["remaining_seconds"]
            if state in {"stale", "stopped"}:
                stage_result["phase_remaining_seconds"] = None

            unknown = 0 if self._scope_complete else 1
            components = []
            if self._stage:
                if prediction is None:
                    unknown += 1
                else:
                    components.append(prediction)
            for index, task in enumerate(self._pending):
                if index == 0 and task.get("stage") == self._stage:
                    continue
                future = self._prediction(str(task.get("stage") or "__unknown__"), task, now)
                if future is None:
                    unknown += 1
                else:
                    components.append(future)
            known_work = self._sum_predictions(components) if components else None
            overall = known_work if unknown == 0 else None
            active_elapsed = max(0, now - self._started)
            percent = None
            if overall is not None and active_elapsed + overall["remaining_seconds"] > 0:
                percent = min(
                    99.9, 100 * active_elapsed / (active_elapsed + overall["remaining_seconds"])
                )
            if self._terminal == "complete":
                overall, percent, unknown = self._sum_predictions([]), 100.0, 0
            elif self._terminal == "stopped":
                overall, percent = None, self._stopped_percent
            overall_result = {
                "remaining_seconds": None,
                "lower_seconds": None,
                "upper_seconds": None,
                "percent": percent,
                "percent_estimated": self._terminal != "complete",
                "scope_complete": self._scope_complete or self._terminal == "complete",
                "unknown_tasks": unknown,
                "known_remaining_seconds": None,
                "known_lower_seconds": None,
                "known_upper_seconds": None,
                "known_task_count": len(components),
            }
            if known_work and state == "estimating":
                overall_result.update(
                    known_remaining_seconds=known_work["remaining_seconds"],
                    known_lower_seconds=known_work["lower_seconds"],
                    known_upper_seconds=known_work["upper_seconds"],
                )
            if overall is not None:
                overall_result.update(
                    {
                        field: overall[field]
                        for field in (
                            "remaining_seconds",
                            "lower_seconds",
                            "upper_seconds",
                        )
                    }
                )
            return {
                "observed_at": datetime.now(UTC).isoformat(),
                "state": state,
                "stage": stage_result,
                "overall": overall_result,
                "reason": reason,
            }
