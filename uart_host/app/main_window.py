"""主窗口：串口配置 + 多设备状态面板。

数据流：SerialWorker(线程) --data_available--> MainWindow
        --> BusMonitor 切帧校验 --> 设备影子 --> StatusPanel 更新卡片
"""

from pathlib import Path
import time

from PySide6.QtCore import QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.protocol import CHIP_CHANNELS, BusMonitor
from app.serial_worker import SerialWorker
from app.ui.serial_panel import SerialPanel
from app.ui.status_panel import StatusPanel


MAX_PARSE_BYTES_PER_TICK = 16 * 1024


class AmbientCanvas(QWidget):
    """Soft, blurred color fields behind translucent panels."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = QLinearGradient(0, 0, self.width(), self.height())
        base.setColorAt(0, QColor("#f5f7fb"))
        base.setColorAt(1, QColor("#eaf1f5"))
        painter.fillRect(self.rect(), base)
        blooms = (
            (QPointF(self.width() * 0.05, self.height() * 0.05), 360, QColor(106, 173, 229, 82)),
            (QPointF(self.width() * 0.92, self.height() * 0.12), 420, QColor(108, 205, 190, 68)),
            (QPointF(self.width() * 0.80, self.height() * 0.96), 500, QColor(224, 180, 144, 46)),
        )
        for center, radius, color in blooms:
            glow = QRadialGradient(center, radius)
            glow.setColorAt(0, color)
            glow.setColorAt(1, QColor(color.red(), color.green(), color.blue(), 0))
            painter.fillRect(self.rect(), glow)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Device Workspace")
        self.resize(1200, 760)

        self._worker = SerialWorker(self)
        self._monitor = BusMonitor()
        self._connected = False
        # 串口线程可能在很短时间内连续投递多个数据块。每轮 GUI 事件只
        # 解析有限字节（MAX_PARSE_BYTES_PER_TICK），剩余数据由这个 1ms
        # 单次定时器续跑，避免积压时长时间阻塞事件循环。
        self._rx_buffer = bytearray()
        self._pending_snapshots = {}
        self._rx_metrics = {
            "processed_bytes": 0,
            "dropped_bytes": 0,
            "gui_high_water_bytes": 0,
            "max_parse_ms": 0.0,
        }
        self._rx_flush_timer = QTimer(self)
        self._rx_flush_timer.setSingleShot(True)
        self._rx_flush_timer.setInterval(1)
        self._rx_flush_timer.timeout.connect(self._flush_received_data)

        self._init_ui()
        self._connect_signals()
        # SerialWorker 的 open/close/write 接口仅负责向命令队列投递任务；
        # 必须先启动 QThread，run() 中的实际串口 I/O 才会执行。
        self._worker.start()

    def _apply_theme(self):
        self.setStyleSheet("""
        * { font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif; }
        QMainWindow, QWidget#root { background: transparent; color: #202a35; }
        QWidget { color: #202a35; }
        QLabel { background: transparent; color: #26323c; }
        QFrame#topbar, QFrame#connection_card, QFrame#fleet_card {
            background: rgba(255,255,255,218); border: 1px solid rgba(255,255,255,235);
            border-radius: 20px;
        }
        QFrame#navigation { background:#f0f4f7; border:1px solid #e6ecef; border-radius:14px; }
        QLabel#brand_logo { background:transparent; border:0; }
        QPushButton#nav_button { background:transparent; color:#68767f; border:0; border-radius:11px; padding:8px 4px; min-height:0; font-size:12px; font-weight:600; }
        QPushButton#nav_button:hover { background:#e0e7ec; color:#3f5564; }
        QPushButton#nav_button:checked { background:#ffffff; color:#2878b8; }
        QFrame#control_hero, QFrame#control_notice, QFrame#target_card { background:rgba(255,255,255,218); border:1px solid rgba(255,255,255,235); border-radius:20px; }
        QLabel#control_title { color:#202d37; font-size:22px; font-weight:700; }
        QLabel#control_subtitle { color:#778691; font-size:12px; }
        QLabel#target_name { color:#26343e; font-size:16px; font-weight:700; }
        QLabel#target_description { color:#7c8a95; font-size:11px; }
        QLabel#planned_badge { background:#f0f3f5; color:#7a8790; border:1px solid #e3e8eb; border-radius:9px; padding:5px 8px; font-size:9px; font-weight:700; }
        QLabel#eyebrow, QLabel#section_eyebrow { color: #82919e; font-size: 10px; font-weight: 700; letter-spacing: 1.5px; }
        QLabel#app_title { color: #18232d; font-size: 23px; font-weight: 700; }
        QLabel#app_subtitle { color: #788692; font-size: 11px; }
        QLabel#live_badge { background:#edf1f4; color:#6d7c87; border:1px solid #e1e7eb; border-radius:12px; padding:7px 12px; font-size:11px; font-weight:700; }
        QLabel#live_badge[connected="true"] { background:#e5f6ef; color:#16845f; border-color:#c8eadc; }
        QLabel#chip_badge { background:#eff4fb; color:#426b9b; border:1px solid #dce7f4; border-radius:12px; padding:7px 12px; font-size:11px; font-weight:600; }
        QLabel#panel_title { color:#25313b; font-size:16px; font-weight:700; }
        QLabel#panel_subtitle { color:#85929d; font-size:11px; }
        QLabel#field_label { color:#65737e; font-size:11px; font-weight:600; }
        QComboBox, QPushButton {
            min-height: 36px; border-radius: 11px; padding: 0 11px;
            border: 1px solid #dce4e9; background: rgba(247,249,251,245); color: #26323c;
        }
        QComboBox QLineEdit { color:#26323c; background:transparent; selection-background-color:#d7e8f7; }
        QComboBox QAbstractItemView { background:#ffffff; color:#26323c; selection-background-color:#e6f1fa; selection-color:#203b55; border:1px solid #dce4e9; outline:0; }
        QComboBox::down-arrow { image:none; width:0; height:0; border:0; }
        QComboBox:hover, QPushButton:hover { border-color:#b8cbd7; background:#ffffff; }
        QComboBox:focus, QPushButton:focus { border:1px solid #98bdd5; }
        QComboBox::drop-down { border:0; width:27px; }
        QPushButton#refresh_btn, QPushButton#clear_btn { min-width:38px; background:#f5f8fa; color:#52616d; }
        QPushButton#connect_btn { min-height:42px; background:#2878b8; border:0; color:#ffffff; font-size:12px; font-weight:700; }
        QPushButton#connect_btn:hover { background:#226ca8; }
        QPushButton#connect_btn:checked { background:#e9eef2; color:#52616d; border:1px solid #d9e1e6; }
        QCheckBox { color:#64727d; spacing:7px; font-size:11px; }
        QCheckBox::indicator { width:15px; height:15px; border-radius:5px; border:1px solid #cbd6dd; background:#ffffff; }
        QCheckBox::indicator:checked { background:#438fc5; border-color:#438fc5; }
        QSplitter::handle { background:transparent; }
        QSplitter::handle:horizontal { width:14px; }
        QSplitter::handle:vertical { height:14px; }
        QFrame#panel_separator { color:#e8edf0; background:#e8edf0; border:0; max-height:1px; }
        QScrollArea { background:transparent; border:0; }
        QScrollBar:vertical { background:transparent; width:10px; margin:3px; }
        QScrollBar::handle:vertical { background:#cad5dc; border-radius:5px; min-height:24px; }
        QScrollBar:horizontal { background:transparent; height:10px; margin:3px; }
        QScrollBar::handle:horizontal { background:#cad5dc; border-radius:5px; min-width:24px; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { width:0; height:0; }
        QStatusBar { background:#f4f8fb; color:#8faabd; border:0; padding-left:8px; font-size:11px; }
        QToolTip { background:#24323d; color:#ffffff; border:1px solid #24323d; padding:5px; }
        """)

    @staticmethod
    def _add_glass_shadow(widget):
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(48, 76, 96, 24))
        widget.setGraphicsEffect(shadow)

    # ---- UI 组装 ----

    def _init_ui(self):
        central = AmbientCanvas()
        central.setObjectName("root")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(22, 18, 22, 10)
        layout.setSpacing(16)

        nav_row = QWidget()
        nav_row_layout = QHBoxLayout(nav_row)
        nav_row_layout.setContentsMargins(0, 0, 0, 0)
        nav_row_layout.setSpacing(0)
        navigation = QFrame()
        navigation.setObjectName("navigation")
        navigation.setFixedSize(178, 58)
        nav_panel_layout = QHBoxLayout(navigation)
        nav_panel_layout.setContentsMargins(4, 4, 4, 4)
        nav_panel_layout.setSpacing(6)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        for index, text in enumerate(("Monitor", "Control")):
            button = QPushButton(text)
            button.setObjectName("nav_button")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self.nav_group.addButton(button, index)
            nav_panel_layout.addWidget(button)
            self.nav_buttons.append(button)
        self.nav_buttons[0].setChecked(True)
        nav_row_layout.addWidget(navigation)
        nav_row_layout.addStretch(1)

        brand_logo = QLabel()
        brand_logo.setObjectName("brand_logo")
        brand_logo.setFixedSize(178, 58)
        brand_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_path = Path(__file__).resolve().parents[1] / "hella.png"
        logo = QPixmap(str(logo_path))
        if not logo.isNull():
            image = logo.toImage()
            if image.hasAlphaChannel():
                left, top = image.width(), image.height()
                right = bottom = -1
                for y in range(image.height()):
                    for x in range(image.width()):
                        if ((image.pixel(x, y) >> 24) & 0xFF) > 8:
                            left, top = min(left, x), min(top, y)
                            right, bottom = max(right, x), max(bottom, y)
                if right >= left and bottom >= top:
                    pad_x = max(2, round((right - left + 1) * 0.035))
                    pad_y = max(2, round((bottom - top + 1) * 0.035))
                    x = max(0, left - pad_x)
                    y = max(0, top - pad_y)
                    width = min(image.width(), right + pad_x + 1) - x
                    height = min(image.height(), bottom + pad_y + 1) - y
                    logo = logo.copy(QRect(x, y, width, height))
            logo = logo.scaled(
                QSize(154, 50),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            brand_logo.setPixmap(logo)
        else:
            brand_logo.setText("HELLA")
        nav_row_layout.addWidget(brand_logo)
        layout.addWidget(nav_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        self.serial_panel = SerialPanel()
        splitter.addWidget(self.serial_panel)

        # 右：多设备状态卡片
        self.status_panel = StatusPanel()
        splitter.addWidget(self.status_panel)
        splitter.setSizes([300, 900])

        self.monitor_page = QWidget()
        monitor_layout = QVBoxLayout(self.monitor_page)
        monitor_layout.setContentsMargins(0, 0, 0, 0)
        monitor_layout.setSpacing(16)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(88)
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(22, 12, 18, 12)
        top_layout.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        eyebrow = QLabel("DEVICE WORKSPACE  /  UART")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Message Monitor")
        title.setObjectName("app_title")
        subtitle = QLabel("Live device status, serial connection and frame diagnostics")
        subtitle.setObjectName("app_subtitle")
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top_layout.addLayout(title_box)
        top_layout.addStretch(1)
        self.chip_badge = QLabel("TPS92662A  ·  12 CH")
        self.chip_badge.setObjectName("chip_badge")
        self.live_badge = QLabel("NOT CONNECTED")
        self.live_badge.setObjectName("live_badge")
        self.live_badge.setProperty("connected", False)
        top_layout.addWidget(self.chip_badge)
        top_layout.addWidget(self.live_badge)
        monitor_layout.addWidget(topbar)
        monitor_layout.addWidget(splitter)

        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.monitor_page)
        self.control_page = self._build_control_page()
        self.page_stack.addWidget(self.control_page)
        self.nav_group.idClicked.connect(self.page_stack.setCurrentIndex)
        self.nav_group.idClicked.connect(self._on_page_changed)
        layout.addWidget(self.page_stack, 1)
        self.setCentralWidget(central)
        self.statusBar().setSizeGripEnabled(False)
        self._apply_theme()
        self._refresh_page_status()

    def _build_control_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        hero = QFrame()
        hero.setObjectName("control_hero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(26, 23, 24, 23)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(6)
        kicker = QLabel("UART COMMAND CENTER")
        kicker.setObjectName("section_eyebrow")
        title = QLabel("Control workspace")
        title.setObjectName("control_title")
        subtitle = QLabel("A dedicated place for sending UART commands to supported driver chips.")
        subtitle.setObjectName("control_subtitle")
        text_layout.addWidget(kicker)
        text_layout.addWidget(title)
        text_layout.addWidget(subtitle)
        hero_layout.addLayout(text_layout)
        hero_layout.addStretch(1)
        planned = QLabel("IN DEVELOPMENT")
        planned.setObjectName("planned_badge")
        hero_layout.addWidget(planned, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(hero)
        self._add_glass_shadow(hero)

        targets = QHBoxLayout()
        targets.setSpacing(14)
        target_specs = (
            ("POWER CONVERSION", "Boost chips", "Power stage setup and control"),
            ("POWER CONVERSION", "Buck chips", "Voltage regulation controls"),
            ("LIGHTING", "LED matrix management", "Matrix configuration and channel control"),
        )
        for category, name, description in target_specs:
            card = QFrame()
            card.setObjectName("target_card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(20, 19, 20, 18)
            card_layout.setSpacing(9)
            category_label = QLabel(category)
            category_label.setObjectName("section_eyebrow")
            name_label = QLabel(name)
            name_label.setObjectName("target_name")
            description_label = QLabel(description)
            description_label.setObjectName("target_description")
            description_label.setWordWrap(True)
            badge = QLabel("PLANNED")
            badge.setObjectName("planned_badge")
            card_layout.addWidget(category_label)
            card_layout.addSpacing(5)
            card_layout.addWidget(name_label)
            card_layout.addWidget(description_label)
            card_layout.addStretch(1)
            card_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)
            targets.addWidget(card, 1)
            self._add_glass_shadow(card)
        layout.addLayout(targets)

        notice = QFrame()
        notice.setObjectName("control_notice")
        notice_layout = QVBoxLayout(notice)
        notice_layout.setContentsMargins(22, 17, 22, 17)
        notice_layout.setSpacing(5)
        notice_title = QLabel("UART transmit workflow")
        notice_title.setObjectName("target_name")
        notice_text = QLabel("Command framing, chip-specific settings and transmit feedback will be added here.")
        notice_text.setObjectName("target_description")
        notice_text.setWordWrap(True)
        notice_layout.addWidget(notice_title)
        notice_layout.addWidget(notice_text)
        layout.addWidget(notice)
        self._add_glass_shadow(notice)
        layout.addStretch(1)
        return page

    # ---- 信号接线 ----

    def _connect_signals(self):
        self.serial_panel.connect_requested.connect(self._worker.open_port)
        self.serial_panel.disconnect_requested.connect(self._worker.close_port)
        self.serial_panel.chip_changed.connect(self._on_chip_changed)

        self._worker.data_available.connect(self._on_data_available)
        self._worker.port_opened.connect(self._on_port_opened)
        self._worker.port_closed.connect(self._on_port_closed)
        self._worker.error_occurred.connect(self._on_error)

    # ---- 槽 ----

    def _on_chip_changed(self, chip: str):
        self._monitor.set_chip(chip)
        self.status_panel.set_chip(chip)
        self.chip_badge.setText(f"{chip}  ·  {CHIP_CHANNELS.get(chip, '?')} CH")

    def _on_data_available(self):
        data, dropped_bytes = self._worker.drain_received()
        if dropped_bytes:
            # A gap invalidates every state inferred from previous traffic.
            self._rx_buffer.clear()
            self._pending_snapshots.clear()
            self._monitor.reset()
            self.status_panel.invalidate_data()
            self._rx_metrics["dropped_bytes"] += dropped_bytes
        if not data:
            return
        self._rx_buffer.extend(data)
        self._rx_metrics["gui_high_water_bytes"] = max(
            self._rx_metrics["gui_high_water_bytes"], len(self._rx_buffer)
        )
        # 收到即解析：ECU 可能只下发一次状态帧（如 16 字节广播写 0x33），
        # 合批等待会让该帧的状态延迟数十毫秒才反映到界面。解析本身开销很小，
        # 积压场景由 _flush_received_data 内的 1ms 续跑定时器保证响应性。
        self._flush_received_data()

    def _reset_receive_pipeline(self, discard_worker: bool = True):
        self._rx_flush_timer.stop()
        self._rx_buffer.clear()
        self._pending_snapshots.clear()
        if discard_worker:
            self._worker.discard_received()

    def _flush_received_data(self):
        """本轮最多解析 MAX_PARSE_BYTES_PER_TICK 字节，剩余字节由 1ms
        单次定时器续跑。数据丢弃只发生在串口线程的有界缓冲里。"""
        if not self._rx_buffer:
            return

        # Bound one GUI event's work; remaining bytes are processed in a later turn.
        count = min(len(self._rx_buffer), MAX_PARSE_BYTES_PER_TICK)
        data = bytes(self._rx_buffer[:count])
        del self._rx_buffer[:count]
        started_at = time.perf_counter()
        result = self._monitor.feed(data)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self._rx_metrics["processed_bytes"] += len(data)
        self._rx_metrics["max_parse_ms"] = max(self._rx_metrics["max_parse_ms"], elapsed_ms)
        self.serial_panel.feed_frame_stream(data)
        for snap in result.snapshots:
            self._pending_snapshots[snap.address] = snap
        for snap in self._pending_snapshots.values():
            self.status_panel.update_device(snap)
        self._pending_snapshots.clear()
        if self._rx_buffer:
            # Yield to input/paint events before continuing a backlog.
            self._rx_flush_timer.start(1)

    def receive_metrics(self) -> dict[str, int | float]:
        """Runtime counters for diagnostics and future production telemetry UI."""
        return {**self._worker.receive_stats(), **self._rx_metrics, "gui_depth_bytes": len(self._rx_buffer)}

    def _on_port_opened(self):
        # The worker discarded stale data before opening; do not discard fresh bytes
        # that may have arrived between the open event and this GUI callback.
        self._reset_receive_pipeline(discard_worker=False)
        self.serial_panel.clear_frame_stream()
        self.serial_panel.set_connected_state(True)
        self._connected = True
        self.status_panel.set_connection(True, self.serial_panel.port_combo.currentText())
        self._monitor.reset()
        self._set_connection_badge(True)
        self._refresh_page_status()

    def _on_port_closed(self):
        self._reset_receive_pipeline()
        self.serial_panel.clear_frame_stream()
        self.serial_panel.set_connected_state(False)
        self._connected = False
        self.status_panel.set_connection(False)
        self._set_disconnected_banner()

    def _on_error(self, message: str):
        self._reset_receive_pipeline()
        self.serial_panel.clear_frame_stream()
        self.serial_panel.set_connected_state(False)
        self._connected = False
        self.status_panel.set_connection(False)
        self._set_disconnected_banner()

    def _set_disconnected_banner(self):
        self._set_connection_badge(False)
        self._refresh_page_status()

    def _set_connection_badge(self, connected: bool):
        self.live_badge.setText("CONNECTED" if connected else "NOT CONNECTED")
        self.live_badge.setProperty("connected", connected)
        self.live_badge.style().unpolish(self.live_badge)
        self.live_badge.style().polish(self.live_badge)
        self.live_badge.update()

    def _on_page_changed(self, index: int):
        self._refresh_page_status()

    def _refresh_page_status(self):
        if self.nav_group.checkedId() == 1:
            message = "Control workspace · UART command interface planned"
        elif self._connected:
            port = self.serial_panel.port_combo.currentText()
            message = f"Monitor · Connected · {port} · Listening for frames"
        else:
            message = "Monitor · Not connected · Select a serial port to begin"
        self.statusBar().showMessage(message)

    # ---- 关闭时优雅退出 ----

    def closeEvent(self, event):
        self._worker.stop()
        self._worker.wait(2000)
        super().closeEvent(event)
