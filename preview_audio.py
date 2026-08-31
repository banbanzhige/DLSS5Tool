#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preview playback audio: extract a WAV with FFmpeg, play/pause/seek via Windows MCI."""

import ctypes
import os
import subprocess
import tempfile
import threading

from video_export import _CREATE_NO_WINDOW, find_ffmpeg, probe_audio_codecs

ALIAS = "dlss5preview"
_MAX_EXTRACT_SECONDS = 20 * 60


def frame_to_ms(frame, fps):
    fps = float(fps) if fps else 30.0
    if fps <= 0:
        fps = 30.0
    return int(round(max(int(frame), 0) * 1000.0 / fps))


def ms_to_frame(ms, fps, last):
    fps = float(fps) if fps else 30.0
    if fps <= 0:
        fps = 30.0
    try:
        ms = max(int(ms), 0)
    except (TypeError, ValueError):
        ms = 0
    return max(0, min(int(ms * fps / 1000.0), max(int(last), 0)))


def _mci(command):
    if os.name != "nt":
        return -1, ""
    buf = ctypes.create_unicode_buffer(512)
    err = ctypes.windll.winmm.mciSendStringW(command, buf, 511, 0)
    return int(err), buf.value


def _mci_path(path):
    return os.path.abspath(path).replace("/", "\\").replace('"', "")


def _extract_wav(video_path, duration_sec):
    if duration_sec > _MAX_EXTRACT_SECONDS:
        raise RuntimeError("视频过长，预览不提取整段音轨")
    ffmpeg = find_ffmpeg()
    codecs = probe_audio_codecs(ffmpeg, video_path)
    if codecs == []:
        return None
    fd, wav_path = tempfile.mkstemp(prefix="dlss5_audio_", suffix=".wav")
    os.close(fd)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path, "-vn", "-ac", "2", "-ar", "44100",
        "-c:a", "pcm_s16le", wav_path,
    ]
    try:
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=120, creationflags=_CREATE_NO_WINDOW,
        )
        if (
            result.returncode != 0
            or not os.path.isfile(wav_path)
            or os.path.getsize(wav_path) < 64
        ):
            try:
                os.remove(wav_path)
            except OSError:
                pass
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or "FFmpeg 未能提取音轨")
        return wav_path
    except Exception:
        try:
            os.remove(wav_path)
        except OSError:
            pass
        raise


class PreviewAudio:
    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._wav = None
        self._ready = False
        self._has_audio = False
        self._opened = False
        self._muted = False

    @property
    def ready(self):
        return self._ready

    @property
    def has_audio(self):
        return self._has_audio and self._ready

    @property
    def muted(self):
        return self._muted

    def close(self):
        with self._lock:
            self._generation += 1
            self._mci_close_locked()
            self._ready = False
            self._has_audio = False
            wav = self._wav
            self._wav = None
        if wav:
            try:
                os.remove(wav)
            except OSError:
                pass

    def prepare(self, video_path, duration_sec, callback=None):
        """Extract audio in a background thread. callback(ok, message, generation)."""
        self.close()
        with self._lock:
            gen = self._generation

        def work():
            message = "no-audio"
            wav = None
            ok = True
            try:
                wav = _extract_wav(video_path, duration_sec)
            except Exception as ex:
                ok = False
                message = str(ex)
                wav = None
            with self._lock:
                if gen != self._generation:
                    if wav:
                        try:
                            os.remove(wav)
                        except OSError:
                            pass
                    return
                self._wav = wav
                self._has_audio = wav is not None
                self._ready = True
            if callback:
                callback(ok, message if wav is None else "ok", gen)

        threading.Thread(target=work, name="dlss-preview-audio", daemon=True).start()

    def current_generation(self):
        return self._generation

    def set_muted(self, muted):
        self._muted = bool(muted)
        if not self.has_audio:
            return
        self._ensure_open()
        volume = 0 if self._muted else 1000
        err, _ = _mci(f"setaudio {ALIAS} volume to {volume}")
        if err != 0 and self._muted:
            _mci(f"pause {ALIAS}")

    def play(self, frame, fps):
        if not self.has_audio:
            return False
        self._ensure_open()
        if not self._opened:
            return False
        if self._muted:
            _mci(f"pause {ALIAS}")
            return True
        ms = frame_to_ms(frame, fps)
        err, _ = _mci(f"play {ALIAS} from {ms}")
        return err == 0

    def pause(self):
        if not self._opened:
            return
        _mci(f"pause {ALIAS}")

    def stop(self):
        if not self._opened:
            return
        _mci(f"stop {ALIAS}")

    def mode(self):
        if not self._opened:
            return "stopped"
        err, value = _mci(f"status {ALIAS} mode")
        return value.strip().lower() if err == 0 else "stopped"

    def position_ms(self):
        if not self._opened:
            return None
        err, value = _mci(f"status {ALIAS} position")
        if err != 0:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def length_ms(self):
        if not self._opened:
            return None
        err, value = _mci(f"status {ALIAS} length")
        if err != 0:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _ensure_open(self):
        with self._lock:
            if self._opened or not self._wav:
                return
            path = _mci_path(self._wav)
            _mci(f"close {ALIAS}")
            err, _ = _mci(f'open "{path}" type waveaudio alias {ALIAS}')
            if err != 0:
                self._opened = False
                return
            _mci(f"set {ALIAS} time format milliseconds")
            self._opened = True
            if self._muted:
                _mci(f"setaudio {ALIAS} volume to 0")

    def _mci_close_locked(self):
        if self._opened or os.name == "nt":
            _mci(f"close {ALIAS}")
        self._opened = False
