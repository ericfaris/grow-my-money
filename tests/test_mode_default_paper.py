"""Acceptance #7: default paper; explicit-only switch to live; no auto path."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.config import load_config
from src.state import State

REPO = Path(__file__).resolve().parent.parent


def test_missing_mode_defaults_paper():
    assert load_config(env={}, use_dotenv=False).mode == "paper"


def test_garbage_mode_defaults_paper():
    assert load_config(env={"MODE": "banana"}, use_dotenv=False).mode == "paper"
    assert load_config(env={"MODE": "LIVE!!"}, use_dotenv=False).mode == "paper"
    assert load_config(env={"MODE": ""}, use_dotenv=False).mode == "paper"


def test_explicit_live_env_is_honored_by_config_but_persisted_wins(tmp_path):
    # config can parse live, but the persisted runtime mode is the source of truth
    cfg = load_config(env={"MODE": "live"}, use_dotenv=False)
    assert cfg.mode == "live"
    st = State(tmp_path / "grow.db")
    # fresh db: no persisted mode yet
    assert st.get_runtime("mode") is None
    # once an explicit set-mode paper is recorded, it wins over a stale live env
    st.set_mode("paper")
    assert st.get_mode() == "paper"
    st.close()


def test_no_automatic_live_assignment_in_source():
    """Grep guard: the only place that sets mode to 'live' is cli set-mode."""
    hits = []
    for py in (REPO / "src").glob("*.py"):
        text = py.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            if 'set_mode("live")' in line or "set_mode('live')" in line:
                hits.append((py.name, lineno, line.strip()))
    # Allowed only in cli.py
    non_cli = [h for h in hits if h[0] != "cli.py"]
    assert non_cli == [], f"unexpected live-mode assignment outside cli.py: {non_cli}"


def test_set_mode_live_without_confirmation_does_not_change_mode(tmp_path, monkeypatch):
    """Feeding a wrong confirmation phrase to set-mode live leaves mode=paper."""
    # Run the CLI in a subprocess with a wrong phrase on stdin, pointed at a temp db.
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    # Use an isolated cwd so state/ is under tmp_path
    workdir = tmp_path / "work"
    (workdir / "state").mkdir(parents=True)
    # symlink src package into workdir
    (workdir / "src").symlink_to(REPO / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "src.cli", "set-mode", "live"],
        input="wrong phrase\n",
        capture_output=True, text=True, cwd=workdir,
        env={**env, "PYTHONPATH": str(REPO)},
    )
    assert proc.returncode == 1
    st = State(workdir / "state" / "grow.db")
    assert st.get_mode("paper") == "paper"
    st.close()
