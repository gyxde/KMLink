"""Update UI uses background work and only restarts after installer handoff."""
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QWidget
from kmlink import updater
from kmlink.update_ui import UpdatePanel


class UpdatePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.owner = QWidget()
        self.closed = []
        self.panel = UpdatePanel(self.owner, lambda: self.closed.append(True), background=False)
        self.release = SimpleNamespace(version="9.8.7", notes="<b>Release notes</b>")

    def tearDown(self):
        self.panel.shutdown()
        if self.panel._thread:
            self.panel._thread.join(timeout=2)
        self.owner.close()
        self.owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def finish(self):
        until = time.monotonic() + 3
        while self.panel._busy and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(self.panel._busy)

    def test_disabled_background_mode_does_not_start_network(self):
        self.assertFalse(self.panel._auto_timer.isActive())
        self.assertFalse(self.panel._startup_timer.isActive())
        self.assertIsNone(self.panel._thread)

    def test_check_exposes_explicit_install_action_and_plain_notes(self):
        with patch.object(updater, "check_for_update", return_value=self.release), \
                patch.object(updater, "installation_supported", return_value=True):
            self.panel.check()
            self.finish()
        self.assertEqual(self.panel._update, self.release)
        self.assertFalse(self.panel.install_button.isHidden())
        self.assertEqual(self.panel.notes.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.panel.notes.text(), "<b>Release notes</b>")
        self.assertEqual(self.closed, [])

    def test_install_handoff_precedes_shutdown(self):
        self.panel._update = self.release
        order = []
        archive, plan = object(), object()
        def download(*args, **kwargs):
            order.append("download")
            kwargs["progress"](50, 100)
            return archive
        def prepare(value):
            self.assertIs(value, archive)
            order.append("prepare")
            return plan
        def launch(value):
            self.assertIs(value, plan)
            order.append("launch")
        with patch.object(updater, "download_update", side_effect=download), \
                patch.object(updater, "installation_supported", return_value=True), \
                patch.object(updater, "prepare_install", side_effect=prepare), \
                patch.object(updater, "launch_install", side_effect=launch):
            self.panel.download()
            self.finish()
        self.assertEqual(order, ["download", "prepare", "launch"])
        self.assertEqual(self.closed, [True])

    def test_failed_installer_keeps_current_application_open(self):
        self.panel._update = self.release
        plan = object()
        with patch.object(updater, "download_update", return_value=object()), \
                patch.object(updater, "installation_supported", return_value=True), \
                patch.object(updater, "prepare_install", return_value=plan), \
                patch.object(updater, "launch_install", side_effect=updater.UpdateError("安装被取消")), \
                patch.object(updater, "discard_install") as discard:
            self.panel.download()
            self.finish()
        discard.assert_called_once_with(plan)
        self.assertEqual(self.closed, [])
        self.assertIn("安装被取消", self.panel.status.text())

    def test_cancel_download_never_prepares_or_launches_installer(self):
        self.panel._update = self.release
        started = threading.Event()
        def download(*args, **kwargs):
            started.set()
            kwargs["cancel"].wait(2)
            raise updater.UpdateCancelled()
        with patch.object(updater, "download_update", side_effect=download), \
                patch.object(updater, "prepare_install") as prepare:
            self.panel.download()
            self.assertTrue(started.wait(1))
            self.panel.cancel()
            self.finish()
        prepare.assert_not_called()
        self.assertEqual(self.closed, [])

    def test_source_mode_download_does_not_attempt_install(self):
        self.panel._update = self.release
        with patch.object(updater, "download_update", return_value=SimpleNamespace(path=Path("release.zip"))), \
                patch.object(updater, "installation_supported", return_value=False), \
                patch.object(updater, "launch_install") as launch:
            self.panel.download()
            self.finish()
        launch.assert_not_called()
        self.assertEqual(self.closed, [])
        self.assertIn("源码", self.panel.hint.text())


if __name__ == "__main__":
    unittest.main()
