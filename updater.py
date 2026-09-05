#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, dependency-free GitHub Releases updater for the portable app."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


GITHUB_REPOSITORY = "banbanzhige/DLSS5Tool"
LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
)
RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
USER_AGENT = "DLSS5Tool-Updater/1 (+https://github.com/banbanzhige/DLSS5Tool)"
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class UpdateError(RuntimeError):
    """A release could not be checked or downloaded safely."""


class DownloadCancelled(UpdateError):
    """The caller cancelled an in-progress download."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int = 0
    digest: str = ""


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    page_url: str
    body: str
    assets: tuple[ReleaseAsset, ...]


_VERSION_RE = re.compile(
    r"^[vV]?(?P<core>0|[1-9]\d*(?:\.(?:0|[1-9]\d*))*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def _parsed_version(value):
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    core = tuple(int(part) for part in match.group("core").split("."))
    prerelease = match.group("pre")
    identifiers = tuple(prerelease.split(".")) if prerelease else ()
    return core, identifiers


def _compare_prerelease(left, right):
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for a, b in zip(left, right):
        if a == b:
            continue
        a_numeric = a.isdigit()
        b_numeric = b.isdigit()
        if a_numeric and b_numeric:
            return 1 if int(a) > int(b) else -1
        if a_numeric != b_numeric:
            return -1 if a_numeric else 1
        return 1 if a > b else -1
    return (len(left) > len(right)) - (len(left) < len(right))


def compare_versions(left, right):
    """Compare two SemVer-like GitHub tags, returning -1, 0, or 1.

    Numeric components are padded, so ``v1.2`` and ``1.2.0`` compare equally.
    Invalid version strings return ``None`` instead of guessing.
    """
    parsed_left = _parsed_version(left)
    parsed_right = _parsed_version(right)
    if parsed_left is None or parsed_right is None:
        return None
    left_core, left_pre = parsed_left
    right_core, right_pre = parsed_right
    width = max(len(left_core), len(right_core))
    left_core += (0,) * (width - len(left_core))
    right_core += (0,) * (width - len(right_core))
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    return _compare_prerelease(left_pre, right_pre)


def is_newer_version(latest, current):
    comparison = compare_versions(latest, current)
    return comparison is not None and comparison > 0


def _trusted_github_url(value, *, api=False):
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except (TypeError, ValueError):
        return False
    expected_host = "api.github.com" if api else "github.com"
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == expected_host


def _read_metadata(response):
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_RELEASE_METADATA_BYTES:
                raise UpdateError("GitHub 返回的版本信息异常大")
        except ValueError:
            pass
    raw = response.read(MAX_RELEASE_METADATA_BYTES + 1)
    if len(raw) > MAX_RELEASE_METADATA_BYTES:
        raise UpdateError("GitHub 返回的版本信息异常大")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise UpdateError("GitHub 返回了无法解析的版本信息") from ex


def fetch_latest_release(timeout=8.0, opener=None):
    """Fetch the latest non-draft, non-prerelease release from GitHub."""
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            payload = _read_metadata(response)
    except urllib.error.HTTPError as ex:
        if ex.code == 403:
            raise UpdateError("GitHub 暂时限制了检查频率，请稍后再试") from ex
        if ex.code == 404:
            raise UpdateError("仓库还没有可用的正式 Release") from ex
        raise UpdateError(f"GitHub 请求失败（HTTP {ex.code}）") from ex
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise UpdateError("无法连接 GitHub，请检查网络后重试") from ex

    if not isinstance(payload, dict):
        raise UpdateError("GitHub 返回了无效的版本信息")
    tag = str(payload.get("tag_name") or "").strip()
    if _parsed_version(tag) is None:
        raise UpdateError("最新 Release 的版本号格式无效")
    page_url = str(payload.get("html_url") or RELEASES_URL)
    if not _trusted_github_url(page_url):
        page_url = RELEASES_URL
    body = str(payload.get("body") or "").strip()
    assets = []
    for item in payload.get("assets") or ():
        if not isinstance(item, dict):
            continue
        name = os.path.basename(str(item.get("name") or "").strip())
        download_url = str(item.get("browser_download_url") or "").strip()
        if not name or not _trusted_github_url(download_url):
            continue
        try:
            size = max(int(item.get("size") or 0), 0)
        except (TypeError, ValueError, OverflowError):
            size = 0
        digest = str(item.get("digest") or "").strip().lower()
        assets.append(ReleaseAsset(name, download_url, size, digest))
    return ReleaseInfo(tag, page_url, body, tuple(assets))


def select_portable_asset(release):
    """Choose the full Windows portable ZIP, never a GPU-only runtime ZIP."""
    expected = f"dlss5tool-{release.tag}-win64.zip".lower()
    for asset in release.assets:
        if asset.name.lower() == expected:
            return asset

    candidates = []
    blocked = ("30系", "40系", "50系", "rtx30", "rtx40", "rtx50")
    for asset in release.assets:
        name = asset.name.lower()
        if not name.endswith(".zip") or "dlss5tool" not in name:
            continue
        if any(marker in name for marker in blocked):
            continue
        score = 0
        score += 100 if "win64" in name else 0
        score += 60 if release.tag.lower() in name else 0
        score += 20 if ("x64" in name or "windows" in name) else 0
        candidates.append((score, asset.size, asset.name.lower(), asset))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:3], reverse=True)
    return candidates[0][-1]


def format_size(size):
    try:
        size = max(int(size), 0)
    except (TypeError, ValueError, OverflowError):
        size = 0
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0


def default_download_directory():
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    downloads = os.path.join(os.path.abspath(profile), "Downloads")
    return downloads if os.path.isdir(downloads) else os.path.abspath(profile)


def unique_download_path(directory, filename):
    directory = os.path.abspath(directory)
    filename = os.path.basename(str(filename or "").strip()) or "DLSS5Tool-update.zip"
    stem, extension = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    index = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{index}{extension}")
        index += 1
    return candidate


def _expected_sha256(digest):
    match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", str(digest or "").strip())
    return match.group(1).lower() if match else ""


def download_asset(asset, destination, progress=None, cancelled=None, timeout=30.0, opener=None):
    """Stream an asset to a temporary file, verify it, then atomically rename it."""
    if not _trusted_github_url(asset.download_url):
        raise UpdateError("下载地址不是受信任的 GitHub HTTPS 地址")
    destination = os.path.abspath(destination)
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    request = urllib.request.Request(
        asset.download_url,
        headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
    )
    open_url = opener or urllib.request.urlopen
    temporary_path = ""
    downloaded = 0
    hasher = hashlib.sha256()
    try:
        try:
            response = open_url(request, timeout=timeout)
        except urllib.error.HTTPError as ex:
            raise UpdateError(f"更新包下载失败（HTTP {ex.code}）") from ex
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise UpdateError("无法从 GitHub 下载更新包") from ex
        with response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=os.path.basename(destination) + ".",
                suffix=".part", dir=directory, delete=False,
            ) as output:
                temporary_path = output.name
                while True:
                    if cancelled is not None and cancelled():
                        raise DownloadCancelled("更新下载已取消")
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    output.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(downloaded, asset.size)
        if asset.size and downloaded != asset.size:
            raise UpdateError(
                f"更新包大小校验失败（应为 {asset.size} 字节，实际 {downloaded} 字节）"
            )
        expected_digest = _expected_sha256(asset.digest)
        actual_digest = hasher.hexdigest()
        if expected_digest and actual_digest != expected_digest:
            raise UpdateError("更新包 SHA-256 校验失败，已丢弃本次下载")
        os.replace(temporary_path, destination)
        temporary_path = ""
        return destination
    finally:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
