import hashlib
import io
import json
import os
import tempfile
import unittest

import updater


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(payload)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


class VersionComparisonTests(unittest.TestCase):
    def test_numeric_tags_are_compared_without_string_ordering_bugs(self):
        self.assertEqual(updater.compare_versions("v1.10.0", "v1.9.9"), 1)
        self.assertEqual(updater.compare_versions("1.2", "v1.2.0"), 0)
        self.assertEqual(updater.compare_versions("v2.0.0", "v10.0.0"), -1)
        self.assertTrue(updater.is_newer_version("v1.1.4", "v1.1.3"))
        self.assertFalse(updater.is_newer_version("v1.1.1", "v1.1.3"))

    def test_semver_prerelease_order_and_invalid_tags(self):
        self.assertEqual(updater.compare_versions("v1.2.0", "v1.2.0-rc.1"), 1)
        self.assertEqual(updater.compare_versions("v1.2.0-rc.2", "v1.2.0-rc.10"), -1)
        self.assertIsNone(updater.compare_versions("zip", "v1.2.0"))


class ReleaseMetadataTests(unittest.TestCase):
    def test_fetch_parses_release_and_filters_untrusted_asset_urls(self):
        payload = json.dumps({
            "tag_name": "v1.2.0",
            "html_url": "https://github.com/banbanzhige/DLSS5Tool/releases/tag/v1.2.0",
            "body": "修复与改进",
            "assets": [
                {
                    "name": "DLSS5Tool-v1.2.0-win64.zip",
                    "browser_download_url": (
                        "https://github.com/banbanzhige/DLSS5Tool/releases/download/"
                        "v1.2.0/DLSS5Tool-v1.2.0-win64.zip"
                    ),
                    "size": 123,
                    "digest": "sha256:" + "a" * 64,
                },
                {
                    "name": "not-safe.zip",
                    "browser_download_url": "https://example.com/not-safe.zip",
                    "size": 10,
                },
            ],
        }).encode("utf-8")

        def opener(request, timeout):
            self.assertEqual(request.full_url, updater.LATEST_RELEASE_API)
            self.assertEqual(timeout, 3)
            return FakeResponse(payload, {"Content-Length": str(len(payload))})

        release = updater.fetch_latest_release(timeout=3, opener=opener)
        self.assertEqual(release.tag, "v1.2.0")
        self.assertEqual(release.body, "修复与改进")
        self.assertEqual(len(release.assets), 1)
        self.assertEqual(release.assets[0].size, 123)

    def test_asset_selection_prefers_full_portable_archive(self):
        release = updater.ReleaseInfo(
            "v1.2.0",
            updater.RELEASES_URL,
            "",
            (
                updater.ReleaseAsset("30系.zip", "https://github.com/a/30.zip", 900),
                updater.ReleaseAsset(
                    "DLSS5Tool-v1.2.0.zip", "https://github.com/a/app.zip", 1000
                ),
                updater.ReleaseAsset(
                    "DLSS5Tool-v1.2.0-win64.zip",
                    "https://github.com/a/win64.zip",
                    2000,
                ),
                updater.ReleaseAsset("50系.zip", "https://github.com/a/50.zip", 900),
            ),
        )
        selected = updater.select_portable_asset(release)
        self.assertEqual(selected.name, "DLSS5Tool-v1.2.0-win64.zip")

    def test_gpu_only_release_is_not_treated_as_application_update(self):
        release = updater.ReleaseInfo(
            "v1.2.0",
            updater.RELEASES_URL,
            "",
            (
                updater.ReleaseAsset("30系.zip", "https://github.com/a/30.zip"),
                updater.ReleaseAsset("50系.zip", "https://github.com/a/50.zip"),
            ),
        )
        self.assertIsNone(updater.select_portable_asset(release))


class DownloadTests(unittest.TestCase):
    def test_download_is_verified_and_atomically_published(self):
        content = (b"portable-release" * 1000) + b"done"
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        asset = updater.ReleaseAsset(
            "DLSS5Tool-v2.0.0-win64.zip",
            "https://github.com/banbanzhige/DLSS5Tool/releases/download/v2.0.0/app.zip",
            len(content),
            digest,
        )
        progress = []

        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, asset.name)
            result = updater.download_asset(
                asset,
                destination,
                progress=lambda done, total: progress.append((done, total)),
                opener=lambda _request, timeout: FakeResponse(content),
            )
            self.assertEqual(result, destination)
            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), content)
            self.assertEqual(progress[-1], (len(content), len(content)))
            self.assertEqual(os.listdir(directory), [asset.name])

    def test_failed_integrity_check_removes_partial_file(self):
        content = b"truncated"
        asset = updater.ReleaseAsset(
            "DLSS5Tool-v2.0.0-win64.zip",
            "https://github.com/banbanzhige/DLSS5Tool/releases/download/v2.0.0/app.zip",
            len(content) + 1,
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, asset.name)
            with self.assertRaises(updater.UpdateError):
                updater.download_asset(
                    asset,
                    destination,
                    opener=lambda _request, timeout: FakeResponse(content),
                )
            self.assertFalse(os.path.exists(destination))
            self.assertEqual(os.listdir(directory), [])

    def test_cancelled_download_removes_partial_file(self):
        asset = updater.ReleaseAsset(
            "DLSS5Tool-v2.0.0-win64.zip",
            "https://github.com/banbanzhige/DLSS5Tool/releases/download/v2.0.0/app.zip",
            5,
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, asset.name)
            with self.assertRaises(updater.DownloadCancelled):
                updater.download_asset(
                    asset,
                    destination,
                    cancelled=lambda: True,
                    opener=lambda _request, timeout: FakeResponse(b"hello"),
                )
            self.assertEqual(os.listdir(directory), [])

    def test_existing_download_gets_a_non_overwriting_name(self):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "DLSS5Tool-v2.0.0-win64.zip")
            open(first, "wb").close()
            candidate = updater.unique_download_path(directory, os.path.basename(first))
            self.assertEqual(
                candidate, os.path.join(directory, "DLSS5Tool-v2.0.0-win64_2.zip")
            )


if __name__ == "__main__":
    unittest.main()
