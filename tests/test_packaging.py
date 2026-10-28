"""Packaging regressions that do not invoke a compiler or signing tool."""
import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "kmlink_build", Path(__file__).resolve().parents[1] / "scripts" / "build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class PackagingTests(unittest.TestCase):
    def test_generated_specs_are_valid_and_platform_specific(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform):
                source = build.spec_source(platform)
                compile(source, "generated.spec", "exec")
                self.assertIn('name="KMLink"', source)
                self.assertEqual("filter_windows_system_icu(a.binaries)" in source,
                                 platform == "win32")
                self.assertEqual('app = BUNDLE(' in source, platform == "darwin")
        with self.assertRaises(ValueError):
            build.spec_source("linux")

    def test_macos_bundle_version_follows_project_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text('[project]\nversion = "9.8.7"\n', encoding="utf-8")
            with patch.object(build, "ROOT", root):
                source = build.spec_source("darwin")
            tree = ast.parse(source)
            bundle = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                          and isinstance(node.value, ast.Call)
                          and isinstance(node.value.func, ast.Name) and node.value.func.id == "BUNDLE")
            plist = ast.literal_eval(next(item.value for item in bundle.keywords if item.arg == "info_plist"))
            self.assertEqual(plist["CFBundleShortVersionString"], "9.8.7")

    def test_invalid_project_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for version in ('version = 12', 'version = ""'):
                (root / "pyproject.toml").write_text('[project]\n' + version + '\n', encoding="utf-8")
                with patch.object(build, "ROOT", root), self.assertRaises(ValueError):
                    build.spec_source("darwin")

    def test_macos_reseals_outer_bundle_then_deep_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "dist" / "KMLink.app"
            binary = app / "Contents" / "MacOS" / "KMLink"
            binary.parent.mkdir(parents=True)
            binary.touch()
            notices = app / "Contents" / "Resources" / "licenses"
            with patch.object(build, "ROOT", root), patch.object(build.sys, "platform", "darwin"), \
                    patch.object(build.subprocess, "run") as run:
                build.finalize_macos_bundle(notices)
            self.assertEqual(run.call_args_list[0].args[0],
                             ["codesign", "--force", "--sign", "-", str(app)])
            self.assertEqual(run.call_args_list[1].args[0],
                             ["codesign", "--verify", "--deep", "--strict", str(app)])
            self.assertTrue(all(call.kwargs == {"check": True} for call in run.call_args_list))

    def test_signing_is_skipped_outside_macos_or_outside_app(self):
        with patch.object(build.sys, "platform", "win32"), patch.object(build.subprocess, "run") as run:
            build.finalize_macos_bundle(build.ROOT / "dist" / "KMLink" / "licenses")
            run.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, patch.object(build.sys, "platform", "darwin"), \
                patch.object(build.subprocess, "run") as run:
            build.finalize_macos_bundle(Path(directory) / "external-notices")
            run.assert_not_called()

    def test_incomplete_macos_bundle_is_not_signed(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build.sys, "platform", "darwin"), \
                patch.object(build.subprocess, "run") as run:
            root = Path(directory)
            with patch.object(build, "ROOT", root):
                with self.assertRaises(FileNotFoundError):
                    build.finalize_macos_bundle(root / "dist" / "KMLink.app" / "Contents" / "Resources" / "licenses")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
