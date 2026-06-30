from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

# Image.Resampling exists since Pillow 9.1; pyproject pins pillow>=10.
_RESAMPLE = Image.Resampling.BOX

# Decode failures that should degrade to "treat the frame as fully changed"
# rather than crash the caller. DecompressionBombError is not an OSError/ValueError.
IMAGE_LOAD_ERRORS = (OSError, ValueError, Image.DecompressionBombError)


PHASH_NEAR_DUPLICATE_MAX_DISTANCE = 4

# Change-detection runs on a proportionally downscaled copy of each frame, not
# the full-resolution pixels. This mirrors screenpipe's FrameComparer: comparing
# at 1/4 resolution preserves aspect ratio (important for ultrawides) and cuts
# the work to ~1/16 the pixels. OCR still runs on the full-resolution original.
# BOX (area-averaging) downscaling is used over NEAREST so a thin change folds
# into its cell's average instead of being missed by point sampling.
DEFAULT_DOWNSCALE_FACTOR = 4
# A pixel counts as changed when any channel differs by at least this much,
# unchanged from the previous full-resolution comparison. Note the diff *score*
# is now the fraction of downscaled pixels that changed, which approximates the
# old full-resolution fraction; the keyframe/crop thresholds are read against it.
DEFAULT_PIXEL_TOLERANCE = 16
# Downscaling only pays off on large captures; below this (longest side) a frame
# is compared at full resolution. numpy makes a full-res diff of a small frame
# sub-millisecond, and it keeps the changed-region bbox pixel-exact.
MIN_SOURCE_DIM_FOR_DOWNSCALE = 512


@dataclass(frozen=True)
class CropBBox:
    x: int
    y: int
    width: int
    height: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "x": self.x,
                "y": self.y,
                "width": self.width,
                "height": self.height,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class ImageDiff:
    score: float
    crop_bbox: CropBBox | None = None


def perceptual_hash_distance(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None

    normalized_left = _normalize_hash(left)
    normalized_right = _normalize_hash(right)
    if normalized_left == normalized_right:
        return 0

    binary_distance = _binary_hash_distance(normalized_left, normalized_right)
    if binary_distance is not None:
        return binary_distance

    hex_distance = _hex_hash_distance(normalized_left, normalized_right)
    if hex_distance is not None:
        return hex_distance

    return None


def are_near_duplicate_hashes(
    left: str | None,
    right: str | None,
    *,
    max_distance: int = PHASH_NEAR_DUPLICATE_MAX_DISTANCE,
) -> bool:
    distance = perceptual_hash_distance(left, right)
    return distance is not None and distance <= max_distance


@dataclass(frozen=True)
class _DownscaledFrame:
    """A frame prepared for comparison: full-resolution dimensions plus the
    downscaled pixel array compared against the baseline."""

    full_width: int
    full_height: int
    pixels: np.ndarray  # (h, w, channels) uint8 of the downscaled image


def _effective_factor(full_width: int, full_height: int, factor: int) -> int:
    if factor <= 1:
        return 1
    if max(full_width, full_height) < MIN_SOURCE_DIM_FOR_DOWNSCALE:
        return 1
    return factor


def _load_downscaled_frame(
    source: str | Path,
    *,
    factor: int = DEFAULT_DOWNSCALE_FACTOR,
) -> _DownscaledFrame:
    with Image.open(source) as image:
        image = image.convert("RGBA")
        full_width, full_height = image.width, image.height
        effective = _effective_factor(full_width, full_height, factor)
        target_width = max(1, full_width // effective)
        target_height = max(1, full_height // effective)
        if (target_width, target_height) != (full_width, full_height):
            image = image.resize((target_width, target_height), _RESAMPLE)
        pixels = np.asarray(image, dtype=np.uint8)

    return _DownscaledFrame(
        full_width=full_width,
        full_height=full_height,
        pixels=pixels,
    )


class FrameComparer:
    """Stateful comparer that diffs candidate frames against a cached baseline.

    The baseline is decoded and downscaled once and reused across candidates, so
    a session with one keyframe and N candidates decodes the keyframe once rather
    than N times. The baseline only advances when the caller promotes a candidate
    (i.e. when finalize selects a new keyframe), matching the previous semantics
    where each frame was compared against the most recent keyframe.
    """

    def __init__(
        self,
        *,
        downscale_factor: int = DEFAULT_DOWNSCALE_FACTOR,
        pixel_tolerance: int = DEFAULT_PIXEL_TOLERANCE,
        bbox_padding: int | None = None,
    ) -> None:
        self._factor = downscale_factor
        self._tolerance = pixel_tolerance
        # Upscaling a downscaled bbox loses sub-cell precision; pad outward to
        # avoid clipping changed text. None => derive padding from the actual
        # scale (and pad nothing when the frame wasn't downscaled).
        self._padding = bbox_padding
        self._baseline: _DownscaledFrame | None = None
        self._last: _DownscaledFrame | None = None

    def set_baseline(self, source: str | Path) -> None:
        frame = _load_downscaled_frame(source, factor=self._factor)
        self._baseline = frame
        self._last = frame

    def promote_last_to_baseline(self) -> None:
        """Make the most recently compared candidate the new baseline, reusing
        its already-decoded pixels (no extra decode)."""
        if self._last is not None:
            self._baseline = self._last

    def compare(self, source: str | Path) -> ImageDiff:
        candidate = _load_downscaled_frame(source, factor=self._factor)
        self._last = candidate
        baseline = self._baseline
        if baseline is None:
            return ImageDiff(
                score=1.0,
                crop_bbox=CropBBox(
                    x=0,
                    y=0,
                    width=candidate.full_width,
                    height=candidate.full_height,
                ),
            )
        if (baseline.full_width, baseline.full_height) != (
            candidate.full_width,
            candidate.full_height,
        ):
            return ImageDiff(
                score=1.0,
                crop_bbox=CropBBox(
                    x=0,
                    y=0,
                    width=candidate.full_width,
                    height=candidate.full_height,
                ),
            )
        return self._diff(baseline, candidate)

    def _diff(
        self,
        baseline: _DownscaledFrame,
        candidate: _DownscaledFrame,
    ) -> ImageDiff:
        delta = np.abs(
            candidate.pixels.astype(np.int16) - baseline.pixels.astype(np.int16)
        )
        changed = delta.max(axis=2) >= self._tolerance
        changed_count = int(changed.sum())
        if changed_count == 0:
            return ImageDiff(score=0.0)

        score = changed_count / changed.size
        ys, xs = np.where(changed)
        bbox = self._upscale_bbox(
            min_x=int(xs.min()),
            min_y=int(ys.min()),
            max_x=int(xs.max()),
            max_y=int(ys.max()),
            down_width=changed.shape[1],
            down_height=changed.shape[0],
            full_width=candidate.full_width,
            full_height=candidate.full_height,
        )
        return ImageDiff(score=score, crop_bbox=bbox)

    def _upscale_bbox(
        self,
        *,
        min_x: int,
        min_y: int,
        max_x: int,
        max_y: int,
        down_width: int,
        down_height: int,
        full_width: int,
        full_height: int,
    ) -> CropBBox:
        scale_x = full_width / down_width
        scale_y = full_height / down_height
        if self._padding is not None:
            pad_x = pad_y = self._padding
        else:
            # Pad by one downscaled cell so quantization doesn't clip text; no
            # padding when the frame was compared at full resolution.
            pad_x = 0 if scale_x <= 1.0 else int(np.ceil(scale_x))
            pad_y = 0 if scale_y <= 1.0 else int(np.ceil(scale_y))
        # Map the inclusive downscaled bbox to full-resolution and pad outward.
        left = int(min_x * scale_x) - pad_x
        top = int(min_y * scale_y) - pad_y
        right = int((max_x + 1) * scale_x) + pad_x
        bottom = int((max_y + 1) * scale_y) + pad_y
        left = max(0, left)
        top = max(0, top)
        right = min(full_width, right)
        bottom = min(full_height, bottom)
        return CropBBox(
            x=left,
            y=top,
            width=max(1, right - left),
            height=max(1, bottom - top),
        )


def calculate_image_diff(
    previous_path: str | Path,
    current_path: str | Path,
    *,
    fallback_width: int | None = None,
    fallback_height: int | None = None,
) -> ImageDiff:
    """Compare two frames on disk. Thin wrapper over FrameComparer for callers
    that diff a single pair; finalize uses FrameComparer directly to reuse the
    baseline across many candidates."""
    comparer = FrameComparer()
    try:
        comparer.set_baseline(previous_path)
        return comparer.compare(current_path)
    except IMAGE_LOAD_ERRORS:
        return ImageDiff(
            score=1.0,
            crop_bbox=_fallback_bbox(fallback_width, fallback_height),
        )


def _normalize_hash(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized.startswith("0x"):
        return normalized[2:]
    return "".join(normalized.split())


def _binary_hash_distance(left: str, right: str) -> int | None:
    if not left or not right:
        return None
    if set(left) <= {"0", "1"} and set(right) <= {"0", "1"}:
        width = max(len(left), len(right))
        return sum(
            1
            for left_bit, right_bit in zip(left.zfill(width), right.zfill(width))
            if left_bit != right_bit
        )
    return None


def _hex_hash_distance(left: str, right: str) -> int | None:
    if not left or not right:
        return None
    try:
        left_value = int(left, 16)
        right_value = int(right, 16)
    except ValueError:
        return None

    return (left_value ^ right_value).bit_count()


def _fallback_bbox(width: int | None, height: int | None) -> CropBBox | None:
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    return CropBBox(x=0, y=0, width=width, height=height)
