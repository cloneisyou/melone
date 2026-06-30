from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


SETTINGS_FILENAME = "settings.json"


@dataclass(frozen=True)
class ScreenTextSettings:
    enabled: bool = False

    def to_payload(self) -> dict[str, bool]:
        return {"enabled": self.enabled}


@dataclass(frozen=True)
class AppSettings:
    screen_text: ScreenTextSettings = field(default_factory=ScreenTextSettings)

    def to_payload(self) -> dict[str, object]:
        return {"screenText": self.screen_text.to_payload()}


def app_settings_path(data_dir: Path) -> Path:
    return data_dir / SETTINGS_FILENAME


def load_app_settings(path: Path) -> AppSettings:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return AppSettings()
    except OSError:
        return AppSettings()

    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return AppSettings()

    if not isinstance(raw, dict):
        return AppSettings()

    screen_text = raw.get("screenText")
    enabled = False
    if isinstance(screen_text, dict):
        enabled = screen_text.get("enabled") is True
    return AppSettings(screen_text=ScreenTextSettings(enabled=enabled))


def save_app_settings(path: Path, settings: AppSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(settings.to_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def update_screen_text_settings(path: Path, *, enabled: bool) -> AppSettings:
    current = load_app_settings(path)
    next_settings = AppSettings(screen_text=ScreenTextSettings(enabled=enabled))
    if current == next_settings and path.exists():
        return next_settings

    save_app_settings(path, next_settings)
    return next_settings
