"""Buzz's bundled Whisper.cpp backend; media and inference stay on this computer.

No OpenAI API mode, URLs, prompts from course content, or shell invocation are accepted.
Five-minute segments are separate persistent tasks, so a long recording resumes by segment.
"""

import codecs
import json
import math
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
import wave
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from .errors import ConfigurationError, WorkflowError
from .local import atomic_json, require_private_path, sha256_file, state_root
from .progress import report
from .store import artifact, fingerprint

LARGE_MODELS = ("large-v3-turbo", "large-v3")
DEFAULT_MODEL = "large-v3-turbo"
_verified_files = ContextVar("buzz_verified_files", default=None)


@contextmanager
def validation_session():
    """Reuse expensive binary hashes within one run, never across independent runs."""
    token = _verified_files.set({}) if _verified_files.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            _verified_files.reset(token)


def _verified_hash(path):
    path = path.resolve()

    def signature():
        value = path.stat()
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    before = signature()
    cache = _verified_files.get()
    key = (str(path), before)
    if cache is not None and key in cache:
        return cache[key]
    checksum = sha256_file(path)
    if signature() != before:
        raise ConfigurationError("Local transcription binary changed while being verified.")
    if cache is not None:
        cache[key] = checksum
    return checksum


def require_large_model(path: Path):
    """Check Whisper's actual encoder dimensions, rather than trusting a filename."""
    with path.open("rb") as stream:
        header = stream.read(44)
    if len(header) != 44:
        raise ConfigurationError("转录模型文件不完整；请配置 large-v3-turbo 或 large-v3。")
    magic, _, _, state, _, layers, *_ = struct.unpack("<11i", header)
    if magic != 0x67676D6C or state < 1280 or layers < 32:
        raise ConfigurationError(
            "转录只允许 Whisper 大模型；旧 tiny/base/small/medium 不再用于作业。请运行 buzz-setup。"
        )


@dataclass(frozen=True)
class BuzzConfig:
    buzz_root: str
    model_path: str
    model_sha256: str
    engine_sha256: str
    language: str = "ja"
    segment_seconds: int = 300
    timeout_seconds: int = 3600

    @property
    def engine(self) -> Path:
        # Use exactly the engine shipped in the configured Buzz installation.
        root = Path(self.buzz_root).resolve()
        options = [
            root / "_internal/buzz/whisper_cpp/whisper-cli.exe",
            root / "buzz/whisper_cpp/whisper-cli",
            root / "whisper_cpp/whisper-cli",
        ]
        return next((p for p in options if p.is_file()), options[0])

    @classmethod
    def load(cls, path: Path | None = None) -> "BuzzConfig":
        path = require_private_path(path or state_root() / "buzz.json")
        if not path.is_file():
            raise ConfigurationError("Buzz backend is not configured; run buzz-setup.")
        try:
            config = cls(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("Invalid private Buzz configuration.") from exc
        return config.validated()

    def validated(self) -> "BuzzConfig":
        if not self.engine.is_file() or _verified_hash(self.engine) != self.engine_sha256:
            raise ConfigurationError(
                "Buzz's bundled engine is missing or changed; run buzz-setup again."
            )
        model = require_private_path(Path(self.model_path))
        if not model.is_file() or _verified_hash(model) != self.model_sha256:
            raise ConfigurationError(
                "Local Whisper.cpp model is missing or fails its SHA-256 check."
            )
        require_large_model(model)
        if not re.fullmatch(r"[a-z]{2,3}|auto", self.language):
            raise ConfigurationError("Invalid local transcription language.")
        if not 30 <= self.segment_seconds <= 600 or not 60 <= self.timeout_seconds <= 14400:
            raise ConfigurationError("Invalid transcription time budget.")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise ConfigurationError(
                "ffmpeg and ffprobe must be installed for local media processing."
            )
        return self


def setup_buzz(root: Path, model_size: str = DEFAULT_MODEL, language: str = "ja") -> dict:
    if model_size not in LARGE_MODELS:
        raise ConfigurationError("转录必须使用 large-v3-turbo 或 large-v3。")
    root = root.resolve()
    if not (root / "Buzz.exe").is_file() and not (root / "buzz").exists():
        raise ConfigurationError("Specify an existing Buzz installation directory.")
    name = f"ggml-{model_size}.bin"
    # Only the upstream Whisper.cpp model repository is contacted. No course media is sent.
    try:
        info = requests.get(
            "https://huggingface.co/api/models/ggerganov/whisper.cpp?blobs=true", timeout=60
        )
        info.raise_for_status()
        data = info.json()
        entry = next(x for x in data["siblings"] if x["rfilename"] == name)
        checksum = entry["lfs"]["sha256"]
        revision = data["sha"]
        if not re.fullmatch(r"[a-f0-9]{64}", checksum) or not re.fullmatch(
            r"[a-f0-9]{40}", revision
        ):
            raise ValueError("Invalid upstream model metadata")
        path = require_private_path(state_root() / "models" / name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or sha256_file(path) != checksum:
            temporary = path.with_suffix(".part")
            with requests.get(
                f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{revision}/{name}",
                stream=True,
                timeout=(30, 120),
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        stream.write(chunk)
            if sha256_file(temporary) != checksum:
                temporary.unlink(missing_ok=True)
                raise WorkflowError("Downloaded local model failed its upstream SHA-256 check.")
            os.replace(temporary, path)
    except (requests.RequestException, ValueError, KeyError, StopIteration) as exc:
        raise WorkflowError(
            "Official local model download failed; no backend success claimed."
        ) from exc
    config = BuzzConfig(str(root), str(path), checksum, "", language)
    if not config.engine.is_file():
        raise ConfigurationError("This Buzz installation has no bundled Whisper.cpp CLI engine.")
    config = BuzzConfig(
        str(root), str(path), checksum, sha256_file(config.engine), language
    ).validated()
    atomic_json(state_root() / "buzz.json", asdict(config))
    return {
        "status": "configured",
        "backend": "Buzz bundled Whisper.cpp",
        "model": name,
        "sha256": checksum,
        "inference": "not_verified_until_transcription",
        "config": str(state_root() / "buzz.json"),
    }


def _stop_local(process):
    """Reap our backend and its children when a timeout or progress callback fails."""
    if process.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
    process.wait()


def _run_streaming(arguments, timeout, on_output):
    """Read real backend output without blocking timeout checks or leaking transcripts."""
    process = subprocess.Popen(
        arguments,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    packets = queue.SimpleQueue()

    def read_output():
        try:
            while data := process.stdout.read1(4096):
                packets.put(data)
        finally:
            packets.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    captured = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkflowError(
                    "Local media processing timed out; rerun to resume completed segments."
                )
            try:
                data = packets.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            if data is None:
                pending += decoder.decode(b"", final=True)
                if pending:
                    on_output(pending)
                break
            # Callers need parsed progress, not an unbounded copy of private course text.
            captured.extend(data[: max(0, 4 * 1024 * 1024 - len(captured))])
            pending += decoder.decode(data)
            lines = re.split(r"[\r\n]", pending)
            pending = lines.pop()
            for line in lines:
                if line:
                    on_output(line)
            if len(pending) > 65536:
                pending = pending[-65536:]
        remaining = deadline - time.monotonic()
        try:
            code = process.wait(timeout=max(0, remaining))
        except subprocess.TimeoutExpired as exc:
            raise WorkflowError(
                "Local media processing timed out; rerun to resume completed segments."
            ) from exc
        if code:
            raise WorkflowError("Local media backend failed; no successful transcript claimed.")
        return captured.decode("utf-8", errors="replace")
    finally:
        _stop_local(process)
        reader.join(timeout=2)
        process.stdout.close()


def run_local(arguments: list[str], timeout: int, *, on_output=None) -> str:
    if on_output is not None:
        return _run_streaming(arguments, timeout, on_output)
    try:
        result = subprocess.run(
            arguments,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError(
            "Local media processing timed out; rerun to resume completed segments."
        ) from exc
    if result.returncode:
        raise WorkflowError("Local media backend failed; no successful transcript claimed.")
    return result.stdout.decode("utf-8", errors="replace")


def _timestamp_seconds(stamp):
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})(?:[,.](\d{1,6}))?", stamp)
    if not match or int(match[2]) >= 60 or int(match[3]) >= 60:
        return None
    fraction = float("0." + match[4]) if match[4] else 0.0
    return int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3]) + fraction


def _ffmpeg_seconds(line):
    if line.startswith("out_time_us="):
        try:
            value = float(line.split("=", 1)[1]) / 1_000_000
        except ValueError:
            return None
        return value if math.isfinite(value) and value >= 0 else None
    if line.startswith("out_time="):
        return _timestamp_seconds(line.split("=", 1)[1])
    return None


def _whisper_seconds(line, duration):
    # Both are emitted by the bundled whisper-cli, never inferred from wall time.
    percent = re.search(r"\bwhisper[^:]*:\s*progress\s*=\s*(\d{1,3})%", line)
    if percent and 0 <= int(percent[1]) <= 100:
        return duration * int(percent[1]) / 100
    stamp = re.match(
        r"\s*\[\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})\]",
        line,
    )
    if stamp:
        return _timestamp_seconds(stamp[1])
    return None


def _wave_duration(path):
    try:
        with wave.open(str(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
    except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
        raise WorkflowError("Local audio extraction did not create a readable WAV file.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise WorkflowError("Local audio extraction did not create nonempty audio.")
    return duration


def seconds(stamp: str) -> float:
    hours, minutes, sec, milli = re.split(r"[:,.]", stamp)
    return int(hours) * 3600 + int(minutes) * 60 + int(sec) + int(milli) / 1000


def parse_srt(text: str, offset: float = 0) -> list[dict]:
    chunks = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        match = re.search(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})", block
        )
        if not match:
            continue
        start, end = seconds(match[1]) + offset, seconds(match[2]) + offset
        words = block[match.end() :].strip()
        if start < 0 or end < start:
            raise WorkflowError("Invalid subtitle timestamps; transcript rejected.")
        if words:
            chunks.append(
                {
                    "locator": f"time {start:.3f}-{end:.3f}s",
                    "start_seconds": start,
                    "end_seconds": end,
                    "text": words,
                }
            )
    return chunks


def transcribe_media(
    path: Path, destination: Path, store, config: BuzzConfig, *, progress=None
) -> dict:
    report(progress, "正在校验 Buzz 本地后端和模型", "transcribe")
    config.validated()
    destination.mkdir(parents=True, exist_ok=True)
    duration = float(
        run_local(
            [
                shutil.which("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path.resolve()),
            ],
            60,
        ).strip()
    )
    if not 0 < duration <= 86400:
        raise WorkflowError("Invalid recording duration or recording longer than 24 hours.")
    media_hash = sha256_file(path)
    identity = {
        "backend": "buzz-whispercpp-v1",
        "engine": config.engine_sha256,
        "model": config.model_sha256,
        "language": config.language,
        "segment_seconds": config.segment_seconds,
        "media": media_hash,
    }
    chunks, artifacts = [], []
    total = math.ceil(duration / config.segment_seconds)
    rate_fields = {
        "model": Path(config.model_path).stem.removeprefix("ggml-"),
        "engine": config.engine_sha256,
        "model_sha256": config.model_sha256,
        "language": config.language,
        "rate_key": fingerprint(
            [config.engine_sha256, config.model_sha256, config.language, ["-ng"]]
        ),
    }
    report(
        progress,
        f"录像共 {duration:.0f} 秒，正在检查 {total} 段本地转录缓存",
        "transcribe",
        phase="start",
        current=0,
        total=total,
        unit="segments",
        media_current_seconds=0,
        media_total_seconds=duration,
        segment_total=total,
        segment_start_seconds=0,
        segment_seconds=min(config.segment_seconds, duration),
        **rate_fields,
    )
    for start in range(0, int(duration) + 1, config.segment_seconds):
        if start >= duration:
            break
        index = start // config.segment_seconds
        segment_duration = min(config.segment_seconds, duration - start)
        media_fields = {
            **rate_fields,
            "current": index,
            "total": total,
            "unit": "segments",
            "media_total_seconds": duration,
            "segment_index": index,
            "segment_start_seconds": start,
            "segment_total": total,
            "segment_seconds": segment_duration,
        }
        report(
            progress,
            f"正在处理录像第 {index + 1}/{total} 段（{start:.0f}–{min(duration, start + config.segment_seconds):.0f} 秒）",
            "transcribe",
            phase="start",
            media_current_seconds=start,
            **media_fields,
        )
        executed = False

        def segment(
            key,
            offset=start,
            index=index,
            segment_duration=segment_duration,
            media_fields=media_fields,
        ):
            nonlocal executed
            executed = True
            folder = destination / key[:24]
            folder.mkdir(parents=True, exist_ok=True)
            wave = folder / "audio.wav"

            def track_phase(phase, message, parser, phase_duration=segment_duration):
                phase_started = time.monotonic()
                processed = 0.0

                def emit(value, *, complete=False):
                    nonlocal processed
                    value = max(processed, min(phase_duration, max(0.0, value)))
                    elapsed = max(0.0, time.monotonic() - phase_started)
                    processed = value
                    report(
                        progress,
                        message,
                        "transcribe",
                        phase=phase,
                        phase_current_seconds=value,
                        phase_total_seconds=phase_duration,
                        phase_elapsed_seconds=elapsed,
                        phase_complete=complete,
                        media_current_seconds=offset + (value if phase == "infer" else 0),
                        processing_rate=value / elapsed if elapsed > 0 and value > 0 else None,
                        cached=False,
                        **media_fields,
                    )

                def observe(line):
                    value = parser(line)
                    if value is not None and math.isfinite(value) and value > processed:
                        emit(value)

                def finish(actual_duration=None):
                    nonlocal phase_duration, processed
                    if actual_duration is not None:
                        phase_duration = actual_duration
                        processed = min(processed, phase_duration)
                    emit(phase_duration, complete=True)

                emit(0)
                return observe, finish

            observe_prepare, finish_prepare = track_phase(
                "prepare", f"正在提取第 {index + 1}/{total} 段音轨", _ffmpeg_seconds
            )
            run_local(
                [
                    shutil.which("ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-nostats",
                    "-progress",
                    "pipe:1",
                    "-y",
                    "-ss",
                    str(offset),
                    "-i",
                    str(path.resolve()),
                    "-t",
                    str(config.segment_seconds),
                    "-vn",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(wave),
                ],
                config.timeout_seconds,
                on_output=observe_prepare,
            )
            audio_duration = min(segment_duration, _wave_duration(wave))
            finish_prepare(audio_duration)
            prefix = folder / "transcript"
            observe_infer, finish_infer = track_phase(
                "infer",
                f"Buzz 大模型 {Path(config.model_path).stem.removeprefix('ggml-')} 正在本机转录第 {index + 1}/{total} 段，完成后缓存",
                lambda line: _whisper_seconds(line, audio_duration),
                audio_duration,
            )
            run_local(
                [
                    str(config.engine),
                    "-m",
                    config.model_path,
                    "-f",
                    str(wave),
                    "-l",
                    config.language,
                    "-osrt",
                    "-ovtt",
                    "-of",
                    str(prefix),
                    "-ng",
                    "-pp",
                ],
                config.timeout_seconds,
                on_output=observe_infer,
            )
            srt, vtt = prefix.with_suffix(".srt"), prefix.with_suffix(".vtt")
            if not srt.is_file() or not vtt.is_file():
                raise WorkflowError("Buzz backend did not create timestamped subtitle files.")
            parsed = parse_srt(srt.read_text(encoding="utf-8-sig"), offset)
            finish_infer()
            wave.unlink(missing_ok=True)
            return {"chunks": parsed, "artifacts": [artifact(srt), artifact(vtt)]}

        result = store.memo("transcript-segment", [identity, start], segment)
        chunks.extend(result["chunks"])
        artifacts.extend(result["artifacts"])
        report(
            progress,
            f"已完成并缓存 {index + 1}/{total} 段带时间戳转录",
            "transcribe",
            phase="complete",
            current=index + 1,
            total=total,
            unit="segments",
            media_current_seconds=min(duration, start + segment_duration),
            media_total_seconds=duration,
            segment_index=index,
            segment_start_seconds=start,
            segment_total=total,
            segment_seconds=segment_duration,
            cached=not executed,
            **rate_fields,
        )
    output = destination / (fingerprint(identity)[:24] + ".json")
    atomic_json(output, {"chunks": chunks, "identity": identity})
    return {
        "chunks": chunks,
        "artifacts": [*artifacts, artifact(output)],
        "backend": "Buzz bundled Whisper.cpp",
        "duration_seconds": duration,
        "warnings": ["Machine transcript; verify quotations against the original recording."],
    }
