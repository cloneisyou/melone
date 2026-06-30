"""Cross-process recording pause flag.

The RPC daemon and the collector service run as separate processes, so pause
state is shared through a flag file rather than in memory. Pure pathlib — safe
to import on Windows (no fcntl), unlike main.py.
"""

from __future__ import annotations

from pathlib import Path


def is_paused(pause_flag_path: Path) -> bool:
    return pause_flag_path.exists()


def set_paused(pause_flag_path: Path) -> None:
    pause_flag_path.parent.mkdir(parents=True, exist_ok=True)
    pause_flag_path.touch(exist_ok=True)


def clear_paused(pause_flag_path: Path) -> None:
    pause_flag_path.unlink(missing_ok=True)
