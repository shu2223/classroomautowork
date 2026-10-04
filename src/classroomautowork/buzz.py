"""Buzz's bundled Whisper.cpp backend; media and inference stay on this computer.

No OpenAI API mode, URLs, prompts from course content, or shell invocation are accepted.
Five-minute segments are separate persistent tasks, so a long recording resumes by segment.
"""

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from .errors import ConfigurationError, WorkflowError
from .local import atomic_json, require_private_path, sha256_file, state_root
from .progress import report
from .store import artifact, fingerprint


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
        if not self.engine.is_file() or sha256_file(self.engine) != self.engine_sha256:
            raise ConfigurationError(
                "Buzz's bundled engine is missing or changed; run buzz-setup again."
            )
        model = require_private_path(Path(self.model_path))
        if not model.is_file() or sha256_file(model) != self.model_sha256:
            raise ConfigurationError(
                "Local Whisper.cpp model is missing or fails its SHA-256 check."
            )
        if not re.fullmatch(r"[a-z]{2,3}|auto", self.language):
            raise ConfigurationError("Invalid local transcription language.")
        if not 30 <= self.segment_seconds <= 600 or not 60 <= self.timeout_seconds <= 14400:
            raise ConfigurationError("Invalid transcription time budget.")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise ConfigurationError(
                "ffmpeg and ffprobe must be installed for local media processing."
            )
        return self


def setup_buzz(root: Path, model_size: str = "small", language: str = "ja") -> dict:
    if model_size not in {"tiny", "base", "small", "medium", "large-v3"}:
        raise ConfigurationError("Choose a multilingual Whisper.cpp model size.")
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


def run_local(arguments: list[str], timeout: int) -> str:
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
    for start in range(0, int(duration) + 1, config.segment_seconds):
        if start >= duration:
            break
        index = start // config.segment_seconds
        report(
            progress,
            f"正在处理录像第 {index + 1}/{total} 段（{start:.0f}–{min(duration, start + config.segment_seconds):.0f} 秒）",
            "transcribe",
            current=index,
            total=total,
            unit="segments",
        )

        def segment(key, offset=start, index=index):
            folder = destination / key[:24]
            folder.mkdir(parents=True, exist_ok=True)
            wave = folder / "audio.wav"
            report(
                progress,
                f"正在提取第 {index + 1}/{total} 段音轨",
                "transcribe",
                current=index,
                total=total,
                unit="segments",
            )
            run_local(
                [
                    shutil.which("ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
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
            )
            prefix = folder / "transcript"
            report(
                progress,
                f"Buzz 正在本机转录第 {index + 1}/{total} 段，单段完成后更新",
                "transcribe",
                current=index,
                total=total,
                unit="segments",
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
                ],
                config.timeout_seconds,
            )
            srt, vtt = prefix.with_suffix(".srt"), prefix.with_suffix(".vtt")
            if not srt.is_file() or not vtt.is_file():
                raise WorkflowError("Buzz backend did not create timestamped subtitle files.")
            parsed = parse_srt(srt.read_text(encoding="utf-8-sig"), offset)
            wave.unlink(missing_ok=True)
            return {"chunks": parsed, "artifacts": [artifact(srt), artifact(vtt)]}

        result = store.memo("transcript-segment", [identity, start], segment)
        chunks.extend(result["chunks"])
        artifacts.extend(result["artifacts"])
        report(
            progress,
            f"已完成并缓存 {index + 1}/{total} 段带时间戳转录",
            "transcribe",
            current=index + 1,
            total=total,
            unit="segments",
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
