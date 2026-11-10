"""Handle ownership and concurrent close behavior of Windows lifetime jobs."""
import ctypes as C
import os
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from dlss5tool import process_job as jobs


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows job objects')


@pytest.fixture
def kernel():
    kernel = mock.Mock()
    kernel.CreateJobObjectW.return_value = 123
    kernel.SetInformationJobObject.return_value = 1
    kernel.AssignProcessToJobObject.return_value = 1
    kernel.CloseHandle.return_value = 1
    with mock.patch.object(jobs.C, 'WinDLL', return_value=kernel):
        yield kernel


@pytest.mark.parametrize('source', ['popen', 'sentinel'])
def test_borrowed_process_handle_is_assigned_but_never_closed(kernel, source):
    job = (jobs.KillOnCloseJob(SimpleNamespace(_handle=987)) if source == 'popen'
           else jobs.KillOnCloseJob.from_handle(987))
    kernel.CreateJobObjectW.assert_called_once_with(None, None)
    kernel.AssignProcessToJobObject.assert_called_once_with(123, 987)
    args = kernel.SetInformationJobObject.call_args.args
    limits = C.cast(args[2], C.POINTER(jobs._Extended)).contents
    assert limits.basic.flags == 0x2000
    job.close()
    job.close()
    kernel.CloseHandle.assert_called_once_with(123)


@pytest.mark.parametrize('stage', ['SetInformationJobObject', 'AssignProcessToJobObject'])
def test_failed_configuration_closes_only_new_job(kernel, stage):
    getattr(kernel, stage).return_value = 0
    with pytest.raises(OSError):
        jobs.KillOnCloseJob.from_handle(987)
    kernel.CloseHandle.assert_called_once_with(123)


def test_owner_and_exit_watcher_can_close_concurrently(kernel):
    job = jobs.KillOnCloseJob.from_handle(987)
    threads = [threading.Thread(target=job.close) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert job._handle is None
    kernel.CloseHandle.assert_called_once_with(123)


def test_failed_close_retains_handle_for_retry(kernel):
    job = jobs.KillOnCloseJob.from_handle(987)
    kernel.CloseHandle.side_effect = [0, 1]
    with pytest.raises(OSError):
        job.close()
    assert job._handle == 123
    job.close()
    assert job._handle is None
