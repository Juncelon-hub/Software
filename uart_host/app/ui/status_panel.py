"""Live device cards and channel status indicators."""

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.protocol import CHIP_CHANNELS, DEFAULT_CHIP, DeviceSnapshot

OFFLINE_AFTER_S = 3.0

_LEVEL_STYLE = {
    "ok": ("#16845f", "#e6f6ef"),
    "warn": ("#a96713", "#fff3dc"),
    "error": ("#bd4747", "#fdeaea"),
    "unknown": ("#687782", "#edf1f3"),
}

_LAMP_ON = "#38a77b"
_LAMP_OFF = "#dce4e8"
_LAMP_FAULT = "#df5a5a"
_LAMP_UNKNOWN = "#c7d1d7"


def _lamp_style(color: str) -> str:
    return (
        f"background:{color}; border:1px solid rgba(42,62,74,28);"
        "border-radius:7px; min-width:14px; max-width:14px;"
        "min-height:14px; max-height:14px;"
    )


class DeviceCard(QFrame):
    def __init__(self, address: int | None, n_channels: int, placeholder=False, parent=None):
        super().__init__(parent)
        self.address = address
        self.n_channels = n_channels
        self.placeholder = placeholder
        self.last_ts = 0.0
        self._snapshot_key = None
        self._last_snapshot: DeviceSnapshot | None = None
        self._stale = False
        self._init_ui()

    def _init_ui(self):
        self.setObjectName("device_card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMaximumHeight(250)
        self.setStyleSheet(
            "QFrame#device_card { background:rgba(249,251,252,238); "
            "border:1px solid #e7edf0; border-radius:16px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        head = QHBoxLayout()
        title_text = "Waiting for device" if self.placeholder else f"Device  0x{self.address:02X}"
        title = QLabel(title_text)
        title.setStyleSheet("color:#25323c; font-size:14px; font-weight:700;")
        self.status_label = QLabel("NO DATA" if self.placeholder else "Waiting")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setMinimumWidth(94)
        self._set_status_style("unknown")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.status_label)
        layout.addLayout(head)

        self._lamp_rows = {}
        layout.addWidget(self._make_lamp_row("OUTPUT", "on"))
        layout.addWidget(self._make_lamp_row("FAULT", "fault"))

        self.channel_detail = QLabel("Channels · Waiting for data")
        self.channel_detail.setWordWrap(True)
        self.channel_detail.setStyleSheet("color:#71808b; font-size:11px;")
        layout.addWidget(self.channel_detail)

        self.fault_detail = QLabel("Faults · Waiting for data")
        self.fault_detail.setWordWrap(True)
        self.fault_detail.setStyleSheet("color:#71808b; font-size:11px;")
        layout.addWidget(self.fault_detail)

    def _make_lamp_row(self, caption: str, row_tag: str) -> QWidget:
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        label = QLabel(caption)
        label.setFixedWidth(52)
        label.setStyleSheet("color:#8a98a2; font-size:9px; font-weight:700; letter-spacing:0.5px;")
        layout.addWidget(label)
        lamps = []
        for channel in range(self.n_channels):
            lamp = QLabel()
            lamp.setStyleSheet(_lamp_style(_LAMP_OFF))
            lamp.setToolTip(f"Channel {channel + 1}")
            layout.addWidget(lamp)
            lamps.append(lamp)
        layout.addStretch(1)
        self._lamp_rows[row_tag] = lamps
        return row

    def _set_status_style(self, level: str):
        color, background = _LEVEL_STYLE.get(level, _LEVEL_STYLE["unknown"])
        self.status_label.setStyleSheet(
            f"background:{background}; color:{color}; border:1px solid {background};"
            "border-radius:9px; padding:5px 9px; font-size:10px; font-weight:700;"
        )

    def update_snapshot(self, snap: DeviceSnapshot):
        self.last_ts = snap.timestamp
        self._last_snapshot = snap
        snapshot_key = (
            snap.system_status,
            snap.status_level,
            tuple(snap.channels_on),
            tuple(snap.channels_width),
            tuple(snap.channels_fault),
            snap.has_width_data,
            snap.has_fault_data,
            tuple(sorted(snap.fault_types.items())),
        )
        if snapshot_key == self._snapshot_key and not self._stale:
            return
        self._stale = False
        self._snapshot_key = snapshot_key
        self.status_label.setText(snap.system_status)
        self.status_label.setToolTip("")
        self._set_status_style(snap.status_level)

        for channel in range(self.n_channels):
            if not snap.has_width_data:
                on_color = _LAMP_UNKNOWN
            elif snap.channels_on[channel]:
                on_color = _LAMP_ON
            else:
                on_color = _LAMP_OFF

            if not snap.has_fault_data:
                fault_color = _LAMP_UNKNOWN
            elif snap.channels_fault[channel]:
                fault_color = _LAMP_FAULT
            else:
                fault_color = _LAMP_OFF
            self._lamp_rows["on"][channel].setStyleSheet(_lamp_style(on_color))
            self._lamp_rows["fault"][channel].setStyleSheet(_lamp_style(fault_color))

        if snap.has_width_data:
            channels = ", ".join(
                f"CH{i + 1}: {width}"
                for i, width in enumerate(snap.channels_width)
            )
            self.channel_detail.setText(f"Channels · {channels}")
        else:
            self.channel_detail.setText("Channels · No data")

        if snap.fault_types:
            faults = ", ".join(
                f"CH{channel + 1}: {description}"
                for channel, description in sorted(snap.fault_types.items())
            )
            self.fault_detail.setText(f"Faults · {faults}")
        elif snap.has_fault_data:
            self.fault_detail.setText("Faults · None")
        else:
            self.fault_detail.setText("Faults · No data")

    def refresh_staleness(self, now: float):
        """超过 OFFLINE_AFTER_S 没有新帧即视为芯片掉线，徽章显示 OFFLINE。"""
        if self._last_snapshot is None or self.last_ts <= 0.0:
            return
        age = now - self.last_ts
        if age <= OFFLINE_AFTER_S:
            return
        if self._stale:
            return
        self._stale = True
        self.status_label.setText("OFFLINE")
        self.status_label.setToolTip(f"No frame received for {age:.1f}s")
        self._set_status_style("error")


class StatusPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards: dict[int, DeviceCard] = {}
        self._chip = DEFAULT_CHIP
        self._channels = CHIP_CHANNELS[DEFAULT_CHIP]
        self._connected = False
        self._conn_desc = ""
        self._discovered: set[int] = set()
        self._placeholder_card: DeviceCard | None = None
        self._init_ui()
        self._rebuild()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check_offline)
        self._timer.start(1000)

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.card = QFrame()
        self.card.setObjectName("fleet_card")
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(13)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        eyebrow = QLabel("BUS OVERVIEW")
        eyebrow.setObjectName("section_eyebrow")
        title = QLabel("Device fleet")
        title.setObjectName("panel_title")
        self.subtitle = QLabel("Devices appear as soon as a valid frame is received")
        self.subtitle.setObjectName("panel_subtitle")
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.device_count = QLabel("0 DEVICES")
        self.device_count.setStyleSheet(
            "background:#f0f4f7; color:#657582; border:1px solid #e3e9ed;"
            "border-radius:10px; padding:7px 10px; font-size:10px; font-weight:700;"
        )
        header.addWidget(self.device_count, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        self._grid = QGridLayout(container)
        self._grid.setContentsMargins(1, 1, 1, 1)
        self._grid.setSpacing(11)
        self.scroll.setWidget(container)

        layout.addWidget(self.scroll, 1)
        outer.addWidget(self.card)
        self._refresh_empty_hint()

    def set_chip(self, chip: str):
        if chip == self._chip:
            return
        self._chip = chip
        self._channels = CHIP_CHANNELS.get(chip, self._channels)
        self.clear()
        self._refresh_empty_hint()

    def set_connection(self, connected: bool, description: str = ""):
        self._connected = connected
        self._conn_desc = description
        if not connected:
            self.clear()
        self._refresh_empty_hint()

    def _refresh_empty_hint(self):
        if not self._discovered:
            self.device_count.setText("0 DEVICES")
            if self._connected:
                self.subtitle.setText(
                    f"Listening on {self._conn_desc} · waiting for the first {self._chip} report"
                )
            else:
                self.subtitle.setText(
                    f"Select a port to begin monitoring {self._chip} · {self._channels} channels"
                )

    def update_device(self, snap: DeviceSnapshot):
        if snap.address not in self._discovered:
            self._discovered.add(snap.address)
            self._rebuild()
        self._cards[snap.address].update_snapshot(snap)

    def invalidate_data(self):
        """A receive overflow makes all inferred device state untrustworthy."""
        self.clear()
        self.device_count.setText("STATE UNKNOWN")
        self.subtitle.setText("Data loss detected — waiting for a complete PWM state")

    def clear(self):
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._discovered.clear()
        self._rebuild()

    def _rebuild(self):
        addrs = sorted(self._discovered)
        for address in list(self._cards):
            if address not in addrs:
                card = self._cards.pop(address)
                card.setParent(None)
                card.deleteLater()
        for address in addrs:
            if address not in self._cards:
                self._cards[address] = DeviceCard(address, self._channels)

        if addrs:
            if self._placeholder_card is not None:
                self._placeholder_card.setParent(None)
                self._placeholder_card.deleteLater()
                self._placeholder_card = None
            for index, address in enumerate(addrs):
                self._grid.addWidget(self._cards[address], index // 2, index % 2)
            self.device_count.setText(f"{len(addrs)} DEVICE" + ("S" if len(addrs) != 1 else ""))
            self.subtitle.setText(f"Monitoring {self._chip} · {self._channels} channels")
        else:
            for card in self._cards.values():
                card.setParent(None)
                card.deleteLater()
            self._cards.clear()
            if self._placeholder_card is None or self._placeholder_card.n_channels != self._channels:
                if self._placeholder_card is not None:
                    self._placeholder_card.setParent(None)
                    self._placeholder_card.deleteLater()
                self._placeholder_card = DeviceCard(None, self._channels, placeholder=True)
            self._grid.addWidget(self._placeholder_card, 0, 0)
            self._refresh_empty_hint()

    def _check_offline(self):
        now = time.time()
        for card in self._cards.values():
            card.refresh_staleness(now)
