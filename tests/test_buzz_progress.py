"""Offline backend fixtures exercise measured media progress and process cleanup."""

import subprocess
import sys
import wave
from pathlib import Path

import pytest

from classroomautowork import buzz
from classroomautowork.errors import WorkflowError
from classroomautowork.store import Store


def write_wave(path, duration):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * int(duration * 16000))


def test_backend_progress_parsers_only_accept_actual_machine_output():
    assert buzz._ffmpeg_seconds("out_time_us=12500000") == 12.5
    assert buzz._ffmpeg_seconds("out_time=00:01:02.345678") == 62.345678
    for line in ("out_time_us=NaN", "out_time_us=-1", "out_time=N/A", "speed=4.2x"):
        assert buzz._ffmpeg_seconds(line) is None
    assert buzz._whisper_seconds("whisper_print_progress_callback: progress =  35%", 300) == 105
    assert buzz._whisper_seconds("[00:01:00.000 --> 00:01:23.400] private text", 300) == 83.4
    for line in ("progress = 50%", "whisper_print_progress_callback: progress = 101%", "loading"):
        assert buzz._whisper_seconds(line, 300) is None


def test_streaming_reads_carriage_returns_and_reaps_cancelled_backend(monkeypatch):
    original = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(buzz.subprocess, "Popen", launch)
    seen = []
    result = buzz.run_local(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; sys.stdout.write('first\\rsecond\\n');sys.stdout.flush()",
        ],
        5,
        on_output=seen.append,
    )
    assert seen == ["first", "second"] and result.replace("\r\n", "\n") == "first\rsecond\n"

    def stop(line):
        raise WorkflowError("requested stop")

    with pytest.raises(WorkflowError, match="requested stop"):
        buzz.run_local(
            [sys.executable, "-u", "-c", "import time; print('started',flush=True);time.sleep(60)"],
            5,
            on_output=stop,
        )
    assert all(child.poll() is not None for child in children)


def test_streaming_timeout_and_failure_never_claim_success(monkeypatch):
    original = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(buzz.subprocess, "Popen", launch)
    with pytest.raises(WorkflowError, match="timed out"):
        buzz.run_local(
            [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
            0.1,
            on_output=lambda _: None,
        )
    with pytest.raises(WorkflowError, match="failed"):
        buzz.run_local(
            [sys.executable, "-u", "-c", "import sys; sys.exit(3)"],
            5,
            on_output=lambda _: None,
        )
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize("first_audio_seconds", [300, 120])
def test_transcription_tracks_phases_and_excludes_cached_segments_from_speed(
    tmp_path, monkeypatch, first_audio_seconds
):
    monkeypatch.setattr(buzz.BuzzConfig, "validated", lambda self: self)
    source = tmp_path / "offline.mp4"
    source.write_bytes(b"offline fixture")
    config = buzz.BuzzConfig(
        str(tmp_path), str(tmp_path / "ggml-large-v3-turbo.bin"), "model", "engine"
    )
    calls = []

    def backend(arguments, timeout, *, on_output=None):
        calls.append(arguments)
        if "-show_entries" in arguments:
            return "450.0"
        if "-progress" in arguments:
            on_output("out_time_us=15000000")
            on_output("out_time=00:00:30.000000")
            offset = float(arguments[arguments.index("-ss") + 1])
            write_wave(Path(arguments[-1]), first_audio_seconds if offset == 0 else 150)
        else:
            assert "-pp" in arguments and "-ng" in arguments
            on_output("whisper_print_progress_callback: progress =  10%")
            on_output("[00:00:30.000 --> 00:01:00.000] offline private text")
            prefix = Path(arguments[arguments.index("-of") + 1])
            prefix.with_suffix(".srt").write_text(
                "1\n00:00:00,000 --> 00:01:00,000\noffline fixture\n", encoding="utf-8"
            )
            prefix.with_suffix(".vtt").write_text("WEBVTT\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(buzz, "run_local", backend)
    updates = []
    with Store(tmp_path / "private") as store:
        first = buzz.transcribe_media(
            source, store.root / "processed", store, config, progress=updates.append
        )
        details = [value.details for value in updates if "phase" in value.details]
        assert details[0]["media_total_seconds"] == 450
        assert details[0]["media_current_seconds"] == 0
        live = [value for value in details if value["phase"] == "infer"]
        assert any(value["media_current_seconds"] == first_audio_seconds / 10 for value in live)
        assert any(value["media_current_seconds"] == 315 for value in live)
        assert all(value["current"] == value["segment_index"] for value in live)
        assert all(value["cached"] is False for value in live)
        assert len([value for value in live if value["phase_complete"]]) == 2
        assert live[0]["phase_total_seconds"] == first_audio_seconds
        assert live[-1]["phase_total_seconds"] == 150
        assert live[-1]["segment_start_seconds"] == 300
        assert all(value["phase_elapsed_seconds"] >= 0 for value in live)
        prepared = [value for value in details if value["phase"] == "prepare"]
        assert all(value["media_current_seconds"] in (0, 300) for value in prepared)
        assert details[-1]["media_current_seconds"] == 450 and details[-1]["current"] == 2
        assert details[-1]["segment_seconds"] == 150
        assert details[-1]["rate_key"] and details[-1]["engine"] == "engine"
        updates.clear()
        calls.clear()
        again = buzz.transcribe_media(
            source, store.root / "processed", store, config, progress=updates.append
        )
        assert again["chunks"] == first["chunks"]
        assert len(calls) == 1  # ffprobe only; cached work must not calibrate inference speed.
        details = [value.details for value in updates if "phase" in value.details]
        assert all(value["phase"] in ("start", "complete") for value in details)
        assert all(value["cached"] is True for value in details if value["phase"] == "complete")


def test_missing_subtitles_do_not_report_inference_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(buzz.BuzzConfig, "validated", lambda self: self)
    source = tmp_path / "offline.mp4"
    source.write_bytes(b"offline fixture")
    config = buzz.BuzzConfig(str(tmp_path), str(tmp_path / "model.bin"), "model", "engine")

    def backend(arguments, timeout, *, on_output=None):
        if "-progress" in arguments:
            write_wave(Path(arguments[-1]), 60)
        return "60.0" if "-show_entries" in arguments else ""

    monkeypatch.setattr(buzz, "run_local", backend)
    updates = []
    with Store(tmp_path / "private") as store:
        with pytest.raises(WorkflowError, match="subtitle"):
            buzz.transcribe_media(
                source, store.root / "processed", store, config, progress=updates.append
            )
    assert not any(
        value.details.get("phase") == "infer" and value.details.get("phase_complete")
        for value in updates
    )
    assert not any(value.details.get("phase") == "complete" for value in updates)
