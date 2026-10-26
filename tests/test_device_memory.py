"""Device admission counts selected consumers before creating native models."""
from contextlib import nullcontext
from unittest import mock

import pytest

from dlss5tool import device_memory as memory


def test_estimate_includes_hdr_nr_fg_and_encoder_reserves():
    pure = memory.estimate_vram(1920,1080,scale=2)
    hdr = memory.estimate_vram(1920,1080,scale=2,hdr=True)
    nr = memory.estimate_vram(1920,1080,scale=2,hdr=True,nr=True)
    both = memory.estimate_vram(1920,1080,scale=2,hdr=True,nr=True,multiplier=4,encoder=True)
    assert pure < hdr < nr < both < 12*1024**3


def test_insufficient_vram_queries_the_supplied_context_and_rejects():
    session = mock.Mock()
    session.current.return_value = nullcontext()
    def query(free, total):
        free._obj.value, total._obj.value = 1024, 2048
        return 0
    with mock.patch.object(memory, 'bind', return_value=query) as bind:
        with pytest.raises(MemoryError, match='selected GPU'):
            memory.require_vram(session, 4096)
    session.current.assert_called_once()
    assert bind.call_args.args[0] is session.cuda


def test_numpy_dispatch_changes_the_codebook_fingerprint():
    from numpy._core import _multiarray_umath
    from dlss5tool.analysis_quantization import cpu_fingerprint
    first = cpu_fingerprint()
    with mock.patch.object(_multiarray_umath, '__cpu_features__', {'SSE2':True}):
        assert cpu_fingerprint() != first
