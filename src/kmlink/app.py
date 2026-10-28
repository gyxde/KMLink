"""Small native desktop UI. Network and OS hooks communicate through queues."""
from __future__ import annotations

import ctypes
import platform
import queue
import socket
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFrame, QHeaderView, QHBoxLayout, QLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from . import __version__
from .engine import Bounds, Engine
from .geometry import query_desktop_layout
from .discovery import Discovery
from .native import NativeInput, enable_dpi_awareness, permissions_status, validate_key_code
from .network import local_ipv4_addresses
from .pairing import PairingService
from .transport import generate_pairing_key, normalize_pairing_key


STYLE = """
QMainWindow, QWidget#root { background: #f4f6f8; color: #172636; }
QWidget { font-family: 'Segoe UI', 'Microsoft YaHei UI', 'PingFang SC'; font-size: 14px; color: #172636; }
QFrame#card { background: white; border: 1px solid #e2e7ec; border-radius: 14px; }
QLabel#eyebrow { color: #267e78; font-weight: 700; font-size: 12px; }
QLabel#title { font-size: 28px; font-weight: 700; }
QLabel#muted { color: #687785; }
QLabel#section { font-size: 17px; font-weight: 650; }
QLabel#device { font-size: 19px; font-weight: 650; }
QLabel#status { background: #e5eef0; color: #37616a; border-radius: 12px; padding: 6px 12px; }
QLabel#step { color: #267e78; background: #e5f4f0; border-radius: 11px; padding: 4px 9px; font-weight: 700; }
QLineEdit, QSpinBox, QComboBox { background: #f8fafb; border: 1px solid #d9e2e8; border-radius: 7px; padding: 9px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #267e78; }
QPushButton { background: white; border: 1px solid #d5dfe5; border-radius: 7px; padding: 9px 15px; min-height: 20px; }
QPushButton:hover { background: #eff5f5; border-color: #80aaa5; }
QPushButton#primary { background: #167c71; border: none; color: white; font-weight: 650; padding: 11px 20px; }
QPushButton#primary:hover { background: #106b61; }
QPushButton:disabled { background: #e9eef1; color: #8a98a4; border-color: #e0e6eb; }
QPushButton#primary:disabled { background: #e9eef1; color: #8a98a4; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #edf1f4; color: #62717e; border-radius: 7px; padding: 10px 27px; margin-right: 7px; margin-bottom: 13px; }
QTabBar::tab:selected { background: #ddf0eb; color: #166e63; font-weight: 650; }
QTextEdit { background: #f8fafb; border: 1px solid #e2e7ec; border-radius: 7px; font-size: 12px; color: #657584; padding: 6px; }
QTableWidget { background: #f8fafb; border: 1px solid #e2e7ec; border-radius: 7px; gridline-color: #edf1f4; selection-background-color: #dbefe9; selection-color: #155e54; }
QHeaderView::section { background: #edf2f4; border: none; padding: 8px 5px; color: #637480; }
"""


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def card():
    widget = QFrame()
    widget.setObjectName("card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(10)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    return widget, layout


def local_addresses():
    return local_ipv4_addresses()


def system_name():
    return "macOS" if platform.system() == "Darwin" else platform.system()


def primary_bounds():
    """Backward-compatible name: the usable desktop now includes every screen."""
    return query_desktop_layout().bounds


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#167c71"))
    painter.drawRoundedRect(0, 0, 64, 64, 15, 15)
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(11, 18, 19, 26, 4, 4)
    painter.drawRoundedRect(34, 18, 19, 26, 4, 4)
    painter.setBrush(QColor("#8bdbc5"))
    painter.drawRect(24, 28, 16, 6)
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    def __init__(self, enable_discovery=True):
        super().__init__()
        self.setWindowTitle("KMLink · 无界键鼠")
        self.setWindowIcon(app_icon())
        self.resize(1040, 900)
        self.setMinimumSize(850, 750)
        self._active = False
        self._closing = False
        self._log_lines = []
        self._peer_label = None
        self._device_id = str(uuid.uuid4())
        self._service_events = queue.Queue(maxsize=512)
        self._service_overflow = threading.Event()
        self._service_generation = 0
        self._closed_requests = deque(maxlen=256)
        self._peers = {}
        self._request_id = None
        self._receiver_request_id = None
        self._engine_request_id = None
        self._auto_receiver_deadline = 0.
        self._pair_dialog = None
        self._pair_dialog_status = None
        self._pair_accept = None
        self.pairing = None
        self.discovery = None
        self._build_ui()
        self.native = NativeInput(
            lambda event: self.engine.enqueue("input", event),
            lambda: self.engine.enqueue("toggle"),
            lambda: self.engine.enqueue("emergency"))
        self._desktop_layout = query_desktop_layout()
        self.engine = Engine(self.native, self._desktop_layout.bounds, self._changed,
                             QApplication.clipboard().setText, validate_key_code, layout=self._desktop_layout)
        self._tracked_screens = set()
        self._watch_screens()
        self._update_layout_info()
        QApplication.instance().screenAdded.connect(self._screens_changed)
        QApplication.instance().screenRemoved.connect(self._screens_changed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(8)
        self.clipboard_timer = QTimer(self)
        self.clipboard_timer.timeout.connect(self._clipboard_poll)
        self.clipboard_timer.start(700)
        self._log("准备就绪。两台电脑需连接同一局域网。")
        if enable_discovery:
            QTimer.singleShot(0, self._start_services)
        else:
            self.discovery_status.setText("设备发现未启用")

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        header = QHBoxLayout()
        brand = label("KMLink")
        brand.setFont(QFont("Segoe UI", 19, QFont.Weight.Bold))
        header.addWidget(brand)
        header.addWidget(label("无界键鼠  /  预览版 " + __version__, "muted"))
        header.addStretch()
        self.status = label("尚未连接", "status")
        header.addWidget(self.status)
        outer.addLayout(header)
        hero = QVBoxLayout()
        hero.setSpacing(7)
        hero.addWidget(label("WINDOWS  ↔  MAC", "eyebrow"))
        hero.addWidget(label("让两台电脑，共用一套键鼠。", "title"))
        hero.addWidget(label("发现身边的电脑，确认后连接。鼠标跨过屏幕边缘，键盘自动跟随。", "muted"))
        outer.addLayout(hero)

        row = QHBoxLayout()
        row.setSpacing(18)
        setup_card, setup = card()
        setup_card.setMinimumWidth(465)
        setup_card.setMinimumHeight(570)
        step_row = QHBoxLayout()
        step_row.addWidget(label("1", "step"))
        step_row.addWidget(label("连接你的另一台电脑", "section"))
        step_row.addStretch()
        setup.addLayout(step_row)
        self.tabs = QTabWidget()
        nearby = QWidget()
        nearby_layout = QVBoxLayout(nearby)
        nearby_layout.setContentsMargins(0, 0, 0, 0)
        nearby_layout.setSpacing(10)
        nearby_layout.addWidget(label("局域网中的 KMLink 设备", "section"))
        nearby_layout.addWidget(label("两台电脑都打开应用即可发现。选中设备，发送控制申请或邀请。", "muted", True))
        self.peer_table = QTableWidget(0, 4)
        self.peer_table.setHorizontalHeaderLabels(["设备名称", "系统", "IP 地址", "状态"])
        self.peer_table.verticalHeader().hide()
        self.peer_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.peer_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.peer_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.peer_table.setMinimumHeight(185)
        self.peer_table.setMaximumHeight(260)
        self.peer_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.peer_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.peer_table.itemSelectionChanged.connect(self._update_peer_actions)
        nearby_layout.addWidget(self.peer_table)
        discover_line = QHBoxLayout()
        self.discovery_status = label("正在启动设备发现…", "muted", True)
        discover_line.addWidget(self.discovery_status, 1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self._refresh_peers)
        discover_line.addWidget(self.refresh_button)
        nearby_layout.addLayout(discover_line)
        self.control_button = QPushButton("申请控制选中设备")
        self.control_button.setObjectName("primary")
        self.control_button.setEnabled(False)
        self.control_button.clicked.connect(lambda: self._request_control("control"))
        nearby_layout.addWidget(self.control_button)
        self.invite_button = QPushButton("邀请对方控制本机")
        self.invite_button.setEnabled(False)
        self.invite_button.clicked.connect(lambda: self._request_control("be_controlled"))
        nearby_layout.addWidget(self.invite_button)
        nearby_layout.addWidget(label("连接前会在两端显示配对数字，需对方核对并同意。", "muted", True))
        self.tabs.addTab(nearby, "发现设备")
        receive = QWidget()
        receive_layout = QVBoxLayout(receive)
        receive_layout.setContentsMargins(0, 0, 0, 0)
        receive_layout.setSpacing(10)
        receive_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        receive_layout.addWidget(label("这台电脑接收控制", "section"))
        receive_layout.addWidget(label("在有键盘鼠标的电脑上选择「发起连接」，输入下方地址和配对密钥。", "muted", True))
        addresses = local_addresses()
        self.address = QLineEdit("  /  ".join(addresses) if addresses else "未检测到局域网 IPv4 地址")
        self.address.setReadOnly(True)
        receive_layout.addWidget(label("本机局域网 IP", "muted"))
        receive_layout.addWidget(self.address)
        receive_layout.addWidget(label("会话配对密钥", "muted"))
        key_row = QHBoxLayout()
        self.host_key = QLineEdit(generate_pairing_key())
        self.host_key.setReadOnly(True)
        self.host_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.host_key.setPlaceholderText("随机生成的配对密钥")
        key_row.addWidget(self.host_key)
        self.copy_button = QPushButton("复制")
        self.copy_button.clicked.connect(self._copy_key)
        key_row.addWidget(self.copy_button)
        receive_layout.addLayout(key_row)
        key_tools = QHBoxLayout()
        self.reveal = QCheckBox("显示密钥")
        self.reveal.toggled.connect(lambda show: self.host_key.setEchoMode(QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password))
        key_tools.addWidget(self.reveal)
        key_tools.addStretch()
        self.regenerate = QPushButton("换一个密钥")
        self.regenerate.clicked.connect(lambda: self.host_key.setText(generate_pairing_key()))
        key_tools.addWidget(self.regenerate)
        receive_layout.addLayout(key_tools)
        receive_layout.addStretch()
        send = QWidget()
        send_layout = QVBoxLayout(send)
        send_layout.setContentsMargins(0, 0, 0, 0)
        send_layout.setSpacing(10)
        send_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        send_layout.addWidget(label("用这台电脑的键鼠", "section"))
        send_layout.addWidget(label("先在另一台电脑开启接收，然后填入它显示的局域网 IP 与配对密钥。", "muted", True))
        send_layout.addWidget(label("另一台电脑的 IP 地址", "muted"))
        self.remote_host = QLineEdit()
        self.remote_host.setPlaceholderText("例如 192.168.1.20")
        send_layout.addWidget(self.remote_host)
        send_layout.addWidget(label("另一台电脑的配对密钥", "muted"))
        self.remote_key = QLineEdit()
        self.remote_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.remote_key.setPlaceholderText("粘贴接收端生成的完整密钥")
        send_layout.addWidget(self.remote_key)
        send_layout.addStretch()
        self.tabs.addTab(receive, "手动接收")
        self.tabs.addTab(send, "手动连接")
        self.tabs.currentChanged.connect(self._tab_changed)
        setup.addWidget(self.tabs)
        port_row = QHBoxLayout()
        port_row.addWidget(label("连接端口", "muted"))
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(24880)
        self.port.setFixedWidth(105)
        port_row.addWidget(self.port)
        port_row.addStretch()
        port_row.addWidget(label("两端保持一致", "muted"))
        self.port_widget = QWidget()
        self.port_widget.setLayout(port_row)
        self.port_widget.setVisible(False)
        setup.addWidget(self.port_widget)
        self.start_button = QPushButton("开启接收")
        self.start_button.setObjectName("primary")
        self.start_button.setVisible(False)
        self.start_button.clicked.connect(self._start)
        setup.addWidget(self.start_button)
        self.stop_button = QPushButton("停止共享")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        setup.addWidget(self.stop_button)
        row.addWidget(setup_card, 6)

        aside = QVBoxLayout()
        aside.setSpacing(18)
        layout_card, layout = card()
        layout_card.setMinimumHeight(350)
        step2 = QHBoxLayout()
        step2.addWidget(label("2", "step"))
        step2.addWidget(label("屏幕与共享", "section"))
        step2.addStretch()
        layout.addLayout(step2)
        self.device_view = label("本机     · · ·     另一台", "device")
        self.device_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.device_view.setMinimumHeight(45)
        layout.addWidget(self.device_view)
        layout.addWidget(label("另一台电脑位于本机的", "muted"))
        self.side = QComboBox()
        for title, value in [("右侧 →", "right"), ("左侧 ←", "left"), ("上方 ↑", "top"), ("下方 ↓", "bottom")]:
            self.side.addItem(title, value)
        self.side.currentIndexChanged.connect(self._layout_changed)
        layout.addWidget(self.side)
        layout.addWidget(label("由发起连接的一端设置。本机屏幕之间不会切换电脑。", "muted", True))
        self.layout_info = label("正在检测本机显示器…", "muted", True)
        layout.addWidget(self.layout_info)
        self.clipboard_check = QCheckBox("同步文本剪贴板")
        self.clipboard_check.setChecked(False)
        self.clipboard_check.toggled.connect(self._options_changed)
        layout.addWidget(self.clipboard_check)
        self.mapping_check = QCheckBox("交换 Ctrl 与 Command / Win")
        self.mapping_check.toggled.connect(self._options_changed)
        layout.addWidget(self.mapping_check)
        layout.addWidget(label("剪贴板需两端开启。按键交换由发起连接端设置。", "muted", True))
        aside.addWidget(layout_card)
        help_card, help_layout = card()
        help_card.setMinimumHeight(260)
        help_layout.addWidget(label("随时收回控制", "section"))
        help_layout.addWidget(label("F8  /  Fn + F8", "device"))
        help_layout.addWidget(label("在本机与另一台电脑间切换", "muted"))
        help_layout.addWidget(label("Ctrl + Alt + Esc", "section"))
        help_layout.addWidget(label("紧急恢复本机控制；接收端按下则断开连接。", "muted", True))
        self.permission_button = QPushButton("Mac 权限与连接帮助")
        self.permission_button.clicked.connect(self._help)
        help_layout.addWidget(self.permission_button)
        aside.addWidget(help_card)
        aside.addStretch()
        row.addLayout(aside, 5)
        outer.addLayout(row, 1)
        self.detail = label("在列表中选择设备，发送申请或邀请；对方确认后连接。", "muted", True)
        outer.addWidget(self.detail)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(70)
        outer.addWidget(self.log)
        footer = QHBoxLayout()
        footer.addWidget(label("●  加密直连    ·    无云端账户    ·    密钥不落盘", "eyebrow"))
        footer.addStretch()
        footer.addWidget(label(socket.gethostname() + "  /  " + platform.system(), "muted"))
        outer.addLayout(footer)

    def _watch_screens(self):
        for screen in QApplication.screens():
            if screen not in self._tracked_screens:
                self._tracked_screens.add(screen)
                screen.geometryChanged.connect(self._screens_changed)
                screen.physicalDotsPerInchChanged.connect(self._screens_changed)

    def _screens_changed(self, *args):
        if not self._closing:
            QTimer.singleShot(0, self._refresh_display_layout)

    def _update_layout_info(self):
        bounds = self._desktop_layout.bounds
        self.layout_info.setText(f"本机 {len(self._desktop_layout.screens)} 块屏幕 · 桌面 {bounds.width} × {bounds.height}\n仅在整组屏幕的外侧边缘切换。")

    def _refresh_display_layout(self):
        if self._closing:
            return
        try:
            layout = query_desktop_layout()
        except Exception as exc:
            self._stop()
            self._log(f"显示器布局读取失败，已停止共享：{exc}")
            return
        self._watch_screens()
        if layout.screens == self._desktop_layout.screens:
            return
        if self._active or self._request_id:
            self._stop()
        self._desktop_layout = layout
        self.engine.layout, self.engine.bounds = layout, layout.bounds
        try:
            position = self.native.position()
            target = layout.clamp(*position)
            if target != position:
                self.native.move_to(*target)
        except RuntimeError:
            pass  # Native devices are initialized only after connecting.
        self._update_layout_info()
        self._log("显示器布局已更新；如刚才正在共享，请重新连接。")

    def _queue_service(self, category, kind, data, generation=None):
        generation = self._service_generation if generation is None else generation
        if self._closing or generation != self._service_generation:
            return
        try:
            self._service_events.put_nowait((generation, category, kind, data))
        except queue.Full:
            self._service_overflow.set()

    def _poll(self):
        if self._closing:
            return
        if self._service_overflow.is_set():
            self._service_overflow.clear()
            self._stop_services()
            self._stop()
            while not self._service_events.empty():
                try:
                    self._service_events.get_nowait()
                except queue.Empty:
                    break
            self.discovery_status.setText("设备发现消息过多，已停止。请重新启动应用。")
            return
        for _ in range(128):
            try:
                generation, category, kind, data = self._service_events.get_nowait()
            except queue.Empty:
                break
            if generation != self._service_generation:
                continue
            try:
                if category == "discovery":
                    if kind == "peers":
                        self._render_peers(data)
                    else:
                        self.discovery_status.setText(str(data))
                        self._log(str(data))
                else:
                    self._pair_event(kind, data)
            except Exception as exc:
                if self._engine_request_id:
                    self.engine.stop(announce=False)
                    self._set_active(False)
                if self._request_id and self.pairing:
                    self.pairing.cancel(self._request_id)
                    self._clear_request(self._request_id)
                self._log(f"设备连接失败：{exc}")
        self.engine.poll()
        if self._auto_receiver_deadline and time.monotonic() >= self._auto_receiver_deadline:
            self._stop()
            self._changed("error", "对方未完成连接，已停止等待。请重新申请。")

    def _start_services(self):
        if self._closing or self.pairing is not None:
            return
        self._service_generation += 1
        generation = self._service_generation
        self.pairing = PairingService(
            self._device_id, socket.gethostname(), system_name(),
            lambda kind, data: self._queue_service("pairing", kind, data, generation))
        try:
            self.pairing.start(port=0)
        except Exception as exc:
            self.pairing.stop()
            self.pairing = None
            self.discovery_status.setText(f"设备发现暂不可用：{exc}。仍可手动连接。")

    def _stop_services(self):
        self._service_generation += 1
        if self.discovery:
            self.discovery.stop()
            self.discovery = None
        if self.pairing:
            self.pairing.stop()
            self.pairing = None
        if self._request_id:
            self._clear_request(self._request_id)

    def _pair_event(self, kind, data):
        request_id = data.get("request_id")
        if request_id and request_id in self._closed_requests:
            return
        if kind == "listening":
            if self.pairing is None:
                return
            if self.discovery:
                self.discovery.stop()
            generation = self._service_generation
            self.discovery = Discovery(
                self._device_id, socket.gethostname(), system_name(), data["port"],
                lambda peers: self._queue_service("discovery", "peers", peers, generation),
                lambda error: self._queue_service("discovery", "error", error, generation))
            self.discovery.start()
            self.discovery_status.setText("正在寻找设备，其他电脑也需打开 KMLink。")
            self._publish_availability()
            return
        if kind in {"incoming", "outgoing"}:
            if kind == "outgoing" and request_id != self._request_id:
                return
            if self._request_id and self._request_id != request_id:
                if self.pairing:
                    self.pairing.reject(request_id)
                return
            if kind == "incoming" and self._active:
                self.pairing.reject(request_id)
                return
            self._request_id = request_id
            self._set_active(self._active)
            self._show_pair_dialog(data, incoming=kind == "incoming")
            return
        if kind in {"prepare_receiver", "connect_controller"}:
            if request_id != self._request_id or not self.pairing:
                return
            issue = permissions_status()
            if issue:
                self.pairing.cancel(request_id)
                self._clear_request(request_id)
                QMessageBox.warning(self, "需要系统权限", issue)
                return
            self._options_changed()
            self._engine_request_id = request_id
            self._set_active(True)
            if self._pair_dialog_status:
                self._pair_dialog_status.setText("已同意，正在建立加密连接…")
            if kind == "prepare_receiver":
                self._receiver_request_id = request_id
                self.engine.start("receiver", "0.0.0.0", self.port.value(), data["key"], self.side.currentData())
                self._auto_receiver_deadline = time.monotonic() + 15.
            else:
                self.engine.start("controller", data["host"], data["port"], data["key"], self.side.currentData())
            return
        if kind in {"finished", "rejected", "error"}:
            if request_id and request_id != self._request_id:
                return
            if kind in {"rejected", "error"} and self._engine_request_id == request_id and request_id:
                self.engine.stop(announce=False)
                self._set_active(False)
            if request_id:
                self._clear_request(request_id)
            detail = data.get("detail", "")
            if detail and kind != "finished":
                self.detail.setText(detail)
                self._log(detail)
            if kind == "error" and not request_id:
                if self.discovery:
                    self.discovery.stop()
                    self.discovery = None
                if self.pairing:
                    self.pairing.stop()
                    self.pairing = None
                self._update_peer_actions()
                self.discovery_status.setText(f"设备发现服务不可用：{detail}。可使用手动连接。")

    def _publish_availability(self):
        busy = self._active or self._request_id is not None
        if self.pairing:
            self.pairing.set_available(not busy)
        if self.discovery:
            self.discovery.set_busy(busy)

    def _selected_peer(self):
        row = self.peer_table.currentRow()
        item = self.peer_table.item(row, 0) if row >= 0 else None
        return self._peers.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _render_peers(self, peers):
        selected = self._selected_peer()
        selected_id = selected.device_id if selected else None
        self._peers = {peer.device_id: peer for peer in peers}
        self.peer_table.blockSignals(True)
        self.peer_table.setRowCount(len(peers))
        selected_row = None
        for row, peer in enumerate(peers):
            for column, value in enumerate((peer.name, peer.system, peer.ip, "使用中" if peer.busy else "可连接")):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, peer.device_id)
                self.peer_table.setItem(row, column, item)
            if peer.device_id == selected_id:
                selected_row = row
        if selected_row is not None:
            self.peer_table.selectRow(selected_row)
        elif len(peers) == 1:
            self.peer_table.selectRow(0)
        else:
            self.peer_table.clearSelection()
            self.peer_table.setCurrentCell(-1, -1)
        self.peer_table.blockSignals(False)
        self.discovery_status.setText(f"发现 {len(peers)} 台设备" if peers else "暂未发现设备，请确认双方在同一局域网并已打开应用。")
        self._update_peer_actions()

    def _update_peer_actions(self):
        peer = self._selected_peer()
        enabled = bool(peer and not peer.busy and not self._active and not self._request_id and self.pairing)
        self.control_button.setEnabled(enabled)
        self.invite_button.setEnabled(enabled)

    def _refresh_peers(self):
        if self.discovery:
            self.discovery.start()
            self.discovery.refresh()
            self.discovery_status.setText("正在刷新局域网设备…")
        elif self.pairing is None:
            self._start_services()

    def _request_control(self, mode):
        peer = self._selected_peer()
        if not peer or peer.busy or self._active or self._request_id or not self.pairing:
            return
        issue = permissions_status()
        if issue:
            QMessageBox.warning(self, "需要系统权限", issue)
            return
        try:
            self._request_id = self.pairing.request(peer.device_id, peer.ip, peer.port, mode)
            self._set_active(False)
            self.detail.setText(f"正在向 {peer.name} 发送请求…")
        except Exception as exc:
            self._request_id = None
            self._set_active(False)
            self._log(f"无法发送请求：{exc}")

    def _show_pair_dialog(self, data, incoming):
        if self._pair_dialog:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("收到连接请求" if incoming else "等待对方确认")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumWidth(460)
        box = QVBoxLayout(dialog)
        box.setContentsMargins(25, 25, 25, 25)
        box.setSpacing(16)
        receiving = data["local_role"] == "receiver"
        if incoming:
            title = "对方申请控制这台电脑" if receiving else "对方邀请你控制它的电脑"
        else:
            title = "邀请对方控制本机" if receiving else "申请控制对方的电脑"
        box.addWidget(label(title, "section"))
        box.addWidget(label(f"{data['peer_name']}\n{data['peer_system']}  ·  {data['peer_ip']}", wrap=True))
        box.addWidget(label("核对两台电脑显示的配对数字", "muted"))
        code = label(str(data["code"]))
        code.setStyleSheet("font-size: 36px; font-family: Consolas, Menlo, monospace; font-weight: bold; color: #167c71;")
        code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(code)
        box.addWidget(label("只有数字一致，才允许连接。设备名称和 IP 不能代替数字核对。", "muted", True))
        self._pair_dialog_status = label("请在 45 秒内确认。" if incoming else "请让对方核对数字并同意，等待期间可以取消。", "muted", True)
        box.addWidget(self._pair_dialog_status)
        buttons = QHBoxLayout()
        cancel = QPushButton("拒绝" if incoming else "取消请求")
        cancel.setDefault(True)
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(cancel)
        self._pair_accept = None
        if incoming:
            approve = QPushButton("数字一致，允许控制" if receiving else "数字一致，接受邀请")
            approve.setObjectName("primary")
            approve.setAutoDefault(False)
            approve.clicked.connect(lambda: self._approve_pair(data["request_id"]))
            buttons.addWidget(approve)
            self._pair_accept = approve
        box.addLayout(buttons)
        dialog.rejected.connect(lambda: self._cancel_pair(data["request_id"], incoming))
        self._pair_dialog = dialog
        dialog.open()

    def _approve_pair(self, request_id):
        if request_id != self._request_id or not self.pairing:
            return
        issue = permissions_status()
        if issue:
            self.pairing.reject(request_id)
            self._clear_request(request_id)
            QMessageBox.warning(self, "需要系统权限", issue)
            return
        if self._pair_accept:
            self._pair_accept.setEnabled(False)
        if self._pair_dialog_status:
            self._pair_dialog_status.setText("正在建立加密连接…")
        self.pairing.approve(request_id)

    def _cancel_pair(self, request_id, incoming=False):
        if request_id != self._request_id:
            return
        if self.pairing:
            # A dialog can be cancelled after approval, while either side is
            # preparing. cancel() interrupts every phase, including handoff.
            self.pairing.cancel(request_id)
        if self._engine_request_id == request_id:
            self.engine.stop(announce=False)
            self._set_active(False)
        self._clear_request(request_id)

    def _clear_request(self, request_id):
        if request_id != self._request_id:
            return
        self._closed_requests.append(request_id)
        self._request_id = None
        self._receiver_request_id = None
        self._engine_request_id = None
        dialog, self._pair_dialog = self._pair_dialog, None
        self._pair_dialog_status = None
        self._pair_accept = None
        if dialog:
            dialog.blockSignals(True)
            dialog.close()
            dialog.deleteLater()
        self._set_active(self._active)

    def _tab_changed(self, index):
        if hasattr(self, "start_button"):
            self.start_button.setText("开启接收" if index == 1 else "连接另一台电脑")
            self.start_button.setVisible(index != 0)
            self.port_widget.setVisible(index != 0)

    def _layout_changed(self):
        if not hasattr(self, "side"):
            return
        arrow = {"right": "本机  · · →  另一台", "left": "另一台  ← · ·  本机",
                 "top": "另一台  ↑  本机", "bottom": "本机  ↓  另一台"}[self.side.currentData()]
        self.device_view.setText(arrow)

    def _options_changed(self):
        if hasattr(self, "engine"):
            self.engine.share_clipboard = self.clipboard_check.isChecked()
            self.engine.map_ctrl_cmd = self.mapping_check.isChecked()

    def _copy_key(self):
        QApplication.clipboard().setText(self.host_key.text())
        self._log("配对密钥已复制，请只提供给要连接的电脑。")

    def _start(self):
        if self._active or self._request_id or self.tabs.currentIndex() == 0:
            return
        try:
            issue = permissions_status()
            if issue:
                QMessageBox.warning(self, "需要系统权限", issue)
                return
            receiver = self.tabs.currentIndex() == 1
            host = "0.0.0.0" if receiver else self.remote_host.text().strip()
            if not host or any(c.isspace() for c in host) or "://" in host:
                raise ValueError("请输入另一台电脑的局域网 IP 地址或主机名。")
            key = normalize_pairing_key(self.host_key.text() if receiver else self.remote_key.text())
            self._options_changed()
            self._set_active(True)
            self.engine.start("receiver" if receiver else "controller", host, self.port.value(), key, self.side.currentData())
        except Exception as exc:
            self._set_active(False)
            self._changed("error", str(exc))

    def _set_active(self, active):
        self._active = active
        if not active:
            self._auto_receiver_deadline = 0.
        busy = active or self._request_id is not None
        self.tabs.tabBar().setEnabled(not busy)
        for widget in (self.start_button, self.port, self.remote_host, self.remote_key, self.regenerate,
                       self.side, self.mapping_check):
            widget.setEnabled(not busy)
        self.stop_button.setEnabled(active)
        self._update_peer_actions()
        self._publish_availability()

    def _stop(self):
        if self._request_id:
            self._cancel_pair(self._request_id)
        self.engine.stop()
        self._set_active(False)

    def _changed(self, state, detail):
        titles = {"stopped": "尚未连接", "error": "连接异常", "disconnected": "已断开",
                  "listening": "等待连接", "connecting": "正在连接", "handshake": "确认设备",
                  "ready": "连接成功", "controlling": "控制另一台", "receiving": "正在被控制"}
        if state in titles:
            self.status.setText(titles[state])
        self.detail.setText(detail)
        self._log(detail)
        if state == "ready":
            self._auto_receiver_deadline = 0.
        if state == "listening" and self._receiver_request_id and self.pairing:
            address = self.engine.transport.listening_address
            self.pairing.receiver_ready(self._receiver_request_id, address[1])
            self._receiver_request_id = None
        if state in {"error", "disconnected", "stopped"}:
            if self._request_id and self.pairing:
                self.pairing.cancel(self._request_id)
                self._clear_request(self._request_id)
            self._set_active(False)

    def _log(self, text):
        self._log_lines.append(datetime.now().strftime("%H:%M:%S") + "   " + text)
        self._log_lines = self._log_lines[-80:]
        self.log.setPlainText("\n".join(self._log_lines))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _clipboard_poll(self):
        if self.engine.share_clipboard and self.engine.remote_bounds:
            mime = QApplication.clipboard().mimeData()
            if mime and mime.hasText() and not mime.hasUrls():
                self.engine.clipboard_changed(mime.text())

    def _help(self):
        QMessageBox.information(self, "权限与连接帮助",
            "1. 两台电脑连接同一局域网并打开 KMLink，在「发现设备」里选择对方。\n"
            "2. 申请控制或邀请对方控制，核对两端配对数字，由接收请求的人同意。发现不可用时可手动连接。\n"
            "3. Windows 防火墙提示时，仅允许你信任的专用网络。\n\n"
            "Mac：系统设置 → 隐私与安全性 → 为 KMLink 开启「辅助功能」和「输入监控」。"
            "如果系统询问本地网络访问，请允许。授权后退出程序再打开。源码启动时授权对象可能是 Terminal / Python。\n\n"
            "F8（Mac 可能需要 Fn+F8）切换控制；Ctrl+Alt+Esc 紧急收回。切换前请松开按键和鼠标按钮。\n\n"
            "预览版支持两台电脑的多屏桌面与文本剪贴板；不支持文件拖放、登录/UAC 安全桌面。"
            "两端都需运行 KMLink。关闭窗口会停止共享。")

    def closeEvent(self, event):
        self._closing = True
        self.timer.stop()
        self.clipboard_timer.stop()
        self._stop_services()
        self.engine.stop(announce=False)
        event.accept()


def main():
    if sys.platform not in {"win32", "darwin"}:
        raise SystemExit("KMLink 目前支持 Windows 和 macOS。")
    enable_dpi_awareness()
    app = QApplication(sys.argv)
    app.setApplicationName("KMLink")
    app.setOrganizationName("KMLink")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow(enable_discovery="--smoke-test" not in sys.argv)
    window.show()
    if "--smoke-test" in sys.argv:
        # Exercise the packaged Qt/runtime startup without installing hooks
        # or accepting connections, then use the normal cleanup path.
        QTimer.singleShot(600, window.close)
    sys.exit(app.exec())
