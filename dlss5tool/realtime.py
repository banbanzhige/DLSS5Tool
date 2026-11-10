"""Control-only client for the native Magpie-based realtime renderer.

No frame pixels cross this protocol. Acknowledged commands are not reported as
successful rendering: only native state/events describe the active session.
"""
from __future__ import annotations

from copy import deepcopy
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import csv
import shutil

from dlss5tool import paths, mod_paths
from dlss5tool.i18n import tr
from dlss5tool.realtime_shortcuts import DEFAULT_HOTKEYS, validate_hotkeys, native_hotkeys

PROTOCOL = 1
NR = r"DLSSNR\DLSSNR_AI_Filter"
from dlss5tool.appearance import DEFAULT_QUALITY
DEFAULT_CONFIG = {
    "effects": [{"name": NR, "parameters": {"multiPass": 1}, "scaling_type": 0, "scale": [1, 1]}],
    "capture_method": 0, "windowed": True, "game_mode": False,
    "hdr": False, "monitor_mode": 0, "monitor_id": "", "gpu_index": -1,
    "sync_enabled": True, "sync_mode": 0, "frame_rate": 60.0,
    "capture_title_bar": False, "disable_topmost": False,
    "capture_fallback": True,
    "window_scale": 1.0,
    'gpu_priority': 0,
    'quality': deepcopy(DEFAULT_QUALITY),
}
CONFIG_KEYS = frozenset(DEFAULT_CONFIG) | {"gpu_vendor", "gpu_device"}
NR_SKIN_PAIRS = tuple((prefix + 'skinStructureStrength', prefix + 'useAutoMask')
                      for prefix in ('', 'pass2_', 'pass3_'))
NR_SWITCH_PARAMETERS = tuple(prefix + name for prefix in ('', 'pass2_', 'pass3_')
                             for name in ('intensity', 'localToneStrength', 'localStructureStrength'))
NR_RANGE_PARAMETERS = NR_SWITCH_PARAMETERS + tuple(strength for strength, _ in NR_SKIN_PAIRS)


def nr_extended_range(effect):
    """UI preference, including migration of old >1 remembered values."""
    return bool(effect.get('extended_range', False) or any(
        name in NR_RANGE_PARAMETERS and type(value) in (int, float) and math.isfinite(value) and value > 1
        for name, value in effect.get('parameters', {}).items()))


def effective_nr_skin(config):
    """Keep draft memory intact; strip UI switches and gate NR native values."""
    result = deepcopy(config)
    for effect in result['effects']:
        disabled = effect.pop('disabled_parameters', [])
        effect.pop('extended_range', None)
        if effect['name'].replace('/', '\\') != NR:
            continue
        # Programmatic configs may share a parameter dict with another effect.
        params = effect['parameters'] = deepcopy(effect.get('parameters', {}))
        for name in disabled:
            if name in NR_SWITCH_PARAMETERS and name in params:
                params[name] = 0.0
        for strength, mask in NR_SKIN_PAIRS:
            if strength in params and mask in params:
                enabled = params[mask] != 0 and params[strength] > 0
                params[mask] = int(enabled)
                if not enabled:
                    params[strength] = 0.0
    return result


def restore_nr_skin_memory(actual, draft):
    """Restore remembered UI values only where the effective pair is confirmed.

    Never persist unrelated, unapplied draft fields as confirmed session state.
    Native parameter IDs stay intact; UI-only switches stay out of actual state.
    """
    result = deepcopy(actual)
    if [(e['name'], e.get('scale', [1, 1]), e.get('scaling_type', 0)) for e in actual['effects']] != [
            (e['name'], e.get('scale', [1, 1]), e.get('scaling_type', 0)) for e in draft['effects']]:
        return result
    effective = effective_nr_skin(draft)
    for saved, remembered, expected in zip(result['effects'], draft['effects'], effective['effects']):
        if saved['name'].replace('/', '\\') != NR:
            continue
        if remembered.get('extended_range'):
            saved['extended_range'] = True
        else:
            saved.pop('extended_range', None)
        params = saved['parameters'] = deepcopy(saved.get('parameters', {}))
        raw, values = remembered.get('parameters', {}), expected.get('parameters', {})
        disabled = []
        for name in remembered.get('disabled_parameters', []):
            if name in params and name in values and math.isclose(params[name], values[name], rel_tol=0, abs_tol=1e-5):
                params[name] = raw[name]
                disabled.append(name)
        if disabled:
            saved['disabled_parameters'] = disabled
        for strength, mask in NR_SKIN_PAIRS:
            if all(n in params and n in values and math.isclose(params[n], values[n], rel_tol=0, abs_tol=1e-5)
                   for n in (strength, mask)):
                params.update({n: raw[n] for n in (strength, mask)})
    return result


def validate_config(value):
    """Whitelist presets: no executable, DLL, credential or arbitrary path fields."""
    if not isinstance(value, dict) or set(value) - CONFIG_KEYS:
        raise ValueError(tr('realtime.error.config_fields'))
    result = deepcopy(DEFAULT_CONFIG)
    # Sparse v1 presets predate the windowed default. Only brand-new sessions
    # use DEFAULT_CONFIG directly; a missing old field keeps its old meaning.
    result['windowed'] = False
    result.update(deepcopy(value))
    for key in ("windowed", "game_mode", "hdr", "sync_enabled", "capture_title_bar", "disable_topmost", "capture_fallback"):
        if type(result[key]) is not bool:
            raise ValueError(tr('realtime.error.boolean', field=key))
    for key, low, high in (("capture_method", 0, 3), ("monitor_mode", 0, 3),
                           ("sync_mode", 0, 2), ("gpu_index", -1, 63), ('gpu_priority', 0, 2)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(tr('realtime.error.invalid', field=key))
    for key in ("gpu_vendor", "gpu_device"):
        if key in result and (type(result[key]) is not int or not 0 <= result[key] <= 0xffff):
            raise ValueError(tr('realtime.error.invalid', field=key))
    rate = result["frame_rate"]
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 1000:
        raise ValueError(tr('realtime.error.rate'))
    if not isinstance(result["monitor_id"], str) or len(result["monitor_id"]) > 512:
        raise ValueError(tr('realtime.error.monitor'))
    scale = result['window_scale']
    if type(scale) not in (int, float) or not math.isfinite(scale) or not 1 <= scale <= 4:
        raise ValueError(tr('realtime.error.window_scale'))
    effects = result["effects"]
    if not isinstance(effects, list) or not 1 <= len(effects) <= 32:
        raise ValueError(tr('realtime.error.effect_count'))
    for effect in effects:
        if not isinstance(effect, dict) or set(effect) - {"name", "parameters", "scaling_type", "scale", 'disabled_parameters', 'extended_range'}:
            raise ValueError(tr('realtime.error.effect_fields'))
        name = effect.get("name", "")
        if (not isinstance(name, str) or not name or len(name) > 256 or
                any(s in name for s in ("..", ":", "\x00")) or name.startswith(("/", "\\"))):
            raise ValueError(tr('realtime.error.effect_name'))
        params = effect.get("parameters", {})
        if not isinstance(params, dict) or len(params) > 256:
            raise ValueError(tr('realtime.error.effect_parameters'))
        if 'extended_range' in effect:
            if type(effect['extended_range']) is not bool or name.replace('/', '\\') != NR:
                raise ValueError(tr('parameter.invalid_range_switch'))
            if not effect['extended_range']:
                effect.pop('extended_range')
        disabled = effect.get('disabled_parameters', [])
        if (not isinstance(disabled, list) or len(disabled) > len(NR_SWITCH_PARAMETERS)
                or any(not isinstance(n, str) or n not in NR_SWITCH_PARAMETERS or n not in params for n in disabled)
                or (disabled and name.replace('/', '\\') != NR)):
            raise ValueError(tr('parameter.invalid_switches'))
        if disabled:
            effect['disabled_parameters'] = list(dict.fromkeys(disabled))
        else:
            effect.pop('disabled_parameters', None)
        for key, number in params.items():
            if not isinstance(key, str) or len(key) > 100 or type(number) not in (int, float) or not math.isfinite(number):
                raise ValueError(tr('realtime.error.parameter_number'))
        if name.replace('/', '\\') == NR:
            # Missing fields in older presets must not enable extra passes.
            effect.setdefault('parameters', {}).setdefault('multiPass', 1)
        kind = effect.get("scaling_type", 0)
        if type(kind) is not int or not 0 <= kind <= 3:
            raise ValueError(tr('realtime.error.scaling_type'))
        scale = effect.get("scale", [1, 1])
        if (not isinstance(scale, list) or len(scale) != 2 or
                any(type(n) not in (int, float) or not math.isfinite(n) or not 0 < n <= 16384 for n in scale)):
            raise ValueError(tr('realtime.error.scale'))
    if result["windowed"] and result["game_mode"]:
        raise ValueError(tr('realtime.error.game_fullscreen'))
    if result["windowed"] and result["capture_method"] == 1:
        raise ValueError(tr('realtime.error.dd_fullscreen'))
    from dlss5tool.appearance import quality
    result['quality'] = quality(result['quality'])
    return result


def target_window_state(selected):
    """Read identity/minimization only; never focus, inject or capture a window."""
    if not selected or os.name != 'nt':
        return 'unknown'
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    hwnd = selected['hwnd']
    pid = wintypes.DWORD()
    if not user.IsWindow(hwnd) or not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value != selected['pid']:
        return 'missing'
    return 'minimized' if user.IsIconic(hwnd) else 'ready'


def default_host_path():
    return paths.runtime_root() / "realtime" / "dlss5_realtime_host.exe"


def nr_preset(config):
    """Portable NR-only subset, without scale, capture, display, SR or FG."""
    config = validate_config(config)
    effects = [e for e in config['effects'] if e['name'] == NR]
    if len(effects) != 1:
        raise ValueError(tr('realtime.error.nr_count'))
    result = {'version': 1, 'kind': 'nr-only', 'parameters': deepcopy(effects[0].get('parameters', {})),
              'quality': deepcopy(config['quality'])}
    if effects[0].get('disabled_parameters'):
        result['disabled_parameters'] = deepcopy(effects[0]['disabled_parameters'])
    if effects[0].get('extended_range'):
        result['extended_range'] = True
    return result


def portable_config(config):
    """Machine selection is local state, not a foreign preset instruction."""
    result = validate_config(config)
    for key in ('gpu_vendor', 'gpu_device'):
        result.pop(key, None)
    result.update(gpu_index=-1, monitor_id='', monitor_mode=0)
    return result


def apply_nr_preset(config, preset):
    if (not isinstance(preset, dict) or set(preset) - {'version', 'kind', 'parameters', 'quality', 'disabled_parameters', 'extended_range'}
            or not {'version', 'kind', 'parameters', 'quality'} <= set(preset)
            or preset['version'] != 1 or preset['kind'] != 'nr-only'):
        raise ValueError(tr('realtime.error.nr_invalid'))
    result = validate_config(config)
    effects = [e for e in result['effects'] if e['name'] == NR]
    if len(effects) != 1:
        raise ValueError(tr('realtime.error.nr_required'))
    effects[0]['parameters'] = deepcopy(preset['parameters'])
    effects[0].pop('disabled_parameters', None)
    if 'disabled_parameters' in preset:
        effects[0]['disabled_parameters'] = deepcopy(preset['disabled_parameters'])
    effects[0].pop('extended_range', None)
    if 'extended_range' in preset:
        effects[0]['extended_range'] = preset['extended_range']
    result['quality'] = deepcopy(preset['quality'])
    return validate_config(result)


def performance_profile(config, name):
    """Explicit user-selected scheduling; never silently reduce quality."""
    profiles = {'balanced': (60, 0), 'game': (60, 2), 'eco': (30, 2), 'quality': (0, 0)}
    if name not in profiles:
        raise ValueError(tr('realtime.error.profile'))
    result = validate_config(config)
    result['frame_rate'], result['gpu_priority'] = profiles[name]
    result['sync_enabled'] = True
    return result


def sample_gpu_load():
    """Optional read-only whole-board telemetry, not this renderer's power."""
    executable = shutil.which('nvidia-smi')
    if not executable:
        return {'available': False, 'reason': 'nvidia-smi unavailable'}
    try:
        output = subprocess.run([executable, '--query-gpu=name,uuid,utilization.gpu,memory.used,power.draw',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=3,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
        boards = []
        def number(value):
            try:
                result = float(value)
                return result if math.isfinite(result) and result >= 0 else None
            except ValueError:
                return None
        for row in csv.reader(output.stdout[:16384].splitlines()):
            if len(row) == 5:
                boards.append(dict(name=row[0].strip(), uuid=row[1].strip(),
                                   utilization=number(row[2]), memory_mib=number(row[3]), watts=number(row[4])))
        return {'available': bool(boards), 'scope': 'whole-board-not-process', 'boards': boards}
    except (OSError, subprocess.SubprocessError) as error:
        return {'available': False, 'reason': type(error).__name__}


def merge_overlay_effects(config, before, after, defaults=None):
    """Three-way merge: an overlay save cannot erase unrelated editor changes."""
    result = validate_config(config)
    current = result['effects']
    if len(current) != len(before) or len(before) != len(after):
        raise ValueError(tr('realtime.error.chain_conflict'))
    missing = object()
    def equal(left, right):
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return math.isclose(left, right, rel_tol=0, abs_tol=1e-5)
        return left == right
    for c, b, a in zip(current, before, after):
        for key, default in (('name', ''), ('scale', [1, 1]), ('scaling_type', 0)):
            if c.get(key, default) != b.get(key, default) or b.get(key, default) != a.get(key, default):
                raise ValueError(tr('realtime.error.order_conflict'))
        params = c.setdefault('parameters', {})
        for key, value in a.get('parameters', {}).items():
            fallback = (defaults or {}).get(c['name'], {}).get(key, missing)
            old = b.get('parameters', {}).get(key, fallback)
            if equal(old, value):
                continue
            existing = params.get(key, fallback)
            if not equal(existing, old) and not equal(existing, value):
                raise ValueError(tr('realtime.error.parameter_conflict', field=key))
            params[key] = value
    return validate_config(result)


def list_windows():
    """Enumerate visible top-level windows only; never capture their contents."""
    if os.name != "nt":
        return []
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    windows = []

    @callback
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        length = user.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user.GetWindowTextW(hwnd, title, len(title))
        klass = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, klass, len(klass))
        if klass.value.startswith(("Window_Magpie_", "Magpie_")):
            return True
        exe = ""
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                buffer = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buffer))
                if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    exe = buffer.value
            finally:
                kernel.CloseHandle(handle)
        windows.append({"hwnd": int(hwnd), "pid": pid.value, "title": title.value,
                        "class": klass.value, "application": exe})
        return True

    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return sorted(windows, key=lambda w: w["title"].casefold())


class PresetStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else paths.state_path("realtime_presets.json")
        self.data = {"version": 1, "presets": {}, "applications": {}}
        if self.path.exists():
            if self.path.stat().st_size > 2 * 1024**2:
                raise ValueError(tr('realtime.error.preset_size'))
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError(tr('realtime.error.preset_version'))
            if not isinstance(data.get('presets', {}), dict) or not isinstance(data.get('applications', {}), dict):
                raise ValueError(tr('realtime.error.preset_table'))
            for name, config in data.get("presets", {}).items():
                if not isinstance(name, str) or not name.strip() or len(name) > 80:
                    raise ValueError(tr('realtime.error.preset_name'))
                self.data["presets"][name] = validate_config(config)
            for app, name in data.get("applications", {}).items():
                if isinstance(app, str) and name in self.data["presets"]:
                    self.data["applications"][app] = name
            if 'session' in data:
                self.data['session'] = validate_config(data['session'])
            self.data['effect_favorites'] = self._effect_favorites(data.get('effect_favorites', []))
            local = data.get('local', {})
            if not isinstance(local, dict) or set(local) - {'runtime_directory', 'hotkeys'}:
                raise ValueError(tr('realtime.error.local_settings'))
            if 'runtime_directory' in local:
                directory = local['runtime_directory']
                if not isinstance(directory, str) or len(directory) > 32767 or not Path(directory).is_absolute():
                    raise ValueError(tr('realtime.error.runtime'))
                self.data['local'] = {'runtime_directory': directory}
            if 'hotkeys' in local:
                self.data.setdefault('local', {})['hotkeys'] = validate_hotkeys(local['hotkeys'])
            self.data['nr_presets'] = {}
            if not isinstance(data.get('nr_presets', {}), dict):
                raise ValueError(tr('realtime.error.nr_table'))
            for name, preset in data.get('nr_presets', {}).items():
                if not isinstance(name, str) or not name.strip() or len(name) > 80:
                    raise ValueError(tr('realtime.error.nr_name'))
                apply_nr_preset(DEFAULT_CONFIG, preset)
                self.data['nr_presets'][name] = preset

    def save(self, name, config, application=""):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError(tr('realtime.error.name_length'))
        config = validate_config(config)
        new = deepcopy(self.data)
        new["presets"][name.strip()] = config
        if application:
            new["applications"][str(application).casefold()] = name.strip()
        self._commit(new)

    def save_session(self, config):
        new = deepcopy(self.data)
        new['session'] = validate_config(config)
        self._commit(new)

    @staticmethod
    def _effect_favorites(names):
        if not isinstance(names, list) or len(names) > 256:
            raise ValueError(tr('realtime.workflow.invalid_favorites'))
        for name in names:
            validate_config({'effects': [{'name': name}]})
        return list(dict.fromkeys(names))

    def save_effect_favorites(self, names):
        new = deepcopy(self.data)
        new['effect_favorites'] = self._effect_favorites(names)
        self._commit(new)

    def save_runtime_directory(self, directory):
        directory = str(Path(directory).resolve(strict=True))
        if not Path(directory).is_dir():
            raise ValueError(tr('realtime.error.runtime_missing'))
        new = deepcopy(self.data)
        new.setdefault('local', {})['runtime_directory'] = directory
        self._commit(new)

    def save_hotkeys(self, bindings):
        new = deepcopy(self.data)
        new.setdefault('local', {})['hotkeys'] = validate_hotkeys(bindings)
        self._commit(new)

    def save_nr(self, name, config):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError(tr('realtime.error.nr_name'))
        new = deepcopy(self.data)
        new.setdefault('nr_presets', {})[name.strip()] = nr_preset(config)
        self._commit(new)

    def _commit(self, new):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.pending")
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(new, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.is_file():
                temporary.unlink()
        self.data = new

    def for_application(self, application):
        name = self.data["applications"].get(str(application).casefold())
        return deepcopy(self.data["presets"].get(name))


class RealtimeClient:
    def __init__(self, settings=None, host=None, *, state_root=None):
        self.settings = dict(settings or {})
        self.host = Path(host) if host else default_host_path()
        # Explicit harness isolation; never accepted from portable presets.
        self.state_root = Path(state_root).resolve() if state_root is not None else None
        self.process = None
        self.events = queue.Queue(maxsize=1024)
        self._writer = threading.Lock()
        self._sequence = 0
        self._reader = None
        self._sender = None
        self._outgoing = None
        self._safety = None
        self.generation = 0
        self.runtime_selection = None
        self.runtime_adapter = None
        self.hotkeys = dict(DEFAULT_HOTKEYS)
        self._handshake = threading.Event()
        self._handshake_event = None

    def configure_hotkeys(self, bindings):
        """Idle-only caller; verify a fresh engine, roll back on any failure."""
        bindings = validate_hotkeys(bindings)
        previous = dict(self.hotkeys)
        previous_ready = self._handshake_event or {}
        try:
            self.close()
            self.hotkeys = bindings
            self.launch()
            if not self._handshake.wait(8):
                raise RuntimeError(tr('realtime.shortcuts.timeout'))
            ready = self._handshake_event or {}
            if ready.get('hotkey_bindings') != bindings:
                raise RuntimeError(tr('realtime.shortcuts.unsupported'))
            failed = [name for name in bindings if ready.get('hotkeys', {}).get(name) is not True]
            if failed:
                from .realtime_shortcuts import ACTION_LABELS
                raise RuntimeError(tr('realtime.shortcuts.occupied', actions=', '.join(
                    tr(ACTION_LABELS[name]) + ' (' + bindings[name] + ')' for name in failed)))
        except Exception as error:
            self.hotkeys = previous
            try:
                self.close()
                self.launch()
                if not self._handshake.wait(8):
                    raise RuntimeError(tr('realtime.shortcuts.timeout'))
                restored = self._handshake_event or {}
                labels = restored.get('hotkey_bindings', DEFAULT_HOTKEYS)
                lost = [name for name, ok in previous_ready.get('hotkeys', {}).items()
                        if ok and restored.get('hotkeys', {}).get(name) is not True]
                if not restored or labels != previous or lost:
                    raise RuntimeError(tr('realtime.shortcuts.restore_unavailable'))
            except Exception as rollback_error:
                raise RuntimeError(tr('realtime.shortcuts.rollback_failed', error=rollback_error)) from error
            raise

    def _put(self, message, generation=None):
        generation = self.generation if generation is None else generation
        if generation != self.generation:
            return
        message['_generation'] = generation
        try:
            self.events.put_nowait(message)
        except queue.Full:
            # Unbounded telemetry is forbidden. A slow UI gets an explicit
            # overflow error instead of silently missing lifecycle events.
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait({"event": "error", "message": tr('realtime.error.overflow'), '_generation': generation})

    def launch(self):
        if self.process and self.process.poll() is None:
            return
        if self.process:
            self.close()
        if not self.host.is_file():
            raise FileNotFoundError(tr('realtime.error.host_missing', path=self.host))
        state = self.state_root if self.state_root is not None else paths.state_path("realtime").resolve()
        state.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        runtime = mod_paths.runtime_info(self.settings, adapter=self.runtime_adapter)
        feature_root = self.settings.get('realtime_runtime_directory') or paths.runtime_root()
        environment["DLSS5_REALTIME_RUNTIME_ROOT"] = str(Path(feature_root).resolve())
        environment['DLSS5_REALTIME_NR_PATH'] = str(Path(runtime['path']).resolve())
        environment["DLSS5_REALTIME_EFFECTS_ROOT"] = str((self.host.parent / "effects").resolve())
        environment['DLSS5_REALTIME_STATE_ROOT'] = str(state)
        bindings = validate_hotkeys(self.hotkeys)
        environment['DLSS5_REALTIME_HOTKEYS'] = json.dumps(native_hotkeys(bindings))
        self.runtime_selection = {**runtime,
                                  'feature_directory': str(Path(feature_root).resolve()),
                                  'meaning': 'paths passed to host, not proof of loaded modules'}
        self.generation += 1
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        generation = self.generation
        self._handshake_event = None
        self._handshake.clear()
        from .realtime_safety import RealtimeSafety
        outgoing = self._outgoing = queue.Queue(maxsize=64)
        def emergency_stop():
            try:
                outgoing.put_nowait('{"command":"stop"}\n')
            except queue.Full:
                pass  # Safety deadline is already armed, independently of IO.
        safety = self._safety = RealtimeSafety(emergency_stop, lambda event: self._put(event, generation),
                                             hotkey=bindings['stop'])
        try:
            safety.start()
            environment['DLSS5_REALTIME_SUPERVISED_STOP'] = '1' if safety.registered else '0'
            process = subprocess.Popen([str(self.host.resolve())], cwd=state, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except BaseException:
            safety.close()
            raise
        self.process = process
        safety.bind(process)

        def write():
            while process.poll() is None:
                try:
                    line = outgoing.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    safety.writing(True)
                    process.stdin.write(line)
                    process.stdin.flush()
                except (OSError, ValueError) as error:
                    safety.request_stop(quitting=True)
                    self._put({'event': 'fatal', 'message': str(error)}, generation)
                    break
                finally:
                    safety.writing(False)
        self._sender = threading.Thread(target=write, daemon=True, name='realtime-writer')
        self._sender.start()

        def read():
            try:
                while True:
                    line = process.stdout.readline(1024**2 + 1)
                    if not line:
                        break
                    if len(line) > 1024**2:
                        self._put({'event': 'fatal', 'message': tr('realtime.error.protocol_size')}, generation)
                        process.terminate()
                        break
                    if generation != self.generation:
                        break
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("event is not an object")
                    except (ValueError, TypeError):
                        event = {"event": "log", "message": line.rstrip()[:8192]}
                    if event.get('event') == 'ready' and safety.registered:
                        event['stop_hotkey_registered'] = event['independent_stop'] = True
                        event.setdefault('hotkeys', {})['stop'] = True
                    if (event.get('event') == 'ready' and bindings != DEFAULT_HOTKEYS
                            and event.get('hotkey_bindings') != bindings):
                        # Old engines ignore the environment. Never claim a custom
                        # chord works or allow fullscreen with misleading guidance.
                        self._put({'event': 'fatal', 'message': tr('realtime.shortcuts.unsupported')}, generation)
                        self._handshake.set()
                        process.terminate()
                        break
                    if event.get('event') == 'ready':
                        self._handshake_event = event
                        self._handshake.set()
                    safety.observe(event)
                    self._put(event, generation)
            finally:
                if generation == self.generation:
                    self._handshake.set()
                    self._put({"event": "exited", "returncode": process.wait()}, generation)
        self._reader = threading.Thread(target=read, daemon=True, name="realtime-control")
        self._reader.start()

    def send(self, command, **fields):
        if command in ('catalog', 'devices', 'start'):
            self.launch()
        elif not self.process or self.process.poll() is not None:
            raise RuntimeError(tr('realtime.error.host_stopped'))
        with self._writer:
            self._sequence += 1
            value = dict(fields, command=command, id=self._sequence)
            line = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(line.encode("utf-8")) >= 1024**2:
                raise ValueError(tr('realtime.error.command_size'))
            if command in ('stop', 'quit') and self._safety:
                self._safety.request_stop(quitting=command == 'quit')
            if command == 'start' and self._safety:
                self._safety.starting()
            try:
                self._outgoing.put_nowait(line + "\n")
            except queue.Full:
                if self._safety:
                    self._safety.request_stop(quitting=True)
                raise RuntimeError(tr('realtime.error.command_queue')) from None
            return self._sequence

    def start(self, hwnd, config, pid=None):
        if type(hwnd) is not int or hwnd <= 0:
            raise ValueError(tr('realtime.error.target'))
        fields = {'hwnd': hwnd, 'config': effective_nr_skin(validate_config(config))}
        if pid is not None:
            fields['pid'] = pid
        self.launch()
        if os.name == 'nt':
            user = ctypes.WinDLL('user32', use_last_error=True)
            user.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
            user.AllowSetForegroundWindow(self.process.pid)
        fields['activate'] = True
        return self.send('start', **fields)

    def close(self):
        process = self.process
        if not process:
            return
        if process.poll() is None:
            try:
                self.send('quit')
                process.wait(timeout=3)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
        if self._safety:
            self._safety.close()
        if self._sender:
            self._sender.join(timeout=1)
        if self._reader:
            self._reader.join(timeout=1)
        for pipe, worker in ((process.stdin, self._sender), (process.stdout, self._reader)):
            # Never wait on a TextIO lock still owned by blocked IO. The safety
            # thread has already terminated and waited for this exact process.
            if pipe and not pipe.closed and (worker is None or not worker.is_alive()):
                pipe.close()
        self.process = None
