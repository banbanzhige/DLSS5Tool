"""Allocation admission must not discard already accepted native output."""
from unittest import mock

import numpy as np
import pytest

from tests.test_host_reconfigure import make_live


def test_drain_copy_allocation_failure_preserves_accepted_frame():
    live = make_live()
    try:
        assert live.enqueue(np.full((6, 8, 4), 7, np.uint8))
        original = live._session
        with mock.patch('dlss5tool.host_reconfigure.np.empty_like', side_effect=MemoryError('drain destination')):
            with pytest.raises(MemoryError, match='drain destination'):
                live.resize(11, 7)
        assert live._session is original and live.pending == 1
        np.testing.assert_array_equal(live.dequeue(), np.full((6, 8, 4), 9, np.uint8))
        assert (live._w, live._h) == (8, 6)
    finally:
        live.close()


def test_invalid_configuration_is_rejected_before_draining_or_releasing_old_resources():
    live = make_live()
    try:
        original = live._session
        original._validate_configuration = True
        assert live.enqueue(np.full((6, 8, 4), 7, np.uint8))
        with mock.patch('dlss5tool.guidance_client.validate', side_effect=ValueError('unsupported combination')):
            with pytest.raises(ValueError, match='unsupported combination'):
                live.resize(11, 7, settings={'host_tiled_mode': True})
        assert live._session is original and not original._closed
        assert live.pending == 1 and not getattr(live, '_drained_outputs', ())
        np.testing.assert_array_equal(live.dequeue(), np.full((6, 8, 4), 9, np.uint8))
    finally:
        live.close()
