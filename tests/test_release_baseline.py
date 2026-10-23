"""Official release identity remains strict for published generated draft tags."""
import unittest

from scripts.verify_release_baseline import official_version


class OfficialReleaseIdentityTests(unittest.TestCase):
    def release(self, **changes):
        return {**dict(tag_name='v3.0.3', name='Release title', draft=False, prerelease=False), **changes}

    def test_version_tag_is_authoritative(self):
        self.assertEqual(official_version(self.release(name='v3.0.4')), 'v3.0.3')

    def test_published_generated_tag_requires_exact_version_title(self):
        self.assertEqual(official_version(self.release(tag_name='untagged-caac535fdc23f2a06b91', name='v3.0.3')), 'v3.0.3')
        for name in (None, '', 'v3.0.3 Release Notes', '3.0.3'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                official_version(self.release(tag_name='untagged-caac535f', name=name))

    def test_other_nonversion_tags_do_not_fall_back_to_title(self):
        for tag in ('latest', 'untagged-other', 'release-v3.0.3'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                official_version(self.release(tag_name=tag, name='v3.0.3'))

    def test_draft_and_prerelease_are_rejected(self):
        for flags in ({'draft': True}, {'prerelease': True}):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                official_version(self.release(**flags))
