"""Update origin, archive and transaction tests; no real installation is changed."""
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from kmlink import updater as u
from kmlink import update_install as install


def release(version="1.2.3", payload=b"zip"):
    name = u.ASSETS["windows-x64"]
    digest = hashlib.sha256(payload).hexdigest()
    return {"tag_name": "v" + version, "draft": False, "prerelease": False, "body": "New version",
            "assets": [{"name": name, "size": len(payload), "state": "uploaded",
                        "browser_download_url": u._release_url("v" + version, name), "digest": "sha256:" + digest},
                       {"name": "SHA256SUMS.txt", "size": 100, "state": "uploaded",
                        "browser_download_url": u._release_url("v" + version, "SHA256SUMS.txt")}]}


def update(payload=b"zip", version="1.2.3", target="windows-x64"):
    tag = "v" + version
    name = u.ASSETS[target]
    return u.ReleaseUpdate(version, tag, "notes", f"https://github.com/gyxde/KMLink/releases/tag/{tag}",
                           name, u._release_url(tag, name), len(payload), hashlib.sha256(payload).hexdigest(),
                           u._release_url(tag, "SHA256SUMS.txt"))


def write_payload(root, version, target="windows-x64"):
    manifest = {"schema": 1, "app": "KMLink", "version": version, "platform": target}
    if target == "windows-x64":
        root.mkdir(parents=True)
        (root / "KMLink.exe").write_bytes(b"MZ " + version.encode())
        (root / "_internal").mkdir()
        (root / "_internal" / "runtime.dll").write_bytes(b"runtime")
        (root / "update-manifest.json").write_text(json.dumps(manifest))
    else:
        (root / "Contents" / "MacOS").mkdir(parents=True)
        (root / "Contents" / "MacOS" / "KMLink").write_bytes(b"program " + version.encode())
        (root / "Contents" / "Resources").mkdir()
        (root / "Contents" / "Resources" / "update-manifest.json").write_text(json.dumps(manifest))
        (root / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.kmlink.desktop", "CFBundleShortVersionString": version}))


def make_archive(folder, version="1.2.3", target="windows-x64", additions=()):
    package = folder / ("KMLink" if target == "windows-x64" else "KMLink.app")
    write_payload(package, version, target)
    path = folder / u.ASSETS[target]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in package.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(folder).as_posix())
        for name, content in additions:
            archive.writestr(name, content)
    value = update(path.read_bytes(), version, target)
    return u.VerifiedArchive(value, path, value.sha256)


class ReleaseTests(unittest.TestCase):
    def test_version_order_is_numeric_and_stable(self):
        self.assertGreater(u.version_tuple("v1.10.0"), u.version_tuple("1.9.9"))
        for value in ("1.2", "01.2.3", "1.2.3-rc1", "1.2.3/evil", 1):
            with self.subTest(value=value), self.assertRaises(u.UpdateError):
                u.version_tuple(value)

    def test_check_selects_exact_platform_and_ignores_current_or_older(self):
        with patch.object(u, "_read_limited", return_value=json.dumps(release()).encode()):
            self.assertEqual(u.check_for_update("1.2.2", "windows-x64").version, "1.2.3")
            self.assertIsNone(u.check_for_update("1.2.3", "windows-x64"))
            self.assertIsNone(u.check_for_update("1.3.0", "windows-x64"))

    def test_release_rejects_foreign_urls_missing_duplicate_or_invalid_assets(self):
        variants = []
        data = release(); data["assets"][0]["browser_download_url"] = "https://example.com/KMLink.zip"; variants.append(data)
        data = release(); data["assets"].pop(); variants.append(data)
        data = release(); data["assets"].append(data["assets"][0]); variants.append(data)
        data = release(); data["assets"][0]["size"] = True; variants.append(data)
        data = release(); data["prerelease"] = True; variants.append(data)
        for data in variants:
            with self.subTest(data=data), patch.object(u, "_read_limited", return_value=json.dumps(data).encode()), self.assertRaises(u.UpdateError):
                u.check_for_update("1.0.0", "windows-x64")

    def test_origin_checks_reject_http_and_arbitrary_redirect_hosts(self):
        for url in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x", "https://user@github.com/x", "https://github.com:444/x", "https://github.com:bad/x"):
            with self.subTest(url=url), self.assertRaises(u.UpdateError):
                u._validate_redirect(url)

    def test_download_requires_matching_digest_and_reports_progress(self):
        payload = b"payload" * 100000
        value = update(payload)
        progress = []
        with tempfile.TemporaryDirectory() as directory, patch.object(u, "_read_limited", return_value=f"{value.sha256}  {value.asset_name}\n".encode()), patch.object(u, "_open", return_value=io.BytesIO(payload)):
            result = u.download_update(value, cache_dir=Path(directory), progress=lambda n, total: progress.append((n, total)))
            self.assertEqual(result.path.read_bytes(), payload)
            self.assertEqual(progress[-1], (len(payload), len(payload)))
            self.assertEqual(result.sha256, value.sha256)

    def test_corrupt_or_cancelled_download_removes_only_its_workspace(self):
        value = update(b"correct")
        for cancel in (None, threading.Event()):
            if cancel is not None:
                cancel.set()
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); keep = root / "keep.txt"; keep.write_text("keep")
                with patch.object(u, "_checksum", return_value=value.sha256), patch.object(u, "_open", return_value=io.BytesIO(b"wrong!!")), self.assertRaises(u.UpdateError):
                    u.download_update(value, cache_dir=root, cancel=cancel)
                self.assertEqual(list(root.iterdir()), [keep])


class ArchiveTests(unittest.TestCase):
    def test_extraction_uses_verified_snapshot_if_cache_changes_after_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root / "source")
            zip_open = zipfile.ZipFile
            def changed_after_read(source, *args, **kwargs):
                archive.path.write_bytes(b"changed after verification")
                return zip_open(source, *args, **kwargs)
            with patch.object(u.zipfile, "ZipFile", side_effect=changed_after_read):
                output = u.extract_archive(archive, root / "output")
            self.assertEqual((output / "KMLink.exe").read_bytes(), b"MZ 1.2.3")

    def test_real_zip_extracts_and_validates_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = make_archive(root / "source")
            output = u.extract_archive(archive, root / "output")
            self.assertTrue((output / "KMLink.exe").is_file())
            self.assertEqual(u.validate_payload(output, "windows-x64", "1.2.3"), output / "KMLink.exe")

    def test_rejects_path_traversal_foreign_root_and_ntfs_stream(self):
        for name in ("KMLink/../../escape", "/KMLink/escape", "C:/escape", "KMLink\\escape", "KMLink/a:stream", "other/file", "KMLink/.. /escape", "KMLink/CON.txt"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); archive = make_archive(root / "source", additions=[(name, b"bad")])
                if "\\" in name:
                    # Python's Windows ZIP writer normalizes separators. A
                    # downloaded ZIP can still contain raw backslash names.
                    archive.path.write_bytes(archive.path.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
                    archive = u.VerifiedArchive(update(archive.path.read_bytes()), archive.path, u.file_sha256(archive.path))
                with self.assertRaises(u.UpdateError):
                    u.extract_archive(archive, root / "output")
                self.assertFalse((root / "output").exists())

    def test_rejects_changed_archive_and_mismatched_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = make_archive(root / "source")
            archive.path.write_bytes(archive.path.read_bytes() + b"tampered")
            with self.assertRaises(u.UpdateError): u.extract_archive(archive, root / "out")
            with self.assertRaises(u.UpdateError): u.validate_payload(root / "source" / "KMLink", "windows-x64", "9.0.0")

    @unittest.skipIf(os.name == "nt", "Unix framework symlinks require macOS/Unix filesystem semantics")
    def test_mac_framework_links_stay_inside_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = make_archive(root / "source", target="macos-arm64")
            with zipfile.ZipFile(archive.path, "a") as package:
                package.writestr("KMLink.app/Contents/Frameworks/Versions/A/lib", b"library")
                info = zipfile.ZipInfo("KMLink.app/Contents/Frameworks/Versions/Current")
                info.create_system = 3; info.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(info, "A")
                package.writestr("__MACOSX/._KMLink.app", b"metadata")
            archive = u.VerifiedArchive(update(archive.path.read_bytes(), target="macos-arm64"), archive.path, u.file_sha256(archive.path))
            output = u.extract_archive(archive, root / "output")
            self.assertEqual((output / "Contents/Frameworks/Versions/Current/lib").read_bytes(), b"library")

    def test_windows_zip_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = make_archive(root / "source")
            with zipfile.ZipFile(archive.path, "a") as package:
                info = zipfile.ZipInfo("KMLink/link"); info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(info, "../outside")
            archive = u.VerifiedArchive(update(archive.path.read_bytes()), archive.path, u.file_sha256(archive.path))
            with self.assertRaises(u.UpdateError): u.extract_archive(archive, root / "output")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "installed" / "KMLink"
        write_payload(self.target, "1.0.0")
        self.archive = make_archive(self.root / "source")
        self.cache = self.root / "cache"
        self.patches = [patch.object(install, "installation_supported", return_value=True),
                        patch.object(install, "_current_root", return_value=self.target),
                        patch.object(install, "platform_key", return_value="windows-x64"),
                        patch.object(install, "update_cache_dir", return_value=self.cache),
                        patch.object(install, "_schedule_completed_cleanup"),
                        patch.object(install, "_checksum", return_value=self.archive.sha256)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_prepare_copies_complete_helper_and_allows_unlaunched_discard(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        data = install._load_plan(plan.path)
        self.assertTrue((Path(data["helper"]) / "_internal/runtime.dll").is_file())
        self.assertEqual(data["old_version"], "1.0.0")
        install.discard_install(plan)
        self.assertFalse(plan.workspace.exists())
        self.assertTrue((self.target / "KMLink.exe").is_file())

    def test_actual_directory_transaction_and_rollback_preserve_old_version(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        data = install._load_plan(plan.path)
        # Mutating the previously extracted preview must not change installation.
        (install._payload(data) / "KMLink.exe").write_bytes(b"tampered preview")
        install._transaction(data)
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.2.3")
        self.assertEqual(install._paths(data)[2].joinpath("KMLink.exe").read_bytes(), b"MZ 1.0.0")
        install._rollback(data)
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.0.0")

    def test_helper_smoke_failure_rolls_back_and_restarts_original(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        with patch.object(install, "_wait_for_parent"), patch.object(install, "_smoke_check", side_effect=u.UpdateError("smoke failed")), patch.object(install, "_spawn") as spawn:
            self.assertEqual(install.run_installer_cli(["--apply-update", str(plan.path)]), 1)
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.0.0")
        self.assertEqual(spawn.call_args.args[0], [str(self.target / "KMLink.exe")])
        self.assertFalse(install.consume_install_result()["success"])

    def test_failed_replacement_restores_original_directory(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        data = install._load_plan(plan.path)
        candidate = install._paths(data)[1]
        original_replace = Path.replace
        def fail_candidate(path, destination):
            if path == candidate:
                raise PermissionError("replacement denied")
            return original_replace(path, destination)
        with patch.object(Path, "replace", fail_candidate), self.assertRaises(u.UpdateError):
            install._transaction(data)
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.0.0")

    def test_cancelled_admin_authorization_keeps_original_and_reopens_normally(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        with patch.object(install, "_wait_for_parent"), patch.object(install, "_writable_parent", return_value=False), \
                patch.object(install, "_elevate_windows", side_effect=u.UpdateError("authorization cancelled")), \
                patch.object(install, "_spawn") as spawn:
            self.assertEqual(install.run_installer_cli(["--apply-update", str(plan.path)]), 1)
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.0.0")
        self.assertEqual(spawn.call_args.args[0], [str(self.target / "KMLink.exe")])

    def test_helper_success_checks_startup_before_launch_and_retains_backup(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        events = []
        with patch.object(install, "_wait_for_parent", side_effect=lambda _: events.append("exit")), patch.object(install, "_smoke_check", side_effect=lambda _: events.append("smoke")), patch.object(install, "_spawn", side_effect=lambda _: events.append("launch")):
            self.assertEqual(install.run_installer_cli(["--apply-update", str(plan.path)]), 0)
        self.assertEqual(events, ["exit", "smoke", "launch"])
        self.assertTrue(install.consume_install_result()["success"])
        self.assertIsNone(install.consume_install_result())

    def test_result_write_failure_does_not_undo_successful_install_or_skip_launch(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        data = install._load_plan(plan.path)
        with patch.object(install, "_wait_for_parent"), patch.object(install, "_smoke_check"), \
                patch.object(install, "_write_result", side_effect=OSError("cache full")), \
                patch.object(install, "_spawn") as spawn:
            self.assertEqual(install.run_installer_cli(["--apply-update", str(plan.path)]), 0)
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.2.3")
        self.assertEqual(install._paths(data)[2].joinpath("KMLink.exe").read_bytes(), b"MZ 1.0.0")
        self.assertEqual(spawn.call_args.args[0], [str(self.target / "KMLink.exe")])

    def test_failed_update_reopens_restored_app_even_when_result_cannot_be_written(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        with patch.object(install, "_wait_for_parent"), \
                patch.object(install, "_smoke_check", side_effect=u.UpdateError("bad startup")), \
                patch.object(install, "_write_result", side_effect=OSError("cache full")), \
                patch.object(install, "_spawn") as spawn:
            self.assertEqual(install.run_installer_cli(["--apply-update", str(plan.path)]), 1)
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.0.0")
        self.assertEqual(spawn.call_args.args[0], [str(self.target / "KMLink.exe")])

    def test_candidate_validation_failure_cleans_only_created_staging_directories(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        data = install._load_plan(plan.path)
        candidate_path = install._paths(data)[1]
        original_validate = install.validate_payload
        def reject_candidate(root, platform, version):
            if root == candidate_path:
                raise u.UpdateError("invalid candidate")
            return original_validate(root, platform, version)
        with patch.object(install, "validate_payload", side_effect=reject_candidate), self.assertRaises(u.UpdateError):
            install._transaction(data)
        target, candidate, backup, _ = install._paths(data)
        self.assertFalse(candidate.exists())
        self.assertFalse(backup.exists())
        self.assertFalse((target.parent / f".{target.name}.extract-{data['token']}").exists())
        self.assertEqual((target / "KMLink.exe").read_bytes(), b"MZ 1.0.0")

    def test_launch_is_separate_and_does_not_reuse_old_frozen_runtime(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        with patch.object(install, "_spawn") as spawn:
            install.launch_install(plan)
            command = spawn.call_args.args[0]
            self.assertNotEqual(Path(command[0]).parent, self.target)
            self.assertEqual(command[1:], ["--apply-update", str(plan.path)])
            with self.assertRaises(u.UpdateError): install.launch_install(plan)
        install.discard_install(plan)
        self.assertTrue(plan.workspace.exists())
        with patch.dict(os.environ, {"_PYI_APPLICATION_HOME_DIR": "old", "_MEIPASS2": "old"}):
            env = install._fresh_environment()
            self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)
            self.assertNotIn("_MEIPASS2", env)
            self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_elevated_helper_does_not_launch_gui(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        with patch.object(install, "_spawn") as spawn, patch.object(install, "_wait_for_parent") as wait:
            self.assertEqual(install.run_installer_cli(["--apply-update", str(plan.path), "--elevated", "install"]), 0)
            spawn.assert_not_called(); wait.assert_not_called()
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.2.3")

    def test_changed_archive_or_installation_is_rejected_without_replacement(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        data = install._load_plan(plan.path)
        Path(data["archive"]).write_bytes(b"changed")
        with self.assertRaises(u.UpdateError): install._load_plan(plan.path)
        self.assertEqual((self.target / "KMLink.exe").read_bytes(), b"MZ 1.0.0")

    def test_source_mode_cannot_install(self):
        with patch.object(install, "installation_supported", return_value=False), self.assertRaises(u.UpdateError):
            install.prepare_install(self.archive)

    def test_completed_cleanup_removes_only_own_cache_after_helper_exits(self):
        plan = install.prepare_install(self.archive, parent_pid=12345)
        download = self.cache / "download-test"
        download.mkdir(); (download / "archive.zip").write_bytes(b"archive")
        (plan.workspace / "finished.json").write_text(json.dumps({"helper_pid": 123456, "download_dir": str(download)}))
        with patch.object(install, "_process_alive", return_value=True):
            install._cleanup_completed(plan.workspace, timeout=0)
        self.assertTrue(plan.workspace.exists())
        with patch.object(install, "_process_alive", return_value=False):
            install._cleanup_completed(plan.workspace, timeout=0)
        self.assertFalse(plan.workspace.exists())
        self.assertFalse(download.exists())
        self.assertTrue((self.target / "KMLink.exe").is_file())

    def test_completed_cleanup_rejects_outside_cache(self):
        outside = self.root / "install-user-documents"
        outside.mkdir(); (outside / "important.txt").write_text("keep")
        (outside / "finished.json").write_text(json.dumps({"helper_pid": 123456, "download_dir": str(self.target.parent)}))
        with patch.object(install, "_process_alive", return_value=False):
            install._cleanup_completed(outside, timeout=0)
        self.assertTrue((outside / "important.txt").is_file())


if __name__ == "__main__":
    unittest.main()
