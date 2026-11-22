"""Explicit managed-portable layout; no updater SDK or filesystem mutations."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import stat
import xml.etree.ElementTree as ET

LAYOUT_FILE = 'portable-layout.json'
MAX_METADATA = 64 * 1024
NUSPEC = '{http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd}'


class LayoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class PortableLayout:
    root: Path
    binary_root: Path
    package_id: str
    edition: str
    channel: str


def _plain_file(path):
    """Refuse redirected metadata and roots before deriving a writable data path."""
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise LayoutError(f'Managed portable path contains a link: {ancestor}')
        if ancestor != path and not stat.S_ISDIR(info.st_mode):
            raise LayoutError(f'Managed portable parent is not a directory: {ancestor}')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LayoutError(f'Managed portable metadata is not a plain file: {path}')
    return info


def _read_metadata(path):
    if _plain_file(path).st_size > MAX_METADATA:
        raise LayoutError(f'Managed portable metadata is too large: {path}')
    with path.open('rb') as source:
        raw = source.read(MAX_METADATA + 1)
    if len(raw) > MAX_METADATA:
        raise LayoutError(f'Managed portable metadata changed while reading: {path}')
    return raw


def validate_layout_config(config):
    """Validate explicit managed portable layout metadata."""
    if (not isinstance(config, dict)
            or set(config) != {'schema', 'provider', 'package_id', 'edition', 'channel'}
            or type(config['schema']) is not int or config['schema'] != 1
            or config['provider'] != 'velopack-portable'
            or not isinstance(config['package_id'], str)
            or not re.fullmatch(r'DLSS5Tool(?:\.[A-Za-z0-9_-]+)*', config['package_id'])
            or config['edition'] not in ('lite', 'full')
            or config['channel'] != 'win-' + config['edition']):
        raise LayoutError('Invalid managed portable layout marker')
    return config


def load_portable_layout(binary_root, resource_root, executable_name):
    """Return None for existing packages; an invalid explicit marker fails closed.

    Never silently fall back into current/ after an opted-in layout is damaged:
    doing so would create fresh settings/models in a directory replaced on update.
    """
    binary_root, resource_root = Path(binary_root).absolute(), Path(resource_root).absolute()
    marker = resource_root / LAYOUT_FILE
    try:
        marker.lstat()
    except FileNotFoundError:
        if (binary_root.name.lower() == 'current'
                and (binary_root.parent / '.portable').exists()
                and (binary_root.parent / 'Update.exe').exists()):
            raise LayoutError('Managed portable application is missing its layout marker')
        return None
    try:
        config = validate_layout_config(json.loads(_read_metadata(marker)))
        if binary_root.name.lower() != 'current' or resource_root != binary_root / '_internal':
            raise LayoutError('Managed portable binary/resource directories do not match')
        root = binary_root.parent
        _plain_file(root / '.portable')
        _plain_file(root / 'Update.exe')
        document = ET.fromstring(_read_metadata(binary_root / 'sq.version'))
        metadata = document.find(NUSPEC + 'metadata')
        if (document.tag != NUSPEC + 'package' or metadata is None
                or metadata.findtext(NUSPEC + 'id') != config['package_id']
                or metadata.findtext(NUSPEC + 'channel') != config['channel']
                or metadata.findtext(NUSPEC + 'mainExe') != executable_name):
            raise LayoutError('Managed portable package identity does not match its marker')
        return PortableLayout(root, binary_root, config['package_id'], config['edition'], config['channel'])
    except LayoutError:
        raise
    except (OSError, ValueError, ET.ParseError, TypeError) as error:
        raise LayoutError('Managed portable layout is incomplete or invalid; keep existing user data') from error
