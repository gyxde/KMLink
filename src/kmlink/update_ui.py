"""Local update controls. Network and installation preparation stay off the UI thread."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QSettings, QTimer, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from . import __version__, updater


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)
    stage = Signal(str)


class UpdatePanel(QFrame):
    """Only a local button can download/install; discovery messages cannot reach this API."""

    CHECK_INTERVAL = 6 * 60 * 60
    available = Signal(str)

    def __init__(self, owner, shutdown: Callable[[], None], *, background=True):
        super().__init__(owner)
        self.setObjectName("card")
        self._shutdown = shutdown
        self._closed = False
        self._busy = False
        self._update = None
        self._cancel = threading.Event()
        self._thread = None
        self._background = background
        self._settings = QSettings("KMLink", "KMLink")
        self._signals = _Signals()
        self._signals.done.connect(self._done)
        self._signals.failed.connect(self._failed)
        self._signals.progress.connect(self._progress)
        self._signals.stage.connect(self._stage)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        title = QLabel("软件更新")
        title.setObjectName("section")
        layout.addWidget(title)
        self.status = QLabel(f"当前版本 {__version__}")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.notes = QLabel("")
        self.notes.setTextFormat(Qt.TextFormat.PlainText)
        self.notes.setWordWrap(True)
        self.notes.hide()
        layout.addWidget(self.notes)
        self.automatic = QCheckBox("自动检查 GitHub 新版本")
        self.automatic.setChecked(self._settings.value("updates/automatic", True, type=bool))
        self.automatic.toggled.connect(self._automatic_changed)
        layout.addWidget(self.automatic)
        row = QHBoxLayout()
        self.check_button = QPushButton("检查更新")
        self.check_button.clicked.connect(lambda: self.check(manual=True))
        row.addWidget(self.check_button)
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.install_button = QPushButton("下载并重启安装")
        self.install_button.setObjectName("primary")
        self.install_button.clicked.connect(self.download)
        self.install_button.hide()
        layout.addWidget(self.install_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.hint = QLabel("安装时会停止共享，并在完成后重新打开软件。")
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(60 * 60 * 1000)
        self._auto_timer.timeout.connect(self._auto_check)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._auto_check)
        if background:
            self._auto_timer.start()
            self._startup_timer.start(1800)
            result = updater.consume_install_result()
            if result:
                self._startup_timer.stop()
                self.status.setText(str(result.get("message", "更新处理完成")))

    def _automatic_changed(self, value):
        if self._background:
            self._settings.setValue("updates/automatic", bool(value))
        if value and self._background:
            self._startup_timer.start(250)

    def _auto_check(self):
        if self._closed or self._busy or not self.automatic.isChecked():
            return
        last = self._settings.value("updates/last_check", 0., type=float)
        if time.time() - last >= self.CHECK_INTERVAL:
            self.check(manual=False)

    def _start(self, work):
        if self._closed or self._busy:
            return False
        self._busy = True
        self._cancel = threading.Event()
        self.check_button.setEnabled(False)
        self.install_button.setEnabled(False)
        cancel, signals = self._cancel, self._signals

        def run():
            try:
                result = work(cancel, signals)
                if not cancel.is_set() or result[0] == "install":
                    signals.done.emit(result)
                else:
                    signals.done.emit(("cancelled", None))
            except updater.UpdateCancelled:
                signals.done.emit(("cancelled", None))
            except Exception as error:
                signals.failed.emit(str(error)[:400])

        self._thread = threading.Thread(target=run, name="KMLink update", daemon=True)
        self._thread.start()
        return True

    def check(self, *, manual=True):
        if self._busy or self._closed:
            return
        self.status.setText("正在检查 GitHub 最新正式版…")
        def work(cancel, signals):
            return "check", updater.check_for_update(__version__)
        self._start(work)

    def download(self):
        if self._busy or self._closed or self._update is None:
            return
        update = self._update
        self.status.setText(f"正在下载 {update.version}…")
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()

        def work(cancel, signals):
            last_progress = 0.
            def progress(done, total):
                nonlocal last_progress
                now = time.monotonic()
                if done == total or now - last_progress >= .1:
                    last_progress = now
                    signals.progress.emit(done, total)
            archive = updater.download_update(update, progress=progress, cancel=cancel)
            if cancel.is_set():
                raise updater.UpdateCancelled()
            if not updater.installation_supported():
                return "downloaded", archive.path
            signals.stage.emit("正在校验安装文件并准备重启…")
            plan = updater.prepare_install(archive)
            if cancel.is_set():
                updater.discard_install(plan)
                raise updater.UpdateCancelled()
            try:
                updater.launch_install(plan)
            except Exception:
                updater.discard_install(plan)
                raise
            # Once the installer is launched it owns the workspace. The old
            # process must exit to release executable files on Windows.
            return "install", update.version
        self._start(work)

    def cancel(self):
        if self._busy:
            self._cancel.set()
            self.cancel_button.setEnabled(False)
            self.status.setText("正在取消…")

    def _stage(self, text):
        if not self._closed:
            self.status.setText(text)
            self.cancel_button.hide()

    def _progress(self, done, total):
        if not self._closed:
            self.progress.setValue(min(100, int(done * 100 / total)) if total else 0)

    def _idle(self):
        self._busy = False
        self.check_button.setEnabled(True)
        self.install_button.setEnabled(True)
        self.cancel_button.hide()
        self.progress.hide()

    def _done(self, result):
        if self._closed:
            return
        self._idle()
        kind, value = result
        if kind == "check":
            if self._background:
                self._settings.setValue("updates/last_check", time.time())
            self._update = value
            self.available.emit(value.version if value is not None else "")
            self.install_button.setVisible(value is not None)
            self.notes.setVisible(value is not None)
            if value is None:
                self.status.setText(f"当前 {__version__} 已是最新正式版")
            else:
                self.status.setText(f"发现新版本 {value.version}")
                self.notes.setText(value.notes[:450])
                self.install_button.setText("下载并重启安装" if updater.installation_supported() else "下载新版安装包")
        elif kind == "install":
            self.status.setText(f"正在安装 {value}，即将重新启动…")
            self._shutdown()
        elif kind == "downloaded":
            self.status.setText(f"下载已完成：{value}")
            self.hint.setText("当前为源码运行；应用内安装需要使用正式发布的程序。")
        elif kind == "cancelled":
            self.status.setText("已取消下载")

    def _failed(self, message):
        if not self._closed:
            self._idle()
            self.status.setText("更新未完成：" + message)

    def shutdown(self):
        self._closed = True
        self._cancel.set()
        self._auto_timer.stop()
        self._startup_timer.stop()
