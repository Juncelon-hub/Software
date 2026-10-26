"""TPS92662A-Q1 总线监控解析（依据 SLUSDL7A Rev.A 数据手册 8.4.5 / 8.5 节实现）。

总线上的帧类型（手册 8.4.5.6）：
  写命令   INIT DEVID REGADDR DATA1..DATAn CRCL CRCH        (广播写 DEVID=0xBF，无应答)
  读命令   INIT DEVID REGADDR CRCL CRCH
  写应答   0x7F (仅 ACKEN=1 时)
  读应答   DATA1..DATAn CRCL CRCH                            (无 INIT/DEVID/REGADDR)

监控原理：本程序是被动监听端。
  1. 以 INIT 字节锚定命令帧；CRC-16-IBM(init=0x0000, poly=0xA001) 校验，
     已用手册 8.4.5.7.7 的 4 组样例验证。
  2. 收到读命令后，紧随其后的 N+2 字节即该设备读应答，据此还原寄存器真值。
  3. 为每颗芯片维护寄存器影子，从 WIDTH 寄存器推算点亮通道、
     从 FAULTL/FAULTH 推算故障通道（手册：开路与短路共用同一 FAULTx 位）。
"""

import time
from collections import namedtuple
from dataclasses import dataclass, field

NUM_CHANNELS = 12
BROADCAST_DEVID = 0xBF
ACK_BYTE = 0x7F
# CRC 校验开关。开启后 CRC 失败的帧被拒绝并逐字节重同步；
# 配合 _rescue_0x33 可抢救“仅 INIT 字节在线路上损坏”的 0x33 帧。
# 若现场所有帧都报 CRC FAIL，说明 ECU 的 CRC 参数与手册不一致，应改回 False。
CRC_CHECK_ENABLED = True

# ---- 状态推断范围 ----
# True: 仅 0x33 帧参与寄存器影子与 OUTPUT 状态推断；
# False: 所有写命令与读应答都参与状态推断（GUI 同时开放非 0x33 报文）。
ONLY_0x33_OUTPUT_MODE = False

# ---- 支持的目标芯片及通道数 ----
# 注意：目前仅 TPS92662A 的寄存器映射经过手册验证；
# 其余芯片暂时沿用 92662A 解析，待提供对应手册后逐芯片适配。
CHIP_CHANNELS = {
    "TPS92662A": 12,
    "TPS92663A": 6,
    "TPS92664": 12,
    "TPS929240": 12,
    "TLD7002": 16,
}
DEFAULT_CHIP = "TPS92662A"

# ---- 帧初始化字节（手册 Table 5）----
INIT_WRITE = {1: 0x87, 2: 0x99, 3: 0x1E, 4: 0xAA, 12: 0x2D, 16: 0x33, 32: 0xB4}
INIT_READ = {1: 0x4B, 2: 0xCC, 3: 0xD2, 4: 0x55, 12: 0xE1, 16: 0x66, 32: 0x78}
WRITE_LEN = {v: k for k, v in INIT_WRITE.items()}
READ_LEN = {v: k for k, v in INIT_READ.items()}
VALID_INIT = set(WRITE_LEN) | set(READ_LEN)

# ---- DEVID 字节 <-> dev_id（手册 Table 7，含奇偶校验位）----
DEVID_TO_ID = {
    0x20: 0x00, 0x61: 0x01, 0xE2: 0x02, 0xA3: 0x03, 0x64: 0x04, 0x25: 0x05,
    0xA6: 0x06, 0xE7: 0x07, 0xA8: 0x08, 0xE9: 0x09, 0x6A: 0x0A, 0x2B: 0x0B,
    0xEC: 0x0C, 0xAD: 0x0D, 0x2E: 0x0E, 0x6F: 0x0F, 0xF0: 0x10, 0xB1: 0x11,
    0x32: 0x12, 0x73: 0x13, 0xB4: 0x14, 0xF5: 0x15, 0x76: 0x16, 0x37: 0x17,
    0x78: 0x18, 0x39: 0x19, 0xBA: 0x1A, 0xFB: 0x1B, 0x3C: 0x1C, 0x7D: 0x1D,
    0xFE: 0x1E, 0xBF: 0x1F,
}

# ---- 关键寄存器地址（手册 Table 12）----
REG_MWIDTH_L0 = 0x10        # MWIDTH01L..MWIDTH12L: WIDTHn[7:0]，0x10..0x1B
REG_MWIDTH_H = (0x13, 0x17, 0x1B, 0x1F)   # 打包的 WIDTH[9:8]，每组 3 通道
REG_LWIDTH_H0 = 0x30        # LWIDTH01H..LWIDTH12H: WIDTHn[9:2]，0x30..0x3B
REG_LWIDTH_L = (0x3C, 0x3D, 0x3E, 0x3F)   # 打包的 WIDTH[1:0]
REG_DWIDTH_L0 = 0x42        # DWIDTH01L=0x42, DWIDTH01H=0x43，每通道 4 字节
REG_SYSCFG = 0x80
REG_ADC1, REG_ADC2 = 0xA0, 0xA1
REG_FAULTL, REG_FAULTH, REG_CERRCNT = 0xB0, 0xB1, 0xB2
REG_ICID = 0xFF

# WIDTH 寄存器区域（M/L/D 三套映射，手册 Table 12）
WIDTH_MAP_RANGES = ((0x10, 0x20), (0x30, 0x40), (0x42, 0x72))


def is_width_write(regaddr: int, length: int) -> bool:
    """判断一次写操作是否覆盖任何 WIDTH 映射区域（用于 0x33 指示器着色）。"""
    return any(not (regaddr + length <= lo or regaddr >= hi) for lo, hi in WIDTH_MAP_RANGES)


def crc16_ibm(data: bytes) -> int:
    """CRC-16-IBM（= CRC-16/ARC：init 0x0000，反射 poly 0xA001），返回 16 位值。"""
    crc = 0x0000
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


# =========================================================================
# 设备状态影子
# =========================================================================

_WIDTH_MAPS = {
    # 名称: (寄存器集合, 计算函数(shadow) -> [12] 个 10bit WIDTH)
    "M": (
        set(range(0x10, 0x20)),
        lambda s: [
            s[0x10 + i] | (((s[REG_MWIDTH_H[i // 3]] >> (2 * (i % 3))) & 3) << 8)
            for i in range(NUM_CHANNELS)
        ],
    ),
    "L": (
        set(range(0x30, 0x40)),
        lambda s: [
            (s[REG_LWIDTH_H0 + i] << 2)
            | ((s[REG_LWIDTH_L[i // 3]] >> (2 * (i % 3))) & 3)
            for i in range(NUM_CHANNELS)
        ],
    ),
    "D": (
        {0x42 + 4 * i + h for i in range(NUM_CHANNELS) for h in (0, 1)},
        lambda s: [
            s[REG_DWIDTH_L0 + 4 * i] | ((s[REG_DWIDTH_L0 + 4 * i + 1] & 3) << 8)
            for i in range(NUM_CHANNELS)
        ],
    ),
}


class DeviceState:
    """单颗芯片的寄存器影子（按字节地址）。"""

    def __init__(self, address: int):
        self.address = address
        self.regs: dict[int, int] = {}
        self.last_seen = 0.0
        self._map_ts = {"M": 0.0, "L": 0.0, "D": 0.0}   # 各 WIDTH 映射的最后更新时间

    def apply_write(self, regaddr: int, data: bytes):
        """写命令更新影子。FAULT 寄存器特殊：写 0 清除、写 1 无效（手册 8.5.22），
        因此用 与运算 模拟。"""
        for i, b in enumerate(data):
            reg = (regaddr + i) & 0xFF        # 越过 0xFF 回卷到 0x00
            if reg in (REG_FAULTL, REG_FAULTH):
                self.regs[reg] = self.regs.get(reg, 0) & b
            else:
                self.regs[reg] = b
        self.last_seen = time.time()
        for name, (regset, _fn) in _WIDTH_MAPS.items():
            if any((regaddr + i) & 0xFF in regset for i in range(len(data))):
                self._map_ts[name] = self.last_seen

    def apply_read(self, regaddr: int, data: bytes):
        """读应答是设备上报的真值，直接覆盖影子。"""
        for i, b in enumerate(data):
            self.regs[(regaddr + i) & 0xFF] = b
        self.last_seen = time.time()
        for name, (regset, _fn) in _WIDTH_MAPS.items():
            if any((regaddr + i) & 0xFF in regset for i in range(len(data))):
                self._map_ts[name] = self.last_seen

    def widths(self) -> list[int] | None:
        """取数据最新的完整 WIDTH 映射，返回 12 个 10bit 值；无完整映射返回 None。"""
        best_name, best_ts = None, -1.0
        for name, (regset, fn) in _WIDTH_MAPS.items():
            if regset <= self.regs.keys() and self._map_ts[name] > best_ts:
                best_name, best_ts = name, self._map_ts[name]
        if best_name is None:
            return None
        return _WIDTH_MAPS[best_name][1](self.regs)

    def faults(self) -> list[bool] | None:
        """FAULT(12:1)，低字节在前；无数据返回 None。"""
        if REG_FAULTL not in self.regs:
            return None
        low = self.regs[REG_FAULTL]
        high = self.regs.get(REG_FAULTH, 0)
        return [bool(low >> i & 1) for i in range(8)] + [bool(high >> i & 1) for i in range(4)]

    @property
    def cerrcnt(self) -> int:
        return self.regs.get(REG_CERRCNT, 0)


# =========================================================================
# 快照（UI 显示用）
# =========================================================================

@dataclass
class DeviceSnapshot:
    address: int
    system_status: str = "未知"
    status_level: str = "unknown"           # ok / warn / error / unknown
    channels_on: list[bool] = field(default_factory=lambda: [False] * NUM_CHANNELS)
    channels_width: list[int] = field(default_factory=lambda: [0] * NUM_CHANNELS)
    channels_fault: list[bool] = field(default_factory=lambda: [False] * NUM_CHANNELS)
    fault_types: dict[int, str] = field(default_factory=dict)
    has_width_data: bool = False            # 是否已从报文获得 WIDTH 数据
    has_fault_data: bool = False
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_state(cls, state: DeviceState, n_channels: int = NUM_CHANNELS) -> "DeviceSnapshot":
        snap = cls(address=state.address)
        faults = state.faults() or []
        widths_raw = state.widths()
        widths = widths_raw or []
        # 按目标芯片通道数截断/补齐（非 92662A 芯片的解析待适配，补齐位显示为熄灭）
        snap.channels_fault = faults[:n_channels] + [False] * (n_channels - len(faults))
        snap.channels_on = ([w > 0 for w in widths][:n_channels]
                            + [False] * (n_channels - len(widths)))
        snap.channels_width = (widths[:n_channels]
                               + [0] * (n_channels - len(widths)))
        snap.has_width_data = widths_raw is not None
        snap.has_fault_data = REG_FAULTL in state.regs
        if any(snap.channels_fault):
            snap.system_status = "Channel Fault"
            snap.status_level = "error"
            # Datasheet 8.3.9: open and short events share the same FAULTx bit
            snap.fault_types = {i: "LED Open/Short" for i, f in enumerate(snap.channels_fault) if f}
        elif state.cerrcnt > 0:
            snap.system_status = f"CRC Errors x{state.cerrcnt}"
            snap.status_level = "warn"
        else:
            # 设备已在线（收到过合法帧）即视为 Normal，不再因缺少 WIDTH 数据报异常。
            snap.system_status = "Normal"
            snap.status_level = "ok"
        return snap


FeedResult = namedtuple("FeedResult", "snapshots infos errors")


# =========================================================================
# 总线监听器
# =========================================================================

class BusMonitor:
    """被动监听 UART 字节流，切帧、校验 CRC、维护设备影子并产出快照。"""

    def __init__(self, chip: str = DEFAULT_CHIP):
        self._buf = bytearray()
        self._pos = 0
        self._devices: dict[int, DeviceState] = {}
        self._pending_read: tuple[int, int, int] | None = None   # (dev, regaddr, n)
        self._channels = CHIP_CHANNELS.get(chip, NUM_CHANNELS)
        # 最近一次 0x33 写帧：(时间戳, dev|None=广播, regaddr, data)，供 GUI 指示器显示
        self.last_0x33: tuple[float, int | None, int, bytes] | None = None

    def set_chip(self, chip: str):
        """切换目标芯片：更新通道数并清空所有设备影子。"""
        self._channels = CHIP_CHANNELS.get(chip, NUM_CHANNELS)
        self.reset()

    def reset(self):
        self._buf.clear()
        self._pos = 0
        self._pending_read = None
        self._devices.clear()
        self.last_0x33 = None

    def feed(self, data: bytes) -> FeedResult:
        self._buf.extend(data)
        dirty_devices: set[int] = set()
        infos: list[str] = []
        errors: list[str] = []
        while self._step(dirty_devices, infos, errors):
            pass
        self._compact_buffer()
        snapshots = [
            DeviceSnapshot.from_state(self._devices[address], self._channels)
            for address in sorted(dirty_devices)
            if address in self._devices
        ]
        return FeedResult(snapshots, infos, errors)

    def _available(self) -> int:
        return len(self._buf) - self._pos

    def _consume(self, count: int):
        self._pos += count

    def _compact_buffer(self):
        """Avoid O(n) front deletion for every parsed frame."""
        if self._pos == len(self._buf):
            self._buf.clear()
            self._pos = 0
        elif self._pos >= 4096 and self._pos * 2 >= len(self._buf):
            del self._buf[:self._pos]
            self._pos = 0

    # ---- 单步状态机：返回 True 表示还有进展，可继续 ----

    def _step(self, dirty_devices, infos, errors) -> bool:
        buf = self._buf
        pos = self._pos
        if self._available() <= 0:
            return False

        # 1. 读应答：仅当 CRC 确认时才按应答消费；CRC 不确认的字节绝不
        # 盲目吞掉——否则紧随其后的 0x33 帧前几个字节会被当作“应答”吃掉，
        # 残帧既无法解析也无法抢救（这正是偶发“灯灭但 GUI 不更新”的根因）。
        if self._pending_read is not None:
            dev, regaddr, n = self._pending_read
            need = n + 2
            # 命令帧优先：应答没等到就被新命令打断（或应答根本没上总线）。
            if buf[pos] in VALID_INIT and self._try_match_command_crc():
                self._pending_read = None
                return True
            if self._available() < need:
                return False
            chunk = bytes(buf[pos:pos + need])
            crc_rx = chunk[n] | (chunk[n + 1] << 8)          # 低字节在前
            crc_ok = crc16_ibm(chunk[:n]) == crc_rx
            if not crc_ok:
                # 0x33 写帧分块到达时等待剩余字节，避免误判。
                if (buf[pos] == INIT_WRITE[16] and self._available() >= 2
                        and buf[pos + 1] in DEVID_TO_ID and self._available() < 21):
                    return False
                # 不可信的“应答”：丢弃 pending 状态，交回逐字节同步/抢救逻辑。
                self._pending_read = None
                infos.append(
                    f"Pending read 0x{dev:02X} REG 0x{regaddr:02X} [{n}B] "
                    f"abandoned: response not CRC-confirmed, resynced"
                )
                errors.append("Read response lost or corrupted; resynchronized")
                return True
            self._consume(need)
            self._pending_read = None
            state = self._devices.get(dev)
            # 临时措施：读应答不参与状态推断（仅 0x33 帧驱动 GUI）。
            if state is not None and not ONLY_0x33_OUTPUT_MODE:
                state.apply_read(regaddr, chunk[:n])
                dirty_devices.add(dev)
            infos.append(
                f"Resp 0x{dev:02X} REG 0x{regaddr:02X} [{n}B] "
                f"{chunk[:n].hex(' ').upper()} | CRC OK"
            )
            return True

        # 2. 同步：逐字节扫描帧头。不能跳到“下一个 INIT 样字节”——
        # 那可能落在真实帧的数据区里，把真帧吃掉一段。
        if buf[pos] not in VALID_INIT:
            verdict = self._rescue_0x33(dirty_devices, infos, errors)
            if verdict == "handled":
                return True
            if verdict == "wait":
                return False
            self._consume(1)
            return True

        # 3. 命令帧
        init = buf[pos]
        if init in WRITE_LEN:
            return self._take_write(dirty_devices, infos, errors)
        return self._take_read(infos, errors)

    # ---- 写命令：INIT DEVID REGADDR DATA(n) CRCL CRCH ----

    def _take_write(self, dirty_devices, infos, errors) -> bool:
        buf = self._buf
        pos = self._pos
        n = WRITE_LEN[buf[pos]]
        total = n + 5   # INIT DEVID REGADDR DATA(n) CRCL CRCH
        if self._available() < total:
            return False
        frame = bytes(buf[pos:pos + total])
        devid = frame[1]
        if devid not in DEVID_TO_ID:
            # 假帧头（噪声字节恰好等于 INIT 编码）：只丢弃这个字节并重新同步，
            # 避免按错误长度吞掉后面真实帧的内容。
            self._consume(1)
            infos.append(
                f"False sync 0x{frame[0]:02X}: invalid DEVID 0x{devid:02X}, resynced"
            )
            return True
        crc_rx = frame[-2] | (frame[-1] << 8)                # CRCL CRCH
        crc_ok = crc16_ibm(frame[:-2]) == crc_rx
        if CRC_CHECK_ENABLED and not crc_ok:
            # Drop only the suspect INIT byte, then resynchronize on remaining bytes.
            self._consume(1)
            infos.append(f"Bad frame {frame.hex(' ').upper()} | CRC FAIL, resynced")
            errors.append("Write command CRC check failed, frame dropped")
            return True

        self._consume(total)
        regaddr, data = frame[2], frame[3:-2]
        broadcast = devid == BROADCAST_DEVID
        dev = DEVID_TO_ID.get(devid, devid & 0x1F)
        tag = "BcastW" if broadcast else "Write"
        target = "All" if broadcast else f"0x{dev:02X}"
        crc_txt = "CRC OK" if crc_ok else "CRC FAIL (accepted)"
        infos.append(
            f"{tag} {target} REG 0x{regaddr:02X} [{n}B] {data.hex(' ').upper()} | {crc_txt}"
        )
        ts = time.time()
        if n == 16:
            self.last_0x33 = (ts, None if broadcast else dev, regaddr, data)
        # 状态推断范围由 ONLY_0x33_OUTPUT_MODE 控制。
        if ONLY_0x33_OUTPUT_MODE and n != 16:
            return True
        if broadcast:
            for state in self._devices.values():
                state.apply_write(regaddr, data)
                dirty_devices.add(state.address)
        else:
            state = self._devices.setdefault(dev, DeviceState(dev))
            state.apply_write(regaddr, data)
            dirty_devices.add(dev)
        return True

    # ---- 读命令：INIT DEVID REGADDR CRCL CRCH ----

    def _take_read(self, infos, errors) -> bool:
        buf = self._buf
        pos = self._pos
        n = READ_LEN[buf[pos]]
        total = 5
        if self._available() < total:
            return False
        frame = bytes(buf[pos:pos + total])
        devid = frame[1]
        if devid not in DEVID_TO_ID:
            self._consume(1)
            infos.append(
                f"False sync 0x{frame[0]:02X}: invalid DEVID 0x{devid:02X}, resynced"
            )
            return True
        crc_rx = frame[-2] | (frame[-1] << 8)
        crc_ok = crc16_ibm(frame[:-2]) == crc_rx
        if CRC_CHECK_ENABLED and not crc_ok:
            self._consume(1)
            infos.append(f"Bad frame {frame.hex(' ').upper()} | CRC FAIL, resynced")
            errors.append("Read command CRC check failed, frame dropped")
            return True

        self._consume(total)
        regaddr = frame[2]
        dev = DEVID_TO_ID.get(devid, devid & 0x1F)
        crc_txt = "CRC OK" if crc_ok else "CRC FAIL (accepted)"
        infos.append(f"Read 0x{dev:02X} REG 0x{regaddr:02X} [{n}B] | {crc_txt}")
        self._devices.setdefault(dev, DeviceState(dev))
        self._pending_read = (dev, regaddr, n)               # 等待 N+2 字节应答
        return True

    def _try_match_command_crc(self) -> bool:
        """判断队首能否构成 CRC 正确的完整命令帧（用于识别丢失的读应答）。"""
        buf = self._buf
        pos = self._pos
        init = buf[pos]
        n = WRITE_LEN.get(init) or READ_LEN.get(init)
        if n is None:
            return False
        total = (n + 5) if init in WRITE_LEN else 5
        if self._available() < total or buf[pos + 1] not in DEVID_TO_ID:
            return False
        frame = bytes(buf[pos:pos + total])
        return crc16_ibm(frame[:-2]) == (frame[-2] | (frame[-1] << 8))

    # ---- INIT 损坏帧抢救（临时模式专用）----

    # 0x33 帧去掉 INIT 后剩余：DEVID REGADDR DATA(16) CRCL CRCH = 20 字节
    _RESCUE_FRAME_BYTES = 20

    def _rescue_0x33(self, dirty_devices, infos, errors) -> str | None:
        """抢救 INIT 字节被干扰损坏的 0x33 帧。

        现场干扰下 0x33 可能变成其他值，导致整帧对同步逻辑隐身。
        此时 DEVID（含奇偶校验位）、REGADDR、数据与 CRC 仍完好；
        补回 0x33 重算 CRC 若吻合，即密码学级确认这是那帧丢失的报文。

        返回 "handled"（已处理）/ "wait"（字节不足，等待）/ None（不是候选）。
        """
        buf = self._buf
        pos = self._pos
        avail = self._available()
        # 判定候选帧头至少需要 2 字节。若当前只有 1 字节且它可能是 DEVID，
        # 必须等待而不能丢弃：INIT 损坏帧的 DEVID 一旦被当噪声吞掉，
        # 整帧就再无法抢救（帧分块到达时必现此问题）。
        if avail < 2:
            return "wait" if buf[pos] in DEVID_TO_ID else None
        devid = buf[pos]
        regaddr = buf[pos + 1]
        if devid not in DEVID_TO_ID or regaddr > 0x70:
            return None
        if avail < self._RESCUE_FRAME_BYTES:
            # 帧还在到达中，等字节到齐后再用 CRC 判定。
            # 不能因为后面出现“像 INIT 的字节”就放弃：0x33 帧的 16 字节数据区
            # 完全可能含有 0x33/0x2D 等数值，那只是数据而非新帧头。
            return "wait"
        frame = bytes(buf[pos:pos + self._RESCUE_FRAME_BYTES])
        crc_rx = frame[-2] | (frame[-1] << 8)
        crc_ok = crc16_ibm(bytes([INIT_WRITE[16]]) + frame[:-2]) == crc_rx
        if not crc_ok:
            # CRC 不确认 → 噪声概率大（INIT 损坏 + 内容也坏的概率低得多）。
            return None
        self._consume(self._RESCUE_FRAME_BYTES)
        data = frame[2:-2]
        dev = DEVID_TO_ID[devid]
        broadcast = devid == BROADCAST_DEVID
        tag = "BcastW" if broadcast else "Write"
        target = "All" if broadcast else f"0x{dev:02X}"
        infos.append(
            f"RESCUED {tag} {target} REG 0x{regaddr:02X} [16B] {data.hex(' ').upper()} "
            f"| INIT byte lost on wire, CRC confirmed"
        )
        errors.append("Recovered a 0x33 frame whose INIT byte was corrupted on the wire")
        self.last_0x33 = (time.time(), None if broadcast else dev, regaddr, data)
        if broadcast:
            for state in self._devices.values():
                state.apply_write(regaddr, data)
                dirty_devices.add(state.address)
        else:
            state = self._devices.setdefault(dev, DeviceState(dev))
            state.apply_write(regaddr, data)
            dirty_devices.add(dev)
        return "handled"
