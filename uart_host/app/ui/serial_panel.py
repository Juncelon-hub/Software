"""Serial port configuration and connection controls."""

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.serial_worker import list_ports
from app.protocol import DEVID_TO_ID, READ_LEN, VALID_INIT, WRITE_LEN, crc16_ibm


class LightComboBox(QComboBox):
    """Draw a theme independent chevron over Qt's native combo box."""

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#7e8d98" if self.isEnabled() else "#b8c2c9")
        painter.setPen(QPen(color, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        center_x = self.width() - 15
        center_y = self.height() / 2
        chevron = QPainterPath()
        chevron.moveTo(center_x - 3, center_y - 1)
        chevron.lineTo(center_x, center_y + 2)
        chevron.lineTo(center_x + 3, center_y - 1)
        painter.drawPath(chevron)


class SerialPanel(QWidget):
    connect_requested = Signal(dict)
    disconnect_requested = Signal()
    chip_changed = Signal(str)

    CHIP_OPTIONS = ["TPS92662A", "TPS92663A", "TPS92664", "TPS929240", "TLD7002"]
    BAUD_RATES = ["500000", "1000000", "2000000"]
    DATA_BITS = ["8", "16"]
    PARITIES = [("None", "N"), ("Even", "E"), ("Odd", "O")]
    MAX_FRAME_BLOCKS = 2000
    MAX_PARTIAL_FRAME_BYTES = 4096
    # ---- 显示范围 ----
    # False: 显示全部帧（0x33 写、读命令、读应答、0x7F ACK、噪声）；
    # True: 仅显示 INIT=0x33 的 16 字节点灯帧。
    ONLY_0X33_FRAME_VIEW = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("connection_card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(14)

        eyebrow = QLabel("CONNECTION")
        eyebrow.setObjectName("section_eyebrow")
        title = QLabel("Serial setup")
        title.setObjectName("panel_title")
        subtitle = QLabel("Configure the target and bus parameters")
        subtitle.setObjectName("panel_subtitle")
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        chip_label = self._field_label("Target chip")
        self.chip_combo = LightComboBox()
        self.chip_combo.addItems(self.CHIP_OPTIONS)
        self.chip_combo.currentTextChanged.connect(self.chip_changed.emit)
        layout.addWidget(chip_label)
        layout.addWidget(self.chip_combo)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("panel_separator")
        layout.addWidget(separator)

        config_title = QLabel("SERIAL PARAMETERS")
        config_title.setObjectName("section_eyebrow")
        layout.addWidget(config_title)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        port_row = QHBoxLayout()
        port_row.setSpacing(6)
        self.port_combo = LightComboBox()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("refresh_btn")
        self.refresh_btn.setToolTip("Refresh available serial ports")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(self.refresh_btn)
        form.addRow(self._field_label("Port"), port_row)

        self.baud_combo = LightComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(self.BAUD_RATES)
        self.baud_combo.setCurrentText("500000")
        form.addRow(self._field_label("Baud"), self.baud_combo)

        self.data_combo = LightComboBox()
        self.data_combo.addItems(self.DATA_BITS)
        form.addRow(self._field_label("Data"), self.data_combo)

        self.parity_combo = LightComboBox()
        for label, _code in self.PARITIES:
            self.parity_combo.addItem(label)
        form.addRow(self._field_label("Parity"), self.parity_combo)

        self.stop_combo = LightComboBox()
        self.stop_combo.addItems(["1", "2"])
        form.addRow(self._field_label("Stop"), self.stop_combo)
        layout.addLayout(form)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connect_btn")
        self.connect_btn.setCheckable(True)
        self.connect_btn.toggled.connect(self._on_connect_toggled)
        layout.addWidget(self.connect_btn)

        hint = QLabel("Passive monitoring · no data is transmitted")
        hint.setObjectName("panel_subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        outer.addWidget(card)

        stream_card = QFrame()
        stream_card.setObjectName("frame_stream_card")
        stream_card.setStyleSheet(
            "QFrame#frame_stream_card { background:rgba(255,255,255,218); "
            "border:1px solid rgba(255,255,255,235); border-radius:20px; }"
        )
        stream_layout = QVBoxLayout(stream_card)
        stream_layout.setContentsMargins(16, 14, 16, 16)
        stream_layout.setSpacing(7)
        stream_eyebrow = QLabel("RAW FRAME STREAM")
        stream_eyebrow.setObjectName("section_eyebrow")
        stream_title = QLabel("Practical Message")
        stream_title.setObjectName("panel_title")
        stream_toolbar = QHBoxLayout()
        stream_toolbar.addWidget(stream_title)
        stream_toolbar.addStretch(1)
        self.frame_pause_btn = QPushButton("Pause")
        self.frame_pause_btn.setObjectName("pause_btn")
        self.frame_pause_btn.clicked.connect(self._toggle_frame_pause)
        stream_toolbar.addWidget(self.frame_pause_btn)
        self.frame_clear_btn = QPushButton("Clear")
        self.frame_clear_btn.setObjectName("clear_btn")
        self.frame_clear_btn.clicked.connect(self.clear_frame_stream)
        stream_toolbar.addWidget(self.frame_clear_btn)
        self.frame_hint = QLabel(
            "0x33 frames only · waiting for UART INIT"
            if self.ONLY_0X33_FRAME_VIEW
            else "Waiting for UART INIT frame"
        )
        stream_hint = self.frame_hint
        stream_hint.setObjectName("panel_subtitle")
        stream_hint.setWordWrap(True)
        stream_layout.addWidget(stream_eyebrow)
        stream_layout.addLayout(stream_toolbar)
        stream_layout.addWidget(stream_hint)

        self.frame_view = QPlainTextEdit()
        self.frame_view.setReadOnly(True)
        self.frame_view.setMaximumBlockCount(self.MAX_FRAME_BLOCKS)
        self.frame_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.frame_view.setMinimumHeight(210)
        self.frame_view.setStyleSheet(
            "QPlainTextEdit { background:rgba(246,249,251,238); color:#34434e; "
            "border:1px solid #e3eaee; border-radius:13px; padding:8px; "
            "font-family:'Cascadia Mono','Consolas',monospace; font-size:10px; }"
        )
        stream_layout.addWidget(self.frame_view)
        outer.addWidget(stream_card, 1)

        self.refresh_ports()
        self._frame_buffer = bytearray()
        self._partial_frame_visible = False
        self._frame_count = 0
        self._pending_response_length = None
        self._pending_response_data = bytearray()
        self._active_line_bytes = b""
        self._active_line_ts = ""
        self._awaiting_write_ack = False
        self._frame_paused = False
        self._display_line_valid = False

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("field_label")
        return label

    def refresh_ports(self):
        self.port_combo.clear()
        for description, device in list_ports():
            self.port_combo.addItem(f"{device} - {description}", userData=device)
        if self.port_combo.count() == 0:
            self.port_combo.addItem("No port found", userData="")

    def _on_connect_toggled(self, checked: bool):
        if checked:
            settings = self.current_settings()
            if not settings["port"]:
                self.connect_btn.setChecked(False)
                return
            self._set_inputs_enabled(False)
            self.connect_requested.emit(settings)
            self.connect_btn.setText("Disconnect")
        else:
            self._set_inputs_enabled(True)
            self.disconnect_requested.emit()
            self.connect_btn.setText("Connect")

    def current_settings(self) -> dict:
        device = self.port_combo.currentData() or ""
        return {
            "port": device,
            "baudrate": int(self.baud_combo.currentText() or "500000"),
            "bytesize": int(self.data_combo.currentText() or "8"),
            "parity": self.PARITIES[self.parity_combo.currentIndex()][1],
            "stopbits": int(self.stop_combo.currentText()),
            "timeout": 0.02,
        }

    def set_connected_state(self, connected: bool):
        if not connected:
            self._set_inputs_enabled(True)
            self.connect_btn.setChecked(False)
            self.connect_btn.setText("Connect")
        else:
            self.connect_btn.setText("Disconnect")

    def feed_frame_stream(self, data: bytes):
        """Split TPS92662A UART command/response frames using INIT lengths."""
        self._frame_buffer.extend(data)
        while True:
            if self._awaiting_write_ack:
                if self._frame_buffer and self._frame_buffer[0] == 0x7F:
                    del self._frame_buffer[:1]
                    if not self.ONLY_0X33_FRAME_VIEW:
                        self._append_to_last_line(b"\x7F")
                        self._active_line_bytes += b"\x7F"
                    self._awaiting_write_ack = False
                    continue
                self._awaiting_write_ack = False

            if self._pending_response_length is not None:
                remaining = self._pending_response_length - len(self._pending_response_data)
                if not self._frame_buffer:
                    return
                response = self._pending_response_data + self._frame_buffer[:remaining]
                response_ok = (len(response) >= self._pending_response_length
                               and crc16_ibm(response[:-2]) == (response[-2] | (response[-1] << 8)))
                init = self._frame_buffer[0]
                if not self._pending_response_data and not response_ok and init in VALID_INIT:
                    length = WRITE_LEN[init] + 5 if init in WRITE_LEN else 5
                    valid_devid = len(self._frame_buffer) >= 2 and self._frame_buffer[1] in DEVID_TO_ID
                    if valid_devid and len(self._frame_buffer) >= length:
                        command = self._frame_buffer[:length]
                        if crc16_ibm(command[:-2]) == (command[-2] | (command[-1] << 8)):
                            self._pending_response_length = None
                            continue
                    if valid_devid and init == 0x33 and len(self._frame_buffer) < length:
                        return
                take = min(remaining, len(self._frame_buffer))
                self._pending_response_data.extend(self._frame_buffer[:take])
                del self._frame_buffer[:take]
                if not self.ONLY_0X33_FRAME_VIEW:
                    self._replace_last_line(self._active_line_bytes + bytes(self._pending_response_data))
                if len(self._pending_response_data) < self._pending_response_length:
                    return
                if not self.ONLY_0X33_FRAME_VIEW:
                    self._active_line_bytes += bytes(self._pending_response_data)
                self._pending_response_data.clear()
                self._pending_response_length = None
                continue

            starts = [self._frame_buffer.find(value) for value in VALID_INIT]
            starts = [value for value in starts if value >= 0]
            start = min(starts) if starts else -1
            if start < 0:
                if self._frame_buffer and not self.ONLY_0X33_FRAME_VIEW:
                    self._append_raw_frame(self._frame_buffer, prefix="RAW (no INIT)")
                self._frame_buffer.clear()
                self._partial_frame_visible = False
                if self._frame_count == 0 and not self.ONLY_0X33_FRAME_VIEW:
                    self._set_frame_hint("No UART INIT detected in received bytes")
                return
            if start:
                if not self.ONLY_0X33_FRAME_VIEW:
                    self._append_raw_frame(self._frame_buffer[:start], prefix="NOISE")
                del self._frame_buffer[:start]

            init = self._frame_buffer[0]
            if init in WRITE_LEN:
                length = WRITE_LEN[init] + 5
            else:
                length = 5
            if len(self._frame_buffer) < length:
                if len(self._frame_buffer) > self.MAX_PARTIAL_FRAME_BYTES:
                    self._frame_buffer.clear()
                    self._partial_frame_visible = False
                elif not self.ONLY_0X33_FRAME_VIEW or init == 0x33:
                    if not self._partial_frame_visible:
                        self._frame_count += 1
                    label = "0x33" if self.ONLY_0X33_FRAME_VIEW else "INIT"
                    self._set_frame_hint(
                        f"Detected {self._frame_count} {label} frame(s) · current frame receiving"
                    )
                    self._show_partial_frame()
                return

            was_partial = self._partial_frame_visible
            command = bytes(self._frame_buffer[:length])
            show = not self.ONLY_0X33_FRAME_VIEW or init == 0x33
            if show:
                self._show_partial_frame(command)
                if not was_partial:
                    self._frame_count += 1
                self._active_line_bytes = command
            else:
                self._active_line_bytes = b""
            del self._frame_buffer[:length]
            self._partial_frame_visible = False
            if init in READ_LEN:
                self._pending_response_length = READ_LEN[init] + 2
                self._pending_response_data.clear()
            elif init in WRITE_LEN:
                self._awaiting_write_ack = True

    def _set_frame_hint(self, text: str):
        if hasattr(self, "frame_hint"):
            self.frame_hint.setText(text)

    @staticmethod
    def _stamp(ts: float) -> str:
        """HH:MM:SS.mmm 格式的时间戳。"""
        millis = int((ts % 1) * 1000)
        return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{millis:03d}"

    def _show_partial_frame(self, frame: bytes | None = None):
        if self._frame_paused:
            self._partial_frame_visible = False
            self._display_line_valid = False
            return
        text = (frame if frame is not None else self._frame_buffer).hex(" ").upper()
        if self._partial_frame_visible:
            cursor = self.frame_view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(f"{self._active_line_ts}  {text}")
        else:
            self._active_line_ts = self._stamp(time.time())
            self.frame_view.appendPlainText(f"{self._active_line_ts}  {text}")
            self._partial_frame_visible = True
        self._display_line_valid = True
        bar = self.frame_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _replace_last_line(self, data: bytes):
        if self._frame_paused:
            return
        if not self._display_line_valid:
            if not self._active_line_ts:
                self._active_line_ts = self._stamp(time.time())
            self.frame_view.appendPlainText(
                f"{self._active_line_ts}  {data.hex(' ').upper()}"
            )
            self._display_line_valid = True
            self.frame_view.verticalScrollBar().setValue(self.frame_view.verticalScrollBar().maximum())
            return
        cursor = self.frame_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(f"{self._active_line_ts}  {data.hex(' ').upper()}")
        self.frame_view.verticalScrollBar().setValue(self.frame_view.verticalScrollBar().maximum())

    def _append_to_last_line(self, data: bytes):
        if self._frame_paused:
            return
        if not self._display_line_valid:
            if not self._active_line_ts:
                self._active_line_ts = self._stamp(time.time())
            self.frame_view.appendPlainText(
                f"{self._active_line_ts}  {(self._active_line_bytes + data).hex(' ').upper()}"
            )
            self._display_line_valid = True
            self.frame_view.verticalScrollBar().setValue(self.frame_view.verticalScrollBar().maximum())
            return
        cursor = self.frame_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(" " + data.hex(" ").upper())
        self.frame_view.verticalScrollBar().setValue(self.frame_view.verticalScrollBar().maximum())

    def _append_raw_frame(self, data: bytes | bytearray, prefix: str):
        if not data or self._frame_paused:
            return
        self.frame_view.appendPlainText(
            f"{self._stamp(time.time())}  {prefix}: {bytes(data).hex(' ').upper()}"
        )
        bar = self.frame_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear_frame_stream(self):
        self._frame_buffer.clear()
        self._partial_frame_visible = False
        self._frame_count = 0
        self._pending_response_length = None
        self._pending_response_data.clear()
        self._active_line_bytes = b""
        self._active_line_ts = ""
        self._awaiting_write_ack = False
        self._display_line_valid = False
        self._set_frame_hint(
            "0x33 frames only · waiting for UART INIT"
            if self.ONLY_0X33_FRAME_VIEW
            else "Waiting for UART INIT frame"
        )
        self.frame_view.clear()

    def _toggle_frame_pause(self):
        self._frame_paused = not self._frame_paused
        self.frame_pause_btn.setText("Resume" if self._frame_paused else "Pause")
        self._partial_frame_visible = False
        self._display_line_valid = False
        self._set_frame_hint(
            "Display paused · receiving and parsing continue"
            if self._frame_paused
            else "Display resumed · receiving and parsing continue"
        )

    def _set_inputs_enabled(self, enabled: bool):
        for widget in (
            self.port_combo,
            self.refresh_btn,
            self.baud_combo,
            self.data_combo,
            self.parity_combo,
            self.stop_combo,
        ):
            widget.setEnabled(enabled)
