"""串口工作线程：所有串口 I/O 都在这个线程里完成。

UI 线程通过 open_port / close_port / write_data 下发命令（内部走命令队列），
收到的数据通过 data_received 信号以 Qt 队列连接安全地跨线程送回 UI。
"""

import queue
from collections import deque
from threading import Lock

import serial
from PySide6.QtCore import QThread, Signal

_OPEN = "open"
_CLOSE = "close"
_WRITE = "write"
RX_BUFFER_BYTES = 256 * 1024


class BoundedReceiveBuffer:
    """Thread-safe byte FIFO with bounded memory and loss reporting."""

    def __init__(self, max_bytes: int):
        self._max_bytes = max_bytes
        self._chunks = deque()
        self._size = 0
        self._dropped_bytes = 0
        self._received_bytes = 0
        self._overflow_count = 0
        self._high_water_bytes = 0
        self._notification_pending = False
        self._lock = Lock()

    def push(self, data: bytes) -> bool:
        """Add data and report whether the GUI needs one wake-up signal."""
        with self._lock:
            self._received_bytes += len(data)
            if len(data) > self._max_bytes:
                self._overflow_count += 1
                self._dropped_bytes += self._size + len(data) - self._max_bytes
                self._chunks.clear()
                self._size = 0
                data = data[-self._max_bytes:]
            elif self._size + len(data) > self._max_bytes:
                # Drop stale queued data: it cannot safely describe current hardware state.
                self._overflow_count += 1
                self._dropped_bytes += self._size
                self._chunks.clear()
                self._size = 0

            self._chunks.append(data)
            self._size += len(data)
            self._high_water_bytes = max(self._high_water_bytes, self._size)
            notify = not self._notification_pending
            self._notification_pending = True
            return notify

    def drain(self) -> tuple[bytes, int]:
        with self._lock:
            data = b"".join(self._chunks)
            dropped = self._dropped_bytes
            self._chunks.clear()
            self._size = 0
            self._dropped_bytes = 0
            self._notification_pending = False
            return data, dropped

    def discard(self):
        with self._lock:
            self._chunks.clear()
            self._size = 0
            self._dropped_bytes = 0
            self._notification_pending = False

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "received_bytes": self._received_bytes,
                "fifo_depth_bytes": self._size,
                "fifo_high_water_bytes": self._high_water_bytes,
                "fifo_overflow_count": self._overflow_count,
            }


class SerialWorker(QThread):
    data_available = Signal()
    port_opened = Signal()
    port_closed = Signal()
    error_occurred = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._commands: queue.Queue = queue.Queue()
        self._received = BoundedReceiveBuffer(RX_BUFFER_BYTES)
        self._running = True

    # ---- UI 线程调用的公开接口（仅入队，不做实际 I/O）----

    def open_port(self, settings: dict):
        """settings 为 pyserial Serial 的关键字参数：
        port / baudrate / bytesize / parity / stopbits / timeout
        """
        self._commands.put((_OPEN, dict(settings)))

    def close_port(self):
        self._commands.put((_CLOSE, None))

    def write_data(self, data: bytes):
        self._commands.put((_WRITE, bytes(data)))

    def stop(self):
        self._running = False
        self._commands.put((_CLOSE, None))

    def drain_received(self) -> tuple[bytes, int]:
        return self._received.drain()

    def discard_received(self):
        self._received.discard()

    def receive_stats(self) -> dict[str, int]:
        return self._received.stats()

    # ---- 工作线程主体 ----

    def run(self):
        ser = None
        while self._running:
            try:
                # 1. 处理所有待处理命令
                while True:
                    try:
                        cmd, payload = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if cmd == _OPEN:
                        if ser is not None:
                            ser.close()
                            ser = None
                        self._received.discard()
                        try:
                            ser = serial.Serial(**payload)
                            # 尽力扩大驱动层 RX 缓冲，降低高速率下丢字节导致
                            # 整帧报废的风险（Windows SetupComm，其他平台忽略）。
                            try:
                                ser.set_buffer_size(rx_size=RX_BUFFER_BYTES)
                            except Exception:
                                pass
                            self.port_opened.emit()
                        except (serial.SerialException, ValueError, OSError) as e:
                            # ValueError: pyserial 仅支持 5-8 数据位等参数校验失败
                            self.error_occurred.emit(f"Failed to open port: {e}")
                            ser = None
                    elif cmd == _CLOSE:
                        if ser is not None:
                            ser.close()
                            ser = None
                            self.port_closed.emit()
                    elif cmd == _WRITE and ser is not None and payload:
                        ser.write(payload)

                # 2. 读取数据（timeout 由 open 时的 settings 控制）
                if ser is not None:
                    data = ser.read(max(ser.in_waiting, 1))
                    if data:
                        if self._received.push(bytes(data)):
                            self.data_available.emit()
                else:
                    self.msleep(50)
            except (serial.SerialException, OSError) as e:
                # Unplugged cable etc.: close the port and notify the UI
                self.error_occurred.emit(f"Serial error: {e}")
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                    self.port_closed.emit()

        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass


def list_ports() -> list[str]:
    """枚举可用串口，返回 [(描述, 设备名), ...] 形式的列表。"""
    from serial.tools import list_ports as _lp

    return [(p.description, p.device) for p in _lp.comports()]
