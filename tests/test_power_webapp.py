"""Offline shutdown HTTP integration: timers and operating system calls are fakes."""

import json
import threading
from contextlib import contextmanager

import pytest
from test_frontend import request, unit_settings
from test_power import FakeTimer, job

from classroomautowork.power import ShutdownController
from classroomautowork.power_service import PowerWatch, WorkerSource, create_companion
from classroomautowork.ui_jobs import FrontendJobs
from classroomautowork.webapp import LocalServer


@pytest.fixture
def fake_power():
    timers, calls = [], []
    now = [100.0]

    def timer_factory(delay, callback):
        timer = FakeTimer(delay, callback)
        timers.append(timer)
        return timer

    controller = ShutdownController(
        executor=lambda: calls.append("shutdown"),
        platform="win32",
        clock=lambda: now[0],
        wall_clock=lambda: 0,
        timer_factory=timer_factory,
    )
    yield controller, now, timers, calls
    controller.close()


@contextmanager
def running(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.fixture(params=["native", "companion"])
def power_server(tmp_path, fake_power, request):
    jobs = FrontendJobs(unit_settings(tmp_path))
    if request.param == "native":
        with running(LocalServer(jobs, power=fake_power[0])) as server:
            yield server
    else:
        with running(LocalServer(jobs)) as upstream:
            source = {"port": upstream.server_port, "token": upstream.token}
            with running(create_companion(source, power=fake_power[0])) as server:
                yield server


def json_request(server, path, **options):
    status, _, body = request(server, path, **options)
    return status, json.loads(body)


def test_shutdown_snapshot_is_off_and_bootstrap_includes_same_state(power_server, fake_power):
    server = power_server
    status, state = json_request(server, "/api/shutdown")
    assert status == 200 and state == fake_power[0].snapshot()
    assert state["supported"] and state["enabled"] is False and state["state"] == "off"
    status, data = json_request(server, "/api/bootstrap")
    assert status == 200 and data["shutdown"] == state
    assert fake_power[2] == fake_power[3] == []


def test_shutdown_switch_waits_for_next_batch_and_can_be_turned_off(power_server, fake_power):
    server = power_server
    for enabled in (True, True, False):
        status, state = json_request(
            server,
            "/api/shutdown",
            method="POST",
            origin=server.origin,
            body={"enabled": enabled},
        )
        assert status == 200 and state["enabled"] is enabled
        assert state["state"] == ("waiting" if enabled else "off")
        assert json_request(server, "/api/bootstrap")[1]["shutdown"] == state
    assert fake_power[2] == fake_power[3] == []


@pytest.mark.parametrize(
    "body",
    [
        {"enabled": 1},
        {"enabled": 0},
        {"enabled": "true"},
        {"enabled": None},
        {"enabled": []},
        {"enabled": {}},
        {"enabled": True, "command": "shutdown"},
        {"command": "shutdown"},
    ],
)
def test_shutdown_switch_rejects_non_boolean_or_extra_payload(power_server, fake_power, body):
    server = power_server
    status, data = json_request(
        server, "/api/shutdown", method="POST", origin=server.origin, body=body
    )
    assert status == 400 and "error" in data
    assert not fake_power[0].snapshot()["enabled"]
    assert fake_power[2] == fake_power[3] == []


@pytest.mark.parametrize("path", ["/api/shutdown", "/api/shutdown/cancel"])
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"token": False}, 401),
        ({"host": "evil.example"}, 403),
        ({"origin": None}, 403),
        ({"origin": "https://evil.example"}, 403),
    ],
)
def test_shutdown_mutations_retain_loopback_token_and_origin_guards(
    power_server, fake_power, path, options, expected
):
    server = power_server
    options = {"origin": server.origin, **options}
    status, _ = json_request(
        server,
        path,
        method="POST",
        body={"enabled": True} if path == "/api/shutdown" else {},
        **options,
    )
    assert status == expected and not fake_power[0].snapshot()["enabled"]
    assert fake_power[2] == fake_power[3] == []


def test_shutdown_read_retains_auth_and_rebinding_guards(power_server, fake_power):
    for options, expected in (
        ({"token": False}, 401),
        ({"host": "evil.example"}, 403),
        ({"origin": "https://evil.example"}, 403),
    ):
        assert json_request(power_server, "/api/shutdown", **options)[0] == expected
    assert not fake_power[0].snapshot()["enabled"]


def test_cancel_endpoint_requires_empty_payload_and_invalidates_countdown(power_server, fake_power):
    server = power_server
    controller, now, timers, calls = fake_power
    # Drive a fake bound batch directly; no worker or real shutdown is started.
    server.power_watch.lookup = lambda _: job(status="completed")
    controller.arm("batch")
    controller.observe(job())
    controller.observe(job(status="completed"))
    assert controller.snapshot()["state"] == "countdown"
    status, _ = json_request(
        server,
        "/api/shutdown/cancel",
        method="POST",
        origin=server.origin,
        body={"command": "shutdown"},
    )
    assert status == 400 and controller.snapshot()["enabled"]
    status, state = json_request(
        server,
        "/api/shutdown/cancel",
        method="POST",
        origin=server.origin,
        body={},
    )
    assert status == 200 and not state["enabled"] and timers[0].cancelled
    now[0] += 60
    timers[0].fire()
    assert calls == []


def test_empty_shutdown_payload_still_only_quits_the_assistant(power_server, fake_power):
    status, state = json_request(
        power_server,
        "/api/shutdown",
        method="POST",
        origin=power_server.origin,
        body={},
    )
    assert status == 200 and state == {"status": "stopping"}
    assert fake_power[2] == fake_power[3] == []


def test_server_close_invalidates_a_pending_shutdown(tmp_path, fake_power):
    controller, now, timers, calls = fake_power
    server = LocalServer(FrontendJobs(unit_settings(tmp_path)), power=controller)
    controller.arm("batch")
    controller.observe(job())
    controller.observe(job(status="completed"))
    server.server_close()
    assert timers[0].cancelled and not controller.snapshot()["enabled"]
    now[0] += 60
    timers[0].fire()
    assert calls == []


def test_native_job_start_notifies_without_holding_job_lock(tmp_path, fake_power, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    release = threading.Event()
    monkeypatch.setattr(jobs, "_run", lambda _: release.wait(timeout=5))
    controller, _, timers, calls = fake_power
    server = LocalServer(jobs, power=controller)
    observed = []
    installed = jobs._on_start

    def callback(value):
        observed.append(jobs._lock._is_owned())
        installed(value)

    jobs._on_start = callback
    try:
        controller.arm()
        started = jobs._start("review", [{"course_id": "1", "assignment_id": "2"}])
        assert observed == [False]
        server.power_watch.poll_once()
        assert controller.snapshot()["job_id"] == started["id"]
        assert controller.snapshot()["state"] == "armed"
        release.set()
        assert jobs.wait_idle(timeout=5) is False  # The fake worker does not edit job state.
        with jobs._lock:
            jobs._jobs[started["id"]]["status"] = "completed"
            jobs._jobs[started["id"]]["items"][0]["status"] = "ready"
            jobs._active = None
        server.power_watch.poll_once()
        assert controller.snapshot()["state"] == "countdown"
        assert len(timers) == 1 and calls == []
    finally:
        release.set()
        jobs._thread.join(timeout=5)
        server.server_close()


def test_nested_followup_start_never_waits_for_controller_lock(tmp_path, fake_power, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    release, notified = threading.Event(), threading.Event()
    monkeypatch.setattr(jobs, "_run", lambda _: release.wait(timeout=5))
    controller, _, _, calls = fake_power
    server = LocalServer(jobs, power=controller)

    def start_followup():
        with jobs._lock:
            jobs._start("fill", [{"course_id": "1", "assignment_id": "2"}])
            notified.set()

    starter = threading.Thread(target=start_followup, daemon=True)
    try:
        with controller._lock:
            starter.start()
            assert notified.wait(timeout=2)
        assert calls == []
    finally:
        release.set()
        starter.join(timeout=5)
        if jobs._thread:
            jobs._thread.join(timeout=5)
        server.server_close()


def test_power_watch_only_reads_worker_while_enabled_and_tracks_bound_completion(fake_power):
    controller, _, timers, calls = fake_power
    current = [job()]
    lookups = []

    def lookup(identifier):
        lookups.append(identifier)
        return current[0]

    watch = PowerWatch(controller, lookup)
    try:
        watch.poll_once()
        assert lookups == []
        assert watch.set_enabled(True)["job_id"] == "batch"
        assert lookups == [None]
        current[0] = job(status="completed")
        watch.poll_once()
        assert lookups == [None, "batch"]
        assert controller.snapshot()["state"] == "countdown" and len(timers) == 1
        watch.set_enabled(False)
        watch.poll_once()
        assert lookups == [None, "batch"] and timers[0].cancelled and calls == []
    finally:
        watch.close()


def test_companion_proxies_authenticated_operations_and_keeps_power_local(
    tmp_path, fake_power, monkeypatch
):
    jobs = FrontendJobs(unit_settings(tmp_path))
    monkeypatch.setattr(jobs, "refresh", lambda: {"status": "offline-refresh"})
    with running(LocalServer(jobs)) as upstream:
        source = {"port": upstream.server_port, "token": upstream.token}
        with running(create_companion(source, power=fake_power[0])) as server:
            assert server.token != upstream.token
            status, health = json_request(server, "/api/health")
            assert status == 200 and health["power_companion"] is True
            status, state = json_request(
                server,
                "/api/shutdown",
                method="POST",
                origin=server.origin,
                body={"enabled": True},
            )
            assert status == 200 and state["enabled"]
            assert not upstream.power.snapshot()["enabled"]
            assert json_request(server, "/api/bootstrap")[1]["shutdown"] == state
            status, response = json_request(
                server, "/api/refresh", method="POST", origin=server.origin, body={}
            )
            assert status == 200 and response == {"status": "offline-refresh"}
            assert fake_power[2] == fake_power[3] == []


@pytest.mark.parametrize("phase", ["armed", "countdown"])
def test_saving_answers_cancels_shutdown_before_task_lookup(power_server, fake_power, phase):
    server = power_server
    controller, now, timers, calls = fake_power
    identifier = "a" * 32
    current = [job(identifier=identifier)]
    server.power_watch.lookup = lambda _: current[0]
    controller.arm(identifier)
    controller.observe(current[0])
    if phase == "countdown":
        current[0] = job(identifier=identifier, status="completed")
        controller.observe(current[0])
    assert controller.snapshot()["state"] == phase
    status, _ = json_request(
        server,
        f"/api/jobs/{identifier}/items/1:2/answers",
        method="POST",
        origin=server.origin,
        body={},
    )
    # The offline job does not exist; cancellation must precede that lookup error.
    assert status == 400 and not controller.snapshot()["enabled"]
    if phase == "countdown":
        assert timers[0].cancelled
    now[0] += 60
    for timer in timers:
        timer.fire()
    assert calls == []


@pytest.fixture(params=["native", "companion"])
def preflight_check(tmp_path, fake_power, monkeypatch, request):
    responses = {}
    server = None
    if request.param == "native":
        jobs = FrontendJobs(unit_settings(tmp_path))
        server = LocalServer(jobs, power=fake_power[0])
        validate = server._power_ready
    else:
        source = WorkerSource({"port": 1, "token": "offline-only"})
        monkeypatch.setattr(source, "json", lambda path: responses[path])
        validate = source.preflight

    def check(bound, history=(), *, active=None, include_target=True):
        observed = ([bound] if include_target else []) + list(history)
        if server is not None:
            with jobs._lock:
                jobs._jobs = {value["id"]: value for value in observed}
                jobs._active = active
        else:
            responses["/api/jobs/" + bound["id"]] = bound
            responses["/api/bootstrap"] = {"jobs": observed, "active_job_id": active}
        return validate(bound["id"])

    try:
        yield check
    finally:
        if server is not None:
            server.server_close()


def completed_batch(**values):
    return job(
        identifier="a" * 32,
        status="completed",
        created_at="2026-10-18T01:00:00Z",
        **values,
    )


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize(
    ("created_at", "allowed"),
    [
        ("2026-10-18T00:59:59Z", True),
        ("2026-10-18T01:00:00Z", False),
        ("2026-10-18T10:00:00+09:00", False),
        ("2026-10-18T01:00:01Z", False),
    ],
)
def test_preflight_allows_only_strictly_older_job_history(
    preflight_check, status, created_at, allowed
):
    other = job(identifier="b" * 32, kind="refresh", status=status, created_at=created_at)
    assert preflight_check(completed_batch(), [other]) is allowed


@pytest.mark.parametrize("target", ["bound", "other"])
@pytest.mark.parametrize("timestamp", [None, "", "invalid", 42, "missing", "2026-10-18T00:00:00"])
def test_preflight_fails_safe_when_any_job_timestamp_is_unknown(preflight_check, target, timestamp):
    bound = completed_batch()
    other = job(identifier="b" * 32, status="failed", created_at="2026-10-18T00:59:59Z")
    record = bound if target == "bound" else other
    if timestamp == "missing":
        record.pop("created_at")
    else:
        record["created_at"] = timestamp
    assert preflight_check(bound, [other]) is False


def test_preflight_requires_visible_target_and_no_active_worker(preflight_check):
    bound = completed_batch()
    assert preflight_check(bound) is True
    assert preflight_check(bound, active="b" * 32) is False
    assert preflight_check(bound, include_target=False) is False
