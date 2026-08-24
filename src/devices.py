"""Device simulation. Heterogeneity ON PURPOSE, because plants are museums.

Three device families, chosen so the collector has to solve the three problems
that actually consume an integrator's week:

1. MODBUS TCP (pymodbus servers). 16-bit registers, so anything wider than an
   integer is a convention rather than a type. Two devices are big-endian
   word-order and one is WORD-SWAPPED, because on any real site one of them is,
   the drawing does not say which, and the symptom is a temperature of 1.7e19.

2. A "SERIAL-ERA" ASCII device behind a TCP bridge, with framing and a checksum.
   The 1998 machine is always the critical one and it never speaks anything
   modern. Its protocol is defined here and parsed by the collector.

3. FROZEN DEVICE behaviour: a device that answers every poll, on time, with
   plausible values that never change again. This is the sneakiest failure in
   industrial data collection because nothing errors, no connection drops, and
   the historian fills with beautiful flat lines that look like a stable process.

Chaos hooks on all of them: connection drops, garbage frames, freezes, drift.
"""
from __future__ import annotations

import asyncio
import math
import struct
import time
from dataclasses import dataclass, field

import zlib

import numpy as np


# --------------------------------------------------------------------------
# register encoding
# --------------------------------------------------------------------------

def float_to_registers(value: float, word_swap: bool = False) -> list[int]:
    """IEEE-754 float32 into two 16-bit Modbus registers.

    Modbus has no float type. It has 16-bit registers, and a float is a
    convention that both ends have to agree on. The word order is the part
    nobody documents.
    """
    raw = struct.pack(">f", float(value))
    hi, lo = struct.unpack(">HH", raw)
    return [lo, hi] if word_swap else [hi, lo]


def registers_to_float(regs: list[int], word_swap: bool = False) -> float:
    hi, lo = (regs[1], regs[0]) if word_swap else (regs[0], regs[1])
    return float(struct.unpack(">f", struct.pack(">HH", hi, lo))[0])


@dataclass
class TagSpec:
    """One tag as the collector's config describes it."""
    name: str
    address: int
    kind: str                    # "float32" | "uint16" | "int16"
    scale: float = 1.0
    units: str = ""
    word_swap: bool = False
    deadband: float = 0.0
    min_plausible: float | None = None
    max_plausible: float | None = None


@dataclass
class DeviceProfile:
    name: str
    protocol: str                # "modbus" | "ascii"
    host: str
    port: int
    unit_id: int = 1
    tags: list[TagSpec] = field(default_factory=list)
    scan_ms: int = 500
    # chaos
    freeze_after_s: float | None = None
    drop_every_s: float | None = None
    garbage_rate: float = 0.0
    clock_offset_s: float = 0.0


# --------------------------------------------------------------------------
# the physical process behind the registers
# --------------------------------------------------------------------------

class ProcessModel:
    """What the device is measuring. Deterministic given (name, t) so the
    collector's output can be checked against ground truth to the sample."""

    def __init__(self, name: str, seed: int = 7):
        self.name = name
        # crc32, NOT hash(): Python salts str hashing per process (PEP 456), so
        # hash() differs on every interpreter start and this device would
        # generate different data on every run -- silently breaking the
        # reproducibility every report in this project claims.
        self.rng = np.random.default_rng(seed + zlib.crc32(name.encode()) % 9999)
        self.t0 = time.time()

    def values(self, t: float) -> dict[str, float]:
        p = 2 * math.pi
        return {
            "temperature_c": 68.0 + 6.0 * math.sin(p * t / 90.0) + self.rng.normal(0, 0.15),
            "pressure_bar": 4.2 + 0.4 * math.sin(p * t / 37.0) + self.rng.normal(0, 0.02),
            "flow_lpm": 120.0 + 18.0 * math.sin(p * t / 53.0) + self.rng.normal(0, 0.7),
            "vibration_mms": 2.1 + 0.5 * math.sin(p * t / 23.0) + self.rng.normal(0, 0.05),
            "part_count": float(int(t * 0.8)),
            "state_code": float(1 if math.sin(p * t / 120.0) > -0.6 else 3),
        }


# --------------------------------------------------------------------------
# Modbus TCP servers
# --------------------------------------------------------------------------

async def run_modbus_device(profile: DeviceProfile, model: ProcessModel,
                            stop: asyncio.Event, truth_log: list) -> None:
    """A pymodbus TCP server whose holding registers track the process."""
    from pymodbus.datastore import (ModbusSequentialDataBlock, ModbusServerContext,
                                    ModbusSlaveContext)
    from pymodbus.server import StartAsyncTcpServer

    block = ModbusSequentialDataBlock(0, [0] * 200)
    store = ModbusSlaveContext(hr=block, ir=block, zero_mode=True)
    context = ModbusServerContext(slaves={profile.unit_id: store}, single=False)

    async def updater():
        started = time.time()
        frozen_values = None
        while not stop.is_set():
            now = time.time()
            elapsed = now - started
            if profile.freeze_after_s and elapsed > profile.freeze_after_s:
                # THE FROZEN DEVICE. It keeps answering. The registers just stop
                # changing. Nothing anywhere reports an error.
                if frozen_values is None:
                    frozen_values = model.values(elapsed)
                vals = frozen_values
            else:
                vals = model.values(elapsed)

            for tag in profile.tags:
                v = vals.get(tag.name)
                if v is None:
                    continue
                raw = v / tag.scale
                if tag.kind == "float32":
                    block.setValues(tag.address, float_to_registers(raw, tag.word_swap))
                else:
                    block.setValues(tag.address, [int(max(0, min(65535, round(raw))))])
            truth_log.append({"device": profile.name, "t": now,
                              "frozen": bool(profile.freeze_after_s and
                                             elapsed > profile.freeze_after_s),
                              **{k: v for k, v in vals.items()}})
            await asyncio.sleep(0.2)

    task = asyncio.create_task(updater())
    server = await StartAsyncTcpServer(context=context,
                                       address=(profile.host, profile.port))
    task.cancel()
    return server


# --------------------------------------------------------------------------
# the cranky ASCII device
# --------------------------------------------------------------------------
#
# Frame format, defined here because in 1998 somebody defined it in a Word
# document that no longer exists:
#
#     <STX> ADDR ',' TAG '=' VALUE ... '*' CHECKSUM <ETX> CR LF
#
#     STX = 0x02, ETX = 0x03
#     CHECKSUM = XOR of every byte between STX and '*', as two uppercase hex chars
#
# Which is to say: NMEA-flavoured, because half the industrial ASCII protocols in
# existence are. The collector must frame on STX/ETX (not on newlines, because
# values can contain them under garbage conditions) and must reject a frame whose
# checksum does not match rather than parsing it optimistically.

STX, ETX = 0x02, 0x03


def ascii_checksum(payload: bytes) -> int:
    c = 0
    for b in payload:
        c ^= b
    return c


def build_ascii_frame(addr: int, values: dict[str, float]) -> bytes:
    body = f"{addr:02d}," + ",".join(f"{k}={v:.3f}" for k, v in values.items())
    payload = body.encode("ascii")
    return bytes([STX]) + payload + b"*" + f"{ascii_checksum(payload):02X}".encode() \
        + bytes([ETX]) + b"\r\n"


def parse_ascii_frame(frame: bytes) -> dict[str, float] | None:
    """Return values, or None if the frame is corrupt. Never guess."""
    try:
        if not frame or frame[0] != STX:
            return None
        end = frame.index(ETX)
        inner = frame[1:end]
        star = inner.rindex(ord("*"))
        payload, cks = inner[:star], inner[star + 1:]
        if int(cks, 16) != ascii_checksum(payload):
            return None
        parts = payload.decode("ascii").split(",")
        out = {}
        for p in parts[1:]:
            k, _, v = p.partition("=")
            out[k] = float(v)
        return out
    except Exception:
        return None


async def run_ascii_device(profile: DeviceProfile, model: ProcessModel,
                           stop: asyncio.Event, truth_log: list) -> asyncio.Server:
    """TCP bridge that pushes ASCII frames, occasionally corrupt, occasionally
    dropping the connection mid-stream."""
    rng = np.random.default_rng(11)
    started = time.time()

    async def handle(reader, writer):
        conn_start = time.time()
        try:
            while not stop.is_set():
                now = time.time()
                elapsed = now - started
                vals = model.values(elapsed)
                keep = {t.name: vals[t.name] for t in profile.tags if t.name in vals}
                frame = build_ascii_frame(profile.unit_id, keep)
                if rng.random() < profile.garbage_rate:
                    # Corrupt a byte in place. The checksum must catch it.
                    b = bytearray(frame)
                    i = int(rng.integers(1, max(2, len(b) - 4)))
                    b[i] = (b[i] + 7) % 256
                    frame = bytes(b)
                writer.write(frame)
                await writer.drain()
                truth_log.append({"device": profile.name, "t": now, "frozen": False,
                                  **keep})
                if profile.drop_every_s and (time.time() - conn_start) > profile.drop_every_s:
                    break  # drop the session; the collector must reconnect
                await asyncio.sleep(profile.scan_ms / 1000.0)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    return await asyncio.start_server(handle, profile.host, profile.port)


# --------------------------------------------------------------------------
# the fleet
# --------------------------------------------------------------------------

def default_fleet(base_port: int = 15020) -> list[DeviceProfile]:
    return [
        DeviceProfile(
            name="PRESS-01", protocol="modbus", host="127.0.0.1", port=base_port,
            unit_id=1, scan_ms=400,
            tags=[
                TagSpec("temperature_c", 0, "float32", units="degC",
                        deadband=0.2, min_plausible=-20, max_plausible=200),
                TagSpec("pressure_bar", 2, "float32", units="bar",
                        deadband=0.05, min_plausible=0, max_plausible=25),
                TagSpec("part_count", 10, "uint16", units="count"),
            ]),
        DeviceProfile(
            # THE WORD-SWAPPED ONE. Same tags, same data types, opposite word
            # order, because the integrator wired it that way in 2011 and the
            # drawing says nothing.
            name="OVEN-02", protocol="modbus", host="127.0.0.1", port=base_port + 1,
            unit_id=1, scan_ms=500,
            tags=[
                TagSpec("temperature_c", 0, "float32", units="degC", word_swap=True,
                        deadband=0.2, min_plausible=-20, max_plausible=400),
                TagSpec("flow_lpm", 2, "float32", units="L/min", word_swap=True,
                        deadband=0.5, min_plausible=0, max_plausible=400),
            ]),
        DeviceProfile(
            # THE FROZEN ONE. Answers every poll forever with the same values.
            name="PUMP-03", protocol="modbus", host="127.0.0.1", port=base_port + 2,
            unit_id=1, scan_ms=400, freeze_after_s=12.0,
            tags=[
                TagSpec("vibration_mms", 0, "float32", units="mm/s",
                        deadband=0.02, min_plausible=0, max_plausible=50),
                TagSpec("flow_lpm", 2, "float32", units="L/min",
                        deadband=0.5, min_plausible=0, max_plausible=400),
            ]),
        DeviceProfile(
            # THE 1998 MACHINE. ASCII over a TCP bridge, drops its session every
            # 15 s, emits a corrupt frame 8% of the time, and its clock is 90 s
            # slow.
            name="LATHE-04", protocol="ascii", host="127.0.0.1", port=base_port + 3,
            unit_id=4, scan_ms=600, drop_every_s=15.0, garbage_rate=0.08,
            clock_offset_s=-90.0,
            tags=[
                TagSpec("temperature_c", 0, "float32", units="degC", deadband=0.2),
                TagSpec("vibration_mms", 1, "float32", units="mm/s", deadband=0.02),
            ]),
    ]
