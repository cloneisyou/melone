import numpy as np
from PIL import Image

from melone_service.pipeline.image_diff import (
    MIN_SOURCE_DIM_FOR_DOWNSCALE,
    FrameComparer,
    calculate_image_diff,
)


def _save(path, array):
    Image.fromarray(array, "RGBA").save(path)
    return path


def _solid(width, height, color=(0, 0, 0, 255)):
    array = np.empty((height, width, 4), dtype=np.uint8)
    array[:, :] = color
    return array


def test_identical_large_frames_short_circuit_to_zero(tmp_path):
    size = MIN_SOURCE_DIM_FOR_DOWNSCALE * 2  # forces the downscale path
    array = _solid(size, size)
    a = _save(tmp_path / "a.png", array)
    b = _save(tmp_path / "b.png", array.copy())

    diff = calculate_image_diff(a, b)

    assert diff.score == 0.0
    assert diff.crop_bbox is None


def test_large_frame_change_is_detected_and_bbox_covers_region(tmp_path):
    size = MIN_SOURCE_DIM_FOR_DOWNSCALE * 2
    base = _solid(size, size)
    changed = base.copy()
    # Paint a quarter-sized white block in the lower-right region.
    y0, x0 = size // 2, size // 2
    changed[y0:y0 + size // 4, x0:x0 + size // 4] = (255, 255, 255, 255)

    a = _save(tmp_path / "a.png", base)
    b = _save(tmp_path / "b.png", changed)

    diff = calculate_image_diff(a, b)

    # ~1/16 of pixels changed; well above the crop threshold, below keyframe.
    assert 0.0 < diff.score < 0.20
    assert diff.crop_bbox is not None
    bbox = diff.crop_bbox
    # The (downscaled, padded) bbox should enclose the painted region.
    assert bbox.x <= x0 and bbox.y <= y0
    assert bbox.x + bbox.width >= x0 + size // 4
    assert bbox.y + bbox.height >= y0 + size // 4


def test_small_frames_skip_downscale_and_keep_exact_bbox(tmp_path):
    base = _solid(10, 10)
    changed = base.copy()
    changed[4:6, 4:6] = (255, 255, 255, 255)  # exact 2x2 block at (4, 4)

    a = _save(tmp_path / "a.png", base)
    b = _save(tmp_path / "b.png", changed)

    diff = calculate_image_diff(a, b)

    assert diff.score == 0.04
    assert diff.crop_bbox is not None
    assert (diff.crop_bbox.x, diff.crop_bbox.y) == (4, 4)
    assert (diff.crop_bbox.width, diff.crop_bbox.height) == (2, 2)


def test_baseline_only_advances_on_promote(tmp_path):
    base = _solid(10, 10)
    moved = base.copy()
    moved[0:2, 0:2] = (255, 0, 0, 255)
    far = base.copy()
    far[8:10, 8:10] = (0, 255, 0, 255)

    base_path = _save(tmp_path / "base.png", base)
    moved_path = _save(tmp_path / "moved.png", moved)
    far_path = _save(tmp_path / "far.png", far)

    comparer = FrameComparer()
    comparer.set_baseline(base_path)

    first = comparer.compare(moved_path)
    # Without promotion the baseline stays on `base`, so comparing `far` diffs
    # against `base`, not against `moved`.
    second = comparer.compare(far_path)

    assert first.score > 0.0
    assert second.crop_bbox is not None
    # `far`'s change is in the bottom-right; baseline is still `base`.
    assert second.crop_bbox.x >= 8 and second.crop_bbox.y >= 8
