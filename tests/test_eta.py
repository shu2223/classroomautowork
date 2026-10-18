import json
import math

import pytest

from classroomautowork.eta import ProgressEstimator


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def timing(tmp_path):
    clock = Clock()
    return ProgressEstimator(tmp_path / "performance.json", clock=clock), clock


def update(estimator, stage="download", current=0, total=100, **extra):
    estimator.update({"stage": stage, "current": current, "total": total, "unit": "bytes", **extra})


def train_rate(path, *, stage="download", seconds=10, total=100, **context):
    clock = Clock()
    estimator = ProgressEstimator(path, clock=clock)
    update(estimator, stage=stage, total=total, **context)
    clock.advance(seconds)
    update(estimator, stage=stage, current=total, total=total, **context)
    estimator.finish(True)


def test_cold_start_has_no_fictional_duration_or_speed(timing):
    estimator, _ = timing
    estimator.transition("ai", {"model": "gpt", "effort": "high"})
    result = estimator.snapshot()
    assert result["state"] == "calibrating"
    assert result["stage"]["remaining_seconds"] is None
    assert result["stage"]["speed"] is None
    assert result["overall"]["remaining_seconds"] is None
    assert result["overall"]["percent"] is None


def test_measured_rate_and_complete_inventory_predict_overall(timing):
    estimator, clock = timing
    update(estimator)
    estimator.set_pending([{"stage": "download"}], scope_complete=True)
    clock.advance(2)
    update(estimator, current=20)
    result = estimator.snapshot()
    assert result["stage"]["speed"] == pytest.approx(10)
    assert result["stage"]["remaining_seconds"] == pytest.approx(8)
    assert result["stage"]["lower_seconds"] < 8 < result["stage"]["upper_seconds"]
    assert result["overall"]["remaining_seconds"] == pytest.approx(8)
    assert result["overall"]["percent"] == pytest.approx(20)
    assert result["overall"]["percent_estimated"] is True


def test_unknown_work_hides_overall_but_keeps_active_estimate(timing):
    estimator, clock = timing
    update(estimator)
    clock.advance(2)
    update(estimator, current=20)
    estimator.set_pending([{"stage": "download"}, {"stage": "__unknown__"}])
    result = estimator.snapshot()
    assert result["state"] == "estimating"
    assert result["stage"]["remaining_seconds"] == pytest.approx(8)
    assert result["overall"]["remaining_seconds"] is None
    assert result["overall"]["unknown_tasks"] == 1
    assert result["overall"]["known_remaining_seconds"] == pytest.approx(8)
    assert result["overall"]["known_lower_seconds"] < 8
    assert result["overall"]["known_upper_seconds"] > 8
    assert result["overall"]["known_task_count"] == 1
    assert result["overall"]["percent"] is None


def test_stall_never_counts_down_to_zero_and_eventually_is_stale(timing):
    estimator, clock = timing
    update(estimator)
    clock.advance(2)
    update(estimator, current=20)
    before = estimator.snapshot()["stage"]["remaining_seconds"]
    clock.advance(10)
    assert estimator.snapshot()["stage"]["remaining_seconds"] >= before
    clock.advance(50)
    result = estimator.snapshot()
    assert result["state"] == "stale"
    assert result["stage"]["remaining_seconds"] is None
    assert result["stage"]["phase_remaining_seconds"] is None
    assert result["overall"]["remaining_seconds"] is None
    assert result["overall"]["known_remaining_seconds"] is None


def test_reading_snapshot_never_persists_or_refreshes_activity(timing):
    estimator, clock = timing
    update(estimator)
    clock.advance(2)
    update(estimator, current=20)
    for _ in range(3):
        clock.advance(20)
        estimator.snapshot()
    assert estimator.snapshot()["state"] == "stale"
    assert not estimator.history_path.exists()


def test_zero_progress_never_teaches_a_rate(timing):
    estimator, clock = timing
    update(estimator)
    clock.advance(30)
    update(estimator)
    assert estimator.snapshot()["stage"]["speed"] is None
    estimator.finish(False)
    assert not estimator.history_path.exists()


def test_resumed_offset_and_cached_offsets_do_not_inflate_speed(timing):
    estimator, clock = timing
    update(estimator, current=800, total=1000)
    clock.advance(10)
    update(estimator, current=900, total=1000)
    assert estimator.snapshot()["stage"]["speed"] == pytest.approx(10)
    clock.advance(1)
    update(estimator, current=950, total=1000, cached=True)
    clock.advance(5)
    update(estimator, current=975, total=1000)
    result = estimator.snapshot()
    assert result["stage"]["speed"] == pytest.approx(5)
    assert result["stage"]["remaining_seconds"] == pytest.approx(5)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True, "100"])
def test_invalid_numeric_updates_are_rejected(timing, invalid):
    estimator, clock = timing
    update(estimator)
    clock.advance(2)
    update(estimator, current=20)
    update(estimator, current=invalid)
    result = estimator.snapshot()
    assert result["stage"]["speed"] == pytest.approx(10)
    assert result["stage"]["remaining_seconds"] == pytest.approx(8)
    assert "NaN" not in json.dumps(result, allow_nan=False)


def test_counter_reset_recalibrates_instead_of_learning_negative_speed(timing):
    estimator, clock = timing
    update(estimator)
    clock.advance(2)
    update(estimator, current=20)
    clock.advance(1)
    update(estimator, current=0)
    assert estimator.snapshot()["stage"]["speed"] is None
    clock.advance(4)
    update(estimator, current=20)
    assert estimator.snapshot()["stage"]["speed"] == pytest.approx(5)


def test_pause_excludes_human_wait_and_resets_rate_baseline(timing):
    estimator, clock = timing
    update(estimator)
    clock.advance(2)
    update(estimator, current=20)
    estimator.pause(True)
    paused = estimator.snapshot()
    clock.advance(3600)
    after = estimator.snapshot()
    assert after["state"] == "paused"
    assert after["stage"] == paused["stage"]
    estimator.pause(False)
    clock.advance(2)
    update(estimator, current=40)
    result = estimator.snapshot()
    assert result["stage"]["elapsed_seconds"] == pytest.approx(4)
    assert result["stage"]["speed"] == pytest.approx(10)


def test_successful_rates_survive_restart_and_are_private(timing):
    estimator, clock = timing
    update(estimator, file_id="private-lesson-title.mp4")
    clock.advance(10)
    update(estimator, current=100, file_id="private-lesson-title.mp4")
    estimator.finish(True)
    serialized = estimator.history_path.read_text(encoding="utf-8")
    assert "private-lesson" not in serialized
    assert not list(estimator.history_path.parent.glob("*.tmp"))
    next_estimator = ProgressEstimator(estimator.history_path, clock=clock)
    update(next_estimator, current=50)
    result = next_estimator.snapshot()
    assert result["stage"]["source"] == "history"
    assert result["stage"]["remaining_seconds"] == pytest.approx(5)


def test_failed_operation_never_teaches_a_rate_or_duration(timing):
    estimator, clock = timing
    update(estimator)
    clock.advance(10)
    update(estimator, current=50)
    estimator.update({"failed": True})
    estimator.transition("validate")
    result = ProgressEstimator(estimator.history_path, clock=clock)
    update(result)
    assert result.snapshot()["stage"]["speed"] is None
    assert not estimator.history_path.exists()


def test_failed_finish_is_stopped_and_never_100_percent(timing):
    estimator, clock = timing
    update(estimator)
    estimator.set_pending([{"stage": "download"}])
    clock.advance(2)
    update(estimator, current=20)
    estimator.finish(False)
    result = estimator.snapshot()
    assert result["state"] == "stopped"
    assert result["overall"]["remaining_seconds"] is None
    assert result["overall"]["percent"] == pytest.approx(20)
    clock.advance(1000)
    assert estimator.snapshot()["stage"]["elapsed_seconds"] == pytest.approx(2)
    assert not estimator.history_path.exists()


def test_fixed_stage_duration_is_learned_only_on_success_and_isolated(timing):
    estimator, clock = timing
    estimator.transition("ai", {"model": "model-a", "effort": "high"})
    clock.advance(20)
    estimator.transition("validate")
    reader = ProgressEstimator(estimator.history_path, clock=clock)
    reader.transition("ai", {"model": "model-a", "effort": "high"})
    assert reader.snapshot()["stage"]["remaining_seconds"] == pytest.approx(20)
    clock.advance(21)
    overdue = reader.snapshot()
    assert overdue["state"] == "calibrating"
    assert overdue["stage"]["remaining_seconds"] is None
    other = ProgressEstimator(estimator.history_path, clock=clock)
    other.transition("ai", {"model": "model-b", "effort": "high"})
    assert other.snapshot()["stage"]["remaining_seconds"] is None
    other.transition("ai", {"model": "model-a", "effort": "low"})
    assert other.snapshot()["stage"]["remaining_seconds"] is None


def test_explicit_duration_and_multistage_pending_sum(timing):
    estimator, clock = timing
    estimator.observe_duration("ai", 30, {"model": "m", "effort": "high"})
    estimator.observe_duration("fill", 10)
    update(estimator)
    estimator.set_pending(
        [
            {"stage": "download"},
            {"stage": "ai", "model": "m", "effort": "high"},
            {"stage": "fill"},
        ]
    )
    clock.advance(2)
    update(estimator, current=20)
    result = estimator.snapshot()
    assert result["overall"]["remaining_seconds"] == pytest.approx(48)
    assert result["overall"]["unknown_tasks"] == 0


def test_same_stage_new_file_commits_separate_duration(timing):
    estimator, clock = timing
    estimator.transition("extract", {"file_id": "one"})
    clock.advance(10)
    estimator.transition("extract", {"file_id": "two"})
    assert estimator.snapshot()["stage"]["elapsed_seconds"] == 0
    clock.advance(20)
    estimator.finish(True)
    reader = ProgressEstimator(estimator.history_path, clock=clock)
    reader.transition("extract")
    assert reader.snapshot()["stage"]["remaining_seconds"] == pytest.approx(15)


def buzz_event(estimator, phase, current, total=100, **extra):
    estimator.update(
        {
            "stage": "transcribe",
            "phase": phase,
            "phase_current_seconds": current,
            "phase_total_seconds": total,
            "media_current_seconds": current if phase == "infer" else 0,
            "media_total_seconds": 200,
            "segment_start_seconds": 0,
            "segment_seconds": 100,
            "segment_index": 0,
            "segment_total": 2,
            "model": "large",
            "engine": "engine1",
            "language": "ja",
            **extra,
        }
    )


def test_buzz_combines_live_inference_and_learned_prepare_overhead(timing):
    estimator, clock = timing
    buzz_event(estimator, "prepare", 0)
    clock.advance(2)
    buzz_event(estimator, "prepare", 100, phase_complete=True)
    buzz_event(estimator, "infer", 0)
    clock.advance(10)
    buzz_event(estimator, "infer", 50)
    result = estimator.snapshot()
    # Current infer: 50 / 5; next infer: 100 / 5; next prepare: 100 / 50.
    assert result["stage"]["remaining_seconds"] == pytest.approx(32)
    assert result["stage"]["phase_remaining_seconds"] == pytest.approx(10)
    assert result["stage"]["source"] == "mixed"


def test_buzz_first_prepare_keeps_phase_rate_while_inference_unknown(timing):
    estimator, clock = timing
    buzz_event(estimator, "prepare", 0)
    clock.advance(2)
    buzz_event(estimator, "prepare", 50)
    result = estimator.snapshot()
    assert result["state"] == "estimating"
    assert result["stage"]["remaining_seconds"] is None
    assert result["stage"]["phase_remaining_seconds"] == pytest.approx(2)
    assert result["stage"]["speed"] == pytest.approx(25)


def test_buzz_engine_model_and_language_profiles_do_not_cross(timing):
    estimator, clock = timing
    buzz_event(estimator, "prepare", 0)
    clock.advance(2)
    buzz_event(estimator, "prepare", 100, phase_complete=True)
    buzz_event(estimator, "infer", 0)
    clock.advance(10)
    buzz_event(estimator, "infer", 100, phase_complete=True)
    estimator.finish(True)
    for field, value in (("engine", "engine2"), ("model", "small"), ("language", "en")):
        reader = ProgressEstimator(estimator.history_path, clock=clock)
        buzz_event(reader, "infer", 0, **{field: value})
        assert reader.snapshot()["stage"]["speed"] is None


def test_buzz_short_audio_tail_does_not_count_as_future_audio(timing):
    estimator, clock = timing
    buzz_event(estimator, "prepare", 0)
    clock.advance(2)
    buzz_event(estimator, "prepare", 50, total=50, phase_complete=True)
    buzz_event(estimator, "infer", 0, total=50)
    clock.advance(5)
    buzz_event(estimator, "infer", 25, total=50)
    # The current video's 50-second silent tail incurs no inference cost.
    assert estimator.snapshot()["stage"]["remaining_seconds"] == pytest.approx(29)


def test_history_corruption_and_nonfinite_values_do_not_break_estimation(tmp_path):
    path = tmp_path / "history.json"
    path.write_text('{"version":1,"profiles":{"' + "a" * 64 + '":{"rates":[NaN]}}}')
    clock = Clock()
    estimator = ProgressEstimator(path, clock=clock)
    update(estimator)
    clock.advance(1)
    update(estimator, current=10)
    assert math.isfinite(estimator.snapshot()["stage"]["speed"])
    path.write_text("broken", encoding="utf-8")
    estimator.finish(True)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_history_is_bounded_and_atomic(tmp_path):
    path = tmp_path / "history.json"
    estimator = ProgressEstimator(path, clock=Clock())
    for number in range(30):
        estimator.observe_duration("ai", number + 1)
    profile = next(iter(json.loads(path.read_text())["profiles"].values()))
    assert len(profile["durations"]) == 24
    assert profile["durations"][0] == 7
    assert not list(tmp_path.glob("*.tmp"))


def test_live_window_recovers_after_an_initial_slow_period(timing):
    estimator, clock = timing
    update(estimator, total=1000)
    clock.advance(100)
    update(estimator, current=10, total=1000)
    for number in range(1, 16):
        clock.advance(1)
        update(estimator, current=10 + number * 10, total=1000)
    # The old 100-second startup is no longer a cap on a recovered live speed.
    assert estimator.snapshot()["stage"]["speed"] > 9


def test_slow_live_measurements_outweigh_a_fast_historical_rate(timing):
    estimator, clock = timing
    train_rate(estimator.history_path, total=1000, seconds=10)
    reader = ProgressEstimator(estimator.history_path, clock=clock)
    update(reader, total=1000)
    clock.advance(10)
    update(reader, current=10, total=1000)
    assert reader.snapshot()["stage"]["speed"] < 2


def test_buzz_learns_startup_cost_for_every_remaining_segment(timing):
    estimator, clock = timing
    buzz_event(estimator, "prepare", 0)
    clock.advance(2)
    buzz_event(estimator, "prepare", 100, phase_complete=True)
    buzz_event(estimator, "infer", 0)
    clock.advance(20)
    buzz_event(estimator, "infer", 25)
    for current in (50, 75, 100):
        clock.advance(5)
        buzz_event(estimator, "infer", current, phase_complete=current == 100)
    estimator.finish(True)
    reader = ProgressEstimator(estimator.history_path, clock=clock)
    buzz_event(reader, "infer", 0, media_total_seconds=300, segment_total=3)
    # 300 / 5 infer + 200 / 50 prepare + 3 * 15 startup.
    assert reader.snapshot()["stage"]["remaining_seconds"] == pytest.approx(109)
    clock.advance(10)
    assert reader.snapshot()["stage"]["remaining_seconds"] == pytest.approx(99)


def test_variable_duration_history_has_low_confidence(timing):
    estimator, _ = timing
    for duration in (1, 2, 100, 1000, 2000):
        estimator.observe_duration("attachment_total_media", duration)
    estimator.transition("attachment_total_media")
    assert estimator.snapshot()["stage"]["confidence"] == "low"
