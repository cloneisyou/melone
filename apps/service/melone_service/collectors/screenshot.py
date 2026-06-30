from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from melone_service.models import NormalizedEvent, utc_timestamp
from melone_service.store.screen import ScreenFrame, ScreenRepository, ScreenSession


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapturedScreenshot:
    png_bytes: bytes
    width: int
    height: int
    perceptual_hash: str | None = None


class ScreenshotCaptureAPI(Protocol):
    def capture_png(self) -> CapturedScreenshot | None:
        """Return one PNG capture, or None when capture is unavailable."""


class SensitiveScreenPolicy(Protocol):
    def should_skip(self, session: ScreenSession) -> bool:
        """Return True when the active screen should not be captured."""


@dataclass(frozen=True)
class DenylistSensitiveScreenPolicy:
    app_names: Sequence[str] = ()
    bundle_ids: Sequence[str] = ()

    def should_skip(self, session: ScreenSession) -> bool:
        app_names = {_normalize_app_name(value) for value in self.app_names}
        bundle_ids = {value for value in self.bundle_ids if value}
        return (
            _normalize_app_name(session.app_name) in app_names
            or (session.bundle_id in bundle_ids if session.bundle_id else False)
        )


class MacOSScreenshotCapture:
    def __init__(
        self,
        *,
        platform_name: str | None = None,
        command: Sequence[str] | None = None,
        timeout_seconds: float = 5.0,
        temp_dir: Path | None = None,
    ) -> None:
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.command = tuple(command or ("/usr/sbin/screencapture", "-x", "-t", "png"))
        self.timeout_seconds = timeout_seconds
        self.temp_dir = temp_dir

    def capture_png(self) -> CapturedScreenshot | None:
        if self.platform_name != "darwin":
            return None

        try:
            capture_path = _temporary_capture_path(self.temp_dir)
        except OSError:
            return None

        try:
            result = subprocess.run(
                [*self.command, str(capture_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=self.timeout_seconds,
            )
            if result.returncode != 0:
                return None

            png_bytes = capture_path.read_bytes()
            width, height = _png_dimensions(png_bytes)
            return CapturedScreenshot(
                png_bytes=png_bytes,
                width=width,
                height=height,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        finally:
            _unlink_quietly(capture_path)


class ScreenshotCollector:
    name = "screenshot"

    def __init__(
        self,
        *,
        screen_repository: ScreenRepository,
        screenshots_dir: Path,
        min_interval_seconds: float,
        capture_api: ScreenshotCaptureAPI | None = None,
        sensitive_policy: SensitiveScreenPolicy | None = None,
        platform_name: str | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        frame_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.screen_repository = screen_repository
        self.screenshots_dir = screenshots_dir
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.capture_api = capture_api or MacOSScreenshotCapture(
            platform_name=self.platform_name
        )
        self.sensitive_policy = sensitive_policy or DenylistSensitiveScreenPolicy()
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.timestamp_factory = timestamp_factory or utc_timestamp
        self.frame_id_factory = frame_id_factory or _new_frame_id
        self._last_capture_attempt_at: float | None = None
        self._transition_frame_only = False
        self._captured_session_ids: set[str] = set()

    def poll(self) -> list[NormalizedEvent]:
        self.capture_latest_frame()
        return []

    def capture_latest_frame(self) -> ScreenFrame | None:
        if self.platform_name != "darwin":
            return None

        session = self.screen_repository.get_latest_open_session()
        if session is None:
            return None
        if self.sensitive_policy.should_skip(session):
            logger.info(
                "skipped sensitive screen: session_id=%s app=%s bundle_id=%s",
                session.id,
                session.app_name,
                session.bundle_id,
            )
            return None
        already_captured = session.id in self._captured_session_ids
        if self._transition_frame_only and already_captured:
            return None

        # Guarantee at least one frame per session: a session we have not yet
        # captured bypasses the global throttle so short-lived scenes still get a
        # keyframe instead of surfacing as a "no screenshot" scene. The throttle
        # only paces repeat captures within a session already captured at least once.
        if already_captured and self._is_throttled():
            return None
        self._last_capture_attempt_at = self.monotonic_clock()

        try:
            captured = self.capture_api.capture_png()
        except Exception:
            return None

        if captured is None:
            return None

        sha256 = hashlib.sha256(captured.png_bytes).hexdigest()
        existing = self.screen_repository.get_frame_by_sha256(
            session_id=session.id,
            sha256=sha256,
        )
        if existing is not None:
            logger.info(
                "screenshot duplicate frame skipped: session_id=%s sha256=%s",
                session.id,
                sha256,
            )
            return None

        frame_id = self.frame_id_factory()
        captured_at = self.timestamp_factory()
        image_path = self._image_path(
            session_id=session.id,
            captured_at=captured_at,
            frame_id=frame_id,
        )

        try:
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(captured.png_bytes)
        except OSError:
            return None

        try:
            frame = self.screen_repository.insert_frame(
                frame_id=frame_id,
                session_id=session.id,
                captured_at=captured_at,
                image_path=str(image_path),
                sha256=sha256,
                perceptual_hash=captured.perceptual_hash,
                width=captured.width,
                height=captured.height,
            )
        except Exception:
            # The DB row is the durable index. If insertion fails after the file
            # write, remove the orphaned PNG best-effort and skip this tick.
            _unlink_quietly(image_path)
            return None

        if frame is None:
            _unlink_quietly(image_path)
            return None

        self._captured_session_ids.add(session.id)
        return frame

    def set_capture_policy(
        self,
        *,
        min_interval_seconds: float,
        transition_frame_only: bool = False,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._transition_frame_only = transition_frame_only

    def _is_throttled(self) -> bool:
        if self._last_capture_attempt_at is None:
            return False
        return (
            self.monotonic_clock() - self._last_capture_attempt_at
            < self.min_interval_seconds
        )

    def _image_path(
        self,
        *,
        session_id: str,
        captured_at: str,
        frame_id: str,
    ) -> Path:
        return self.screenshots_dir / session_id / f"{captured_at}-{frame_id}.png"


def _temporary_capture_path(temp_dir: Path | None) -> Path:
    with tempfile.NamedTemporaryFile(
        suffix=".png",
        prefix="melone-capture-",
        dir=temp_dir,
        delete=False,
    ) as handle:
        return Path(handle.name)


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    if len(png_bytes) < 24 or not png_bytes.startswith(PNG_SIGNATURE):
        raise ValueError("capture did not produce a PNG")
    if png_bytes[12:16] != b"IHDR":
        raise ValueError("PNG is missing IHDR")

    width = int.from_bytes(png_bytes[16:20], byteorder="big")
    height = int.from_bytes(png_bytes[20:24], byteorder="big")
    return width, height


def _new_frame_id() -> str:
    return f"screen_frame_{uuid.uuid4().hex}"


def _normalize_app_name(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
