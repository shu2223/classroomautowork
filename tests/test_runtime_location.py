"""Bootstrap regression cases; these fixtures do not claim OAuth/API success."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from classroomautowork import local
from classroomautowork.errors import ConfigurationError


def test_windows_package_root_is_found_when_normal_appdata_is_empty(tmp_path, monkeypatch):
    normal = tmp_path / "normal"
    package = (
        tmp_path / "AppData/Local/Packages/OpenAI.Codex_unit/LocalCache/Local/classroomautowork"
    )
    package.mkdir(parents=True)
    local.atomic_json(package / "settings.json", {"account": "unit-test@example.invalid"})
    monkeypatch.delenv("CLASSROOMAUTOWORK_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(normal))
    roots = local.state_candidates(windows=True, profile=tmp_path)
    monkeypatch.setattr(local, "state_candidates", lambda: roots)
    assert local.state_root() == package.resolve()


def test_identical_virtual_and_physical_configs_use_physical_root(tmp_path, monkeypatch):
    physical, virtual = tmp_path / "physical", tmp_path / "virtual"
    for root in (physical, virtual):
        local.atomic_json(root / "settings.json", {"account": "unit-test@example.invalid"})
    monkeypatch.delenv("CLASSROOMAUTOWORK_HOME", raising=False)
    monkeypatch.setattr(local, "state_candidates", lambda: [physical, virtual])
    assert local.state_root() == physical.resolve()
    local.atomic_json(virtual / "settings.json", {"account": "other-unit@example.invalid"})
    with pytest.raises(ConfigurationError, match="Multiple private installations"):
        local.state_root()


def test_explicit_root_is_preserved_and_git_root_rejected(tmp_path, monkeypatch):
    private = tmp_path / "explicit"
    monkeypatch.setenv("CLASSROOMAUTOWORK_HOME", str(private))
    assert local.state_root() == private.resolve()
    (private / ".git").mkdir(parents=True)
    with pytest.raises(ConfigurationError, match="outside Git"):
        local.state_root()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell launcher")
def test_powershell_launcher_bootstraps_with_empty_localappdata(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    private = tmp_path / "private"
    local.atomic_json(
        private / "runtime.json", {"python": sys.executable, "skill": str(tmp_path / "skill")}
    )
    local.atomic_json(private / "settings.json", {})
    env = {
        **os.environ,
        "LOCALAPPDATA": str(tmp_path / "empty-appdata"),
        "CLASSROOMAUTOWORK_HOME": str(private),
    }
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(repo / "scripts/start-review.ps1"),
            "-DueBefore",
            "2026-10-10",
            "-ValidateOnly",
        ],
        env=env,
        capture_output=True,
        timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert str(private / "settings.json") in result.stdout
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["oauth"] == "not_verified_by_doctor"
