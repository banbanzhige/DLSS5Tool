"""Native SDR output packing preserves pixels, alpha and input ownership."""
import ctypes
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from dlss5tool import appearance, residual_composition as rc, video_export


@pytest.mark.parametrize('order', ['rgb', 'bgr'])
@pytest.mark.parametrize('channels', [3, 4])
@pytest.mark.parametrize('strided', [False, True])
def test_native_sdr_packing_preserves_channels_and_source_alpha(order, channels, strided):
    rng = np.random.default_rng(42)
    original = rng.integers(0, 256, (7, 18 if strided else 9, channels), dtype=np.uint8)
    processed = rng.integers(0, 256, original.shape, dtype=np.uint8)
    if strided:
        original, processed = original[:, ::2], processed[:, ::2]
    before_original, before_processed = original.copy(), processed.copy()
    native_pixels = rng.integers(0, 256, (7, 9, 4), dtype=np.uint8)

    def dispatch(handle, base, nr, output, width, height, *args):
        memory = (ctypes.c_uint8 * (width*height*4)).from_address(output.value)
        np.ctypeslib.as_array(memory).reshape(height, width, 4)[:] = native_pixels
        return 1

    bridge = SimpleNamespace(handle=1, library=SimpleNamespace(dlss5_quality_compose=dispatch))
    result = rc._native_compose_frame(original, processed, appearance.preflight({}),
                                     order=order, bridge=bridge)
    expected = native_pixels[..., :channels].copy()
    if order == 'bgr':
        expected[..., :3] = native_pixels[..., 2::-1]
    if channels == 4:
        expected[..., 3] = original[..., 3]
    np.testing.assert_array_equal(result, expected)
    np.testing.assert_array_equal(original, before_original)
    np.testing.assert_array_equal(processed, before_processed)
    assert result.dtype == np.uint8 and result.flags.c_contiguous
    assert not np.shares_memory(result, original) and not np.shares_memory(result, processed)


def test_rgba_export_preserves_exact_bgr_bytes_after_composition():
    rng = np.random.default_rng(8)
    pixels = rng.integers(0, 256, (7, 9, 4), dtype=np.uint8)
    received = []
    writer = SimpleNamespace(write=lambda frame: received.append(frame.copy()))
    with (patch.object(rc, 'compose_to_gpu_writer', return_value=False),
          patch.object(video_export, 'compose_sdr_rgba', return_value=pixels)):
        video_export.write_composed_frame(writer, pixels, pixels, order='rgb')
    np.testing.assert_array_equal(received[0], pixels[..., 2::-1])
