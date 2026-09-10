"""Explicit opt-in for legacy internal depth-pipeline regression fixtures."""
import os
from unittest import mock


def depth_test_case(cls):
    original = cls.setUp

    def setup(self):
        patch = mock.patch.dict(os.environ, {'DLSS5TOOL_ENABLE_DEPTH': '1'})
        patch.start()
        self.addCleanup(patch.stop)
        original(self)

    cls.setUp = setup
    return cls
