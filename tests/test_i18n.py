import json
import ast
import os
import tempfile
import unittest
from unittest import mock
from string import Formatter

import app_settings
import i18n


class LocalizationTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_language_catalogs_have_identical_nonempty_keys(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        catalogs = []
        for language in i18n.SUPPORTED_LANGUAGES:
            with open(
                os.path.join(root, "locales", f"{language}.json"), encoding="utf-8"
            ) as handle:
                catalogs.append(json.load(handle))
        self.assertEqual(set(catalogs[0]), set(catalogs[1]))
        self.assertTrue(catalogs[0])
        for catalog in catalogs:
            self.assertTrue(all(isinstance(value, str) and value for value in catalog.values()))
        formatter = Formatter()
        for key in catalogs[0]:
            placeholders = []
            for catalog in catalogs:
                placeholders.append({
                    field_name
                    for _literal, field_name, _format, _conversion
                    in formatter.parse(catalog[key])
                    if field_name is not None
                })
            self.assertEqual(placeholders[0], placeholders[1], key)

    def test_gui_translation_keys_exist_in_both_catalogs(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "gui.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        used = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "tr"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        for language in i18n.SUPPORTED_LANGUAGES:
            self.assertFalse(used - i18n.catalog_keys(language))

    def test_translation_formats_named_values_and_falls_back_safely(self):
        i18n.set_language("en-US")
        self.assertEqual(i18n.tr("view.original"), "Original")
        self.assertEqual(
            i18n.tr("status.selected_jobs", count=3), "Selected 3 jobs."
        )
        self.assertEqual(i18n.tr("missing.translation.key"), "missing.translation.key")

    def test_legacy_preview_labels_migrate_to_stable_ids(self):
        self.assertEqual(
            app_settings.validate({"preview_view": "原图"})["preview_view"],
            "original",
        )
        self.assertEqual(
            app_settings.validate({"preview_view": "DLSS"})["preview_view"],
            "dlss",
        )
        self.assertEqual(
            app_settings.validate({"preview_view": "对比"})["preview_view"],
            "compare",
        )

    def test_existing_settings_without_language_remain_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"ui_theme": "light"}, handle)
            self.assertEqual(app_settings.load(path)["ui_language"], "zh_CN")

    def test_first_run_follows_system_language(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "missing.json")
            with mock.patch.object(i18n, "system_language", return_value="en_US"):
                self.assertEqual(app_settings.load(path)["ui_language"], "en_US")

    def test_language_selection_remains_pending_until_restart(self):
        import gui

        app = gui.App.__new__(gui.App)
        app._ui_language = "zh_CN"
        app._preferred_ui_language = "zh_CN"
        app._collect_persisted_settings = lambda: {
            "ui_language": app._preferred_ui_language
        }
        app.logln = lambda _message: None
        with (
            mock.patch.object(
                gui.app_settings, "save", side_effect=lambda values: dict(values)
            ) as save,
            mock.patch.object(gui.messagebox, "showinfo") as showinfo,
        ):
            app._select_ui_language("en_US")
        self.assertEqual(app._ui_language, "zh_CN")
        self.assertEqual(app._preferred_ui_language, "en_US")
        self.assertEqual(save.call_args.args[0]["ui_language"], "en_US")
        showinfo.assert_called_once()

    def test_release_configuration_includes_both_languages(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "DLSS5Tool.spec"), encoding="utf-8") as handle:
            spec = handle.read()
        with open(os.path.join(root, "build_release.ps1"), encoding="utf-8-sig") as handle:
            release_script = handle.read()
        for name in ("zh_CN", "en_US", "README.en.md"):
            self.assertIn(name, spec)
        self.assertIn("README.en.md", release_script)


if __name__ == "__main__":
    unittest.main()
