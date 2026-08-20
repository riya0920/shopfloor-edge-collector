"""Protocol drivers. One per family, all behind the same interface.

The interface is the point: the collector calls `poll()` and gets back
(tag_name, value_or_None) pairs. Adding a protocol means adding a driver, not
touching the collector, the buffer, or the uplink.

Connection state machine, per device:

    DISCONNECTED --connect--> CONNECTED --error--> BACKOFF --timer--> DISCONNECTED

Backoff is exponential WITH JITTER. Without jitter, a plant that loses a switch
brings every gateway back at the same instant and the reconnect storm looks like
the outage continuing. The jitter is one line and it is the difference between a
recovery and a second incident.
"""
from __future__ import annotations

import asyncio
import random
import time

from devices import DeviceProfile, parse_ascii_frame, registers_to_float


class Driver:
    def __init__(self, profile: DeviceProfile):
        self.profile = profile
        self.connected = False
        self.backoff_s = 0.25
        self.reconnects = 0
        self.errors = 0

    async def connect(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    async def poll(self) -> dict[str, float | None]:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        pass

    async def backoff(self) -> None:
        """Exponential backoff with jitter, capped."""
        wait = min(self.backoff_s, 5.0) * (0.5 + random.random())
        await asyncio.sleep(wait)
        self.backoff_s = min(self.backoff_s * 2, 5.0)

    def on_success(self) -> None:
        self.backoff_s = 0.25


class ModbusDriver(Driver):
    """Modbus TCP. Reads holding registers per the configured register map."""

    def __init__(self, profile: DeviceProfile):
        super().__init__(profile)
        self.client = None

    async def connect(self) -> bool:
        from pymodbus.client import AsyncModbusTcpClient

        try:
            self.client = AsyncModbusTcpClient(self.profile.host, port=self.profile.port)
            await self.client.connect()
            self.connected = bool(self.client.connected)
            if self.connected:
                self.reconnects += 1
                self.on_success()
            return self.connected
        except Exception:
            self.connected = False
            return False

    async def poll(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        if not self.connected or self.client is None:
            return {t.name: None for t in self.profile.tags}
        for tag in self.profile.tags:
            count = 2 if tag.kind == "float32" else 1
            try:
                rr = await self.client.read_holding_registers(
                    address=tag.address, count=count, device_id=self.profile.unit_id)
                if rr.isError():
                    self.errors += 1
                    out[tag.name] = None
                    continue
                regs = list(rr.registers)
                if tag.kind == "float32":
                    # WORD ORDER. This is the config field that turns a temperature
                    # of 1.7e19 back into 68.3, and there is no way to detect it
                    # from the wire -- the bytes are valid either way. It is a
                    # site-survey fact, which is why it lives in the register map.
                    val = registers_to_float(regs, tag.word_swap)
                else:
                    val = float(regs[0])
                out[tag.name] = val * tag.scale
            except Exception:
                self.errors += 1
                out[tag.name] = None
                self.connected = False
        return out

    async def close(self) -> None:
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass


class AsciiDriver(Driver):
    """The 1998 machine: framed ASCII over a TCP bridge, with a checksum."""

    def __init__(self, profile: DeviceProfile):
        super().__init__(profile)
        self.reader = None
        self.writer = None
        self.rejected_frames = 0
        self._buf = bytearray()

    async def connect(self) -> bool:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.profile.host, self.profile.port),
                timeout=2.0)
            self.connected = True
            self.reconnects += 1
            self.on_success()
            return True
        except Exception:
            self.connected = False
            return False

    async def poll(self) -> dict[str, float | None]:
        if not self.connected or self.reader is None:
            return {t.name: None for t in self.profile.tags}
        try:
            chunk = await asyncio.wait_for(self.reader.read(4096), timeout=2.0)
            if not chunk:
                self.connected = False
                return {t.name: None for t in self.profile.tags}
            self._buf.extend(chunk)
        except (asyncio.TimeoutError, ConnectionResetError, OSError):
            self.connected = False
            return {t.name: None for t in self.profile.tags}

        # FRAME ON STX/ETX, not on newlines. A corrupted byte can be a newline,
        # and a parser that splits on '\n' will happily cut a frame in half and
        # then parse both halves as if they were data.
        result: dict[str, float | None] = {}
        while True:
            try:
                start = self._buf.index(0x02)
                end = self._buf.index(0x03, start)
            except ValueError:
                break
            frame = bytes(self._buf[start:end + 1])
            del self._buf[:end + 1]
            vals = parse_ascii_frame(frame)
            if vals is None:
                # Checksum failure. REJECT, do not parse optimistically. A frame
                # that fails its checksum is not "mostly right".
                self.rejected_frames += 1
                self.errors += 1
                continue
            for t in self.profile.tags:
                if t.name in vals:
                    result[t.name] = vals[t.name] * t.scale
        if not result:
            return {}
        return result

    async def close(self) -> None:
        try:
            if self.writer:
                self.writer.close()
        except Exception:
            pass


def make_driver(profile: DeviceProfile) -> Driver:
    return {"modbus": ModbusDriver, "ascii": AsciiDriver}[profile.protocol](profile)
