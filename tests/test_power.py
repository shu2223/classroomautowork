"""Shutdown lifecycle fixtures: every OS action and timer is an offline fake."""

from copy import deepcopy

import pytest

from classroomautowork import power
from classroomautowork.errors import WorkflowError
from classroomautowork.power import ShutdownController


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.started = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Deliberately allow stale callbacks to emulate a cancellation race.
        self.callback()


@pytest.fixture
def controller():
    timers, calls = [], []
    now = [100.0]

    def timer_factory(delay, callback):
        timer = FakeTimer(delay, callback)
        timers.append(timer)
        return timer

    result = ShutdownController(
        executor=lambda: calls.append("shutdown"),
        clock=lambda: now[0],
        wall_clock=lambda: 0,
        timer_factory=timer_factory,
        platform="win32",
    )
    return result, now, timers, calls


def job(identifier="batch", status="running", kind="review", **options):
    return {
        "id": identifier,
        "kind": kind,
        "status": status,
        "items": [{"status": "ready"}, {"status": "document_ready"}],
        **options,
    }


def finish(controller):
    controller.arm("batch")
    controller.observe(job())
    return controller.observe(job(status="completed"))


def test_default_is_off_and_history_or_reads_never_arm(controller):
    value, now, timers, calls = controller
    assert value.snapshot()["state"] == "off"
    assert not value.snapshot()["enabled"]
    value.observe(job(status="completed"))
    now[0] += 10000
    for _ in range(3):
        value.snapshot()
    assert timers == calls == []
    value.arm()
    assert value.observe(job(status="completed"))["state"] == "waiting"
    assert not timers


def test_idle_waits_for_assignment_batch_not_oauth_or_refresh(controller):
    value, _, timers, _ = controller
    value.arm()
    for kind in ("refresh", "document_auth"):
        assert value.observe(job(kind=kind))["state"] == "waiting"
        assert value.observe(job(kind=kind, status="completed"))["state"] == "waiting"
    assert value.observe(job(kind="fill"))["state"] == "armed"
    state = value.observe(job(kind="fill", status="completed"))
    assert state["state"] == "countdown"
    assert state["remaining_seconds"] == 60
    assert state["deadline_at"] == "1970-01-01T00:01:00Z"
    assert len(timers) == 1 and timers[0].started and timers[0].daemon


def test_whole_batch_completion_starts_once_and_reads_do_not_execute(controller):
    value, now, timers, calls = controller
    value.arm("batch")
    state = value.observe(job(items=[{"status": "ready"}, {"status": "drafting"}]))
    assert state["state"] == "armed" and not timers
    state = value.observe(job(status="completed"))
    assert state["remaining_seconds"] == 60
    now[0] += 20.2
    assert value.snapshot()["remaining_seconds"] == 40
    assert value.observe(job(status="completed"))["remaining_seconds"] == 40
    assert value.arm("batch")["remaining_seconds"] == 40
    assert len(timers) == 1
    now[0] += 40
    assert value.snapshot()["remaining_seconds"] == 0 and calls == []
    timers[0].fire()
    assert calls == ["shutdown"]
    assert value.snapshot()["state"] == "shutting_down"
    timers[0].fire()
    value.observe(job(status="completed"))
    assert calls == ["shutdown"]


def test_early_timer_rechecks_monotonic_deadline(controller):
    value, now, timers, calls = controller
    finish(value)
    now[0] += 20
    timers[0].fire()
    assert len(timers) == 2 and timers[1].delay == 40 and calls == []
    now[0] += 40
    timers[1].fire()
    assert calls == ["shutdown"]


@pytest.mark.parametrize("action", ["disable", "cancel", "close"])
def test_cancel_close_or_switch_off_invalidates_even_stale_timer(controller, action):
    value, now, timers, calls = controller
    finish(value)
    getattr(value, action)()
    assert timers[0].cancelled and not value.snapshot()["enabled"]
    now[0] += 60
    timers[0].fire()
    assert calls == []
    if action == "close":
        with pytest.raises(WorkflowError, match="关闭"):
            value.arm()


@pytest.mark.parametrize(
    "status", ["stopping", "paused", "failed", "interrupted", "completed_with_issues"]
)
def test_unsuccessful_bound_job_turns_switch_off(controller, status):
    value, _, timers, _ = controller
    value.arm("batch")
    value.observe(job())
    state = value.observe(job(status=status))
    assert state["state"] == "cancelled" and not state["enabled"]
    assert "取消" in state["message"] and not timers


@pytest.mark.parametrize(
    "items",
    [
        [],
        None,
        [{"status": "materials_ready"}],
        [{"status": "ready"}, {"status": "needs_document"}],
        [{"status": "ready", "requires_regeneration": True}],
        [{"status": "document_ready", "document_fill": {"status": "partial"}}],
        [{"status": "document_ready", "document_fill": {"remaining_required_fields": ["q2"]}}],
        [{"status": "document_ready", "document_fill": {"unrecognized_questions": ["q4"]}}],
        [{"status": "document_ready", "document_fill": "invalid"}],
        [
            {
                "status": "form_opened",
                "form_fill": {"forms": [{"remaining_fields": [{"title": "Q1", "required": True}]}]},
            }
        ],
        [
            {
                "status": "form_opened",
                "form_fill": {
                    "forms": [{"remaining_answer_fields": [{"title": "Q1", "required": True}]}]
                },
            }
        ],
        [
            {
                "status": "form_opened",
                "form_fill": {
                    "forms": [{"user_input_fields": [{"title": "学年", "required": True}]}]
                },
            }
        ],
        [{"status": "form_opened", "generation": {"form_mapping_complete": False}}],
        ["invalid"],
    ],
)
def test_empty_materials_partial_or_unanswered_work_cannot_shutdown(controller, items):
    value, _, timers, _ = controller
    value.arm("batch")
    value.observe(job())
    state = value.observe(job(status="completed", items=items))
    assert not state["enabled"] and not timers


def test_old_completed_bound_id_without_observed_activity_cannot_shutdown(controller):
    value, _, timers, _ = controller
    value.arm("batch")
    assert not value.observe(job(status="completed"))["enabled"]
    assert not timers


@pytest.mark.parametrize("phase", ["armed", "countdown"])
def test_new_active_batch_cancels_binding_and_does_not_rearm(controller, phase):
    value, now, timers, calls = controller
    value.arm("batch")
    value.observe(job())
    if phase == "countdown":
        value.observe(job(status="completed"))
    state = value.observe(job(identifier="new-batch"))
    assert not state["enabled"] and state["state"] == "cancelled"
    value.observe(job(identifier="new-batch", status="completed"))
    now[0] += 60
    for timer in timers:
        timer.fire()
    assert calls == []


def test_restarted_bound_job_cancels_countdown(controller):
    value, now, timers, calls = controller
    finish(value)
    assert not value.observe(job())["enabled"]
    now[0] += 60
    timers[0].fire()
    assert calls == []


def test_disable_and_rearm_cannot_execute_previous_countdown(controller):
    value, now, timers, calls = controller
    finish(value)
    value.disable()
    now[0] += 20
    finish(value)
    now[0] += 40
    timers[0].fire()
    assert calls == [] and value.snapshot()["remaining_seconds"] == 20
    now[0] += 20
    timers[1].fire()
    assert calls == ["shutdown"]


def test_preflight_blocks_unobserved_new_work_or_lookup_failure(controller):
    value, now, timers, calls = controller
    seen = []
    value._preflight = lambda identifier: seen.append(identifier) or False
    finish(value)
    now[0] += 60
    timers[0].fire()
    assert seen == ["batch"] and calls == []
    assert value.snapshot()["state"] == "cancelled"

    def broken(identifier):
        raise RuntimeError("private worker data")

    value._preflight = broken
    finish(value)
    now[0] += 60
    timers[1].fire()
    assert calls == [] and "private" not in str(value.snapshot())


def test_os_failure_is_sanitized_and_not_retried(controller):
    value, now, timers, _ = controller
    calls = []

    def failed():
        calls.append(1)
        raise RuntimeError("sensitive local directory and command output")

    value._executor = failed
    finish(value)
    now[0] += 60
    timers[0].fire()
    state = value.snapshot()
    assert state["state"] == "failed" and not state["enabled"]
    assert "sensitive" not in str(state)
    timers[0].fire()
    assert calls == [1]


def test_job_input_and_returned_snapshot_are_not_mutated(controller):
    value, _, _, _ = controller
    original = job()
    expected = deepcopy(original)
    value.arm()
    state = value.observe(original)
    state["enabled"] = False
    assert original == expected and value.snapshot()["enabled"]


def test_other_platform_never_arms():
    value = ShutdownController(platform="linux", executor=lambda: pytest.fail("OS action"))
    assert not value.snapshot()["supported"]
    with pytest.raises(WorkflowError, match="Windows"):
        value.arm()
    assert not value.snapshot()["enabled"]


def test_windows_command_has_no_forced_close_or_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(
        power.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    power.shutdown_windows()
    args, kwargs = calls[0]
    assert args == (["shutdown.exe", "/s", "/t", "0"],)
    assert kwargs["shell"] is False and kwargs["check"] is True
    assert kwargs["capture_output"] is True and kwargs["timeout"] == 10
    assert "/f" not in args[0]
