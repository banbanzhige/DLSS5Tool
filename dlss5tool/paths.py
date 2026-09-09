"""Separate source resources, native runtimes and mutable development state."""
from pathlib import Path
import shutil
import sys


def project_root():
    return Path(__file__).resolve().parents[1]


def app_root():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else project_root()


def resource_root():
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', app_root()))
    return project_root()


def runtime_root():
    return resource_root() if getattr(sys, 'frozen', False) else project_root() / 'runtime'


def state_path(filename):
    """Keep portable state beside the EXE; adopt legacy source state once.

    Never overwrite either copy. Explicit settings/queue environment overrides
    are handled by their callers before reaching this function.
    """
    if getattr(sys, 'frozen', False):
        return app_root() / filename
    root = project_root()
    directory = root / 'var'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    legacy = root / filename
    if not target.exists() and legacy.is_file():
        try:
            with legacy.open('rb') as source, target.open('xb') as destination:
                shutil.copyfileobj(source, destination)
        except FileExistsError:
            pass
    return target
