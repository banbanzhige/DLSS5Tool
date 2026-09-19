"""Immutable SDR image sequences, exposed through a VideoCapture-like reader.

Descriptors are portable application state, not copies of the input pictures.
Every frame is checked against its import snapshot before decoding; changed
inputs must be re-imported instead of mixing stale cached and new pixels.
"""
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re

import cv2
import numpy as np

from dlss5tool.i18n import tr


EXTENSION = '.dlssseq'
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg'}


def is_sequence(source):
    return Path(source).suffix.lower() == EXTENSION


def parse_rate(value):
    try:
        rate = Fraction(str(value).strip())
        if not 1 <= rate <= 240:
            raise ValueError()
        return rate
    except (ValueError, ZeroDivisionError, TypeError):
        raise ValueError(tr('sequence.invalid_rate')) from None


def _signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _decode(path):
    frame = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if frame is None or frame.size == 0:
        raise ValueError(tr('sequence.unreadable', path=path.name))
    # No silent loss of bit depth or transparency in the SDR-only first version.
    if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError(tr('sequence.rgb_only', path=path.name))
    return frame


def discover(selected, check_cancel=lambda: None):
    selected = Path(selected).resolve(strict=True)
    match = re.fullmatch(r'(.*?)([0-9]+)([^0-9]*)', selected.stem)
    if selected.suffix.lower() not in IMAGE_EXTENSIONS or not match:
        raise ValueError(tr('sequence.numbered_required'))
    prefix, _, suffix = match.groups()
    pattern = re.compile(re.escape(prefix) + r'([0-9]+)' + re.escape(suffix))
    members = []
    for path in selected.parent.iterdir():
        check_cancel()
        if path.is_file() and path.suffix.lower() == selected.suffix.lower():
            found = pattern.fullmatch(path.stem)
            if found:
                members.append((int(found[1]), path))
    members.sort(key=lambda item: (item[0], item[1].name))
    if len(members) < 2:
        raise ValueError(tr('sequence.too_short'))
    for (a, _), (b, path) in zip(members, members[1:]):
        if b != a + 1:
            raise ValueError(tr('sequence.number_gap', path=path.name, expected=a + 1))
    return tuple(path for _, path in members)


@dataclass(frozen=True)
class ImageSequence:
    files: tuple
    signatures: tuple
    rate: Fraction
    width: int
    height: int

    @classmethod
    def scan(cls, selected, rate, check_cancel=lambda: None, progress=None):
        rate = parse_rate(rate)
        files = discover(selected, check_cancel)
        signatures = []
        shape = None
        for index, path in enumerate(files):
            check_cancel()
            signature = _signature(path)
            frame = _decode(path)
            if shape is not None and frame.shape != shape:
                raise ValueError(tr('sequence.size_mismatch', path=path.name))
            shape = frame.shape
            if _signature(path) != signature:
                raise ValueError(tr('sequence.changed', path=path.name))
            signatures.append(signature)
            if progress:
                progress(index + 1, len(files))
        check_cancel()
        return cls(files, tuple(signatures), rate, shape[1], shape[0])

    @classmethod
    def load(cls, source):
        try:
            data = json.loads(Path(source).read_text(encoding='utf-8'))
            if data['version'] != 1:
                raise ValueError()
            files = tuple(Path(p) for p in data['files'])
            signatures = tuple(tuple(s) for s in data['signatures'])
            width, height = data['width'], data['height']
            if (len(files) < 2 or len(files) != len(signatures)
                    or len(set(files)) != len(files)
                    or any(not p.is_absolute() or p.suffix.lower() not in IMAGE_EXTENSIONS for p in files)
                    or any(len(s) != 2 or any(type(v) is not int or v < 0 for v in s) for s in signatures)
                    or type(width) is not int or type(height) is not int or min(width, height) <= 0):
                raise ValueError()
            return cls(files, signatures, parse_rate(data['rate']), width, height)
        except (KeyError, TypeError, ValueError, OSError):
            raise ValueError(tr('sequence.invalid_manifest')) from None

    def save(self, directory):
        payload = json.dumps(dict(version=1, files=[str(p) for p in self.files],
            signatures=self.signatures, rate=str(self.rate), width=self.width,
            height=self.height), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:20]
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (self.files[0].stem + '-' + digest + EXTENSION)
        try:
            with target.open('x', encoding='utf-8') as handle:
                handle.write(payload)
        except FileExistsError:
            if target.read_text(encoding='utf-8') != payload:
                raise ValueError(tr('sequence.invalid_manifest'))
        return str(target)

    def check_frame(self, index):
        path = self.files[index]
        try:
            unchanged = _signature(path) == self.signatures[index]
        except OSError:
            unchanged = False
        if not unchanged:
            raise ValueError(tr('sequence.changed', path=path.name))

    def validate(self, check_cancel=lambda: None):
        for index in range(len(self.files)):
            check_cancel()
            self.check_frame(index)

    def read(self, index):
        self.check_frame(index)
        frame = _decode(self.files[index])
        if frame.shape[:2] != (self.height, self.width):
            raise ValueError(tr('sequence.size_mismatch', path=self.files[index].name))
        self.check_frame(index)
        return frame

    @property
    def metadata(self):
        return dict(width=self.width, height=self.height, frames=len(self.files),
            nb_frames=str(len(self.files)), fps=float(self.rate),
            r_frame_rate=str(self.rate), avg_frame_rate=str(self.rate),
            duration=float(len(self.files) / self.rate), is_hdr=False, profile='srgb',
            label=tr('sequence.sdr'), pixel_format='bgr24', color_primaries='bt709',
            color_transfer='iec61966-2-1', color_space='bt709', color_range='pc')


class SequenceCapture:
    def __init__(self, source):
        self.sequence = ImageSequence.load(source)
        self.position = 0
        self.opened = True

    def isOpened(self):
        return self.opened

    def get(self, prop):
        seq = self.sequence
        return {cv2.CAP_PROP_FRAME_COUNT: len(seq.files), cv2.CAP_PROP_FPS: float(seq.rate),
            cv2.CAP_PROP_FRAME_WIDTH: seq.width, cv2.CAP_PROP_FRAME_HEIGHT: seq.height,
            cv2.CAP_PROP_POS_FRAMES: self.position,
            cv2.CAP_PROP_POS_MSEC: self.position / float(seq.rate) * 1000}.get(prop, 0)

    def set(self, prop, value):
        if prop != cv2.CAP_PROP_POS_FRAMES or not math.isfinite(value):
            return False
        self.position = max(0, min(int(value), len(self.sequence.files)))
        return True

    def read(self):
        if not self.opened or self.position >= len(self.sequence.files):
            return False, None
        frame = self.sequence.read(self.position)
        self.position += 1
        return True, frame

    def release(self):
        self.opened = False


def open_capture(source):
    return SequenceCapture(source) if is_sequence(source) else cv2.VideoCapture(str(source))


def source_bytes(source):
    if is_sequence(source):
        return sum(s[0] for s in ImageSequence.load(source).signatures)
    return Path(source).stat().st_size


def audio_source(source):
    return None if is_sequence(source) else source


def output_source(source):
    """Default output lives beside the pictures, never in internal state."""
    return str(ImageSequence.load(source).files[0]) if is_sequence(source) else source
