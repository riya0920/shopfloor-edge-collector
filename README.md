# SE-1 — Shop-Floor Data Collection System (edge, buffered, secure)

**Status: ~20% slice.** Real Modbus TCP and a framed-ASCII protocol, a
config-driven collector with quality flags, a durable store-and-forward buffer
with a bounded overflow policy, and a chaos soak that verifies zero loss and zero
duplication. TLS, config signing, containerised resource limits, and the ops
dashboard are not built.

```bash
python run_soak.py                 # 180 s soak
python run_soak.py --seconds 600
python run_soak.py --report-only
```

Writes [docs/RESULTS.md](docs/RESULTS.md). The security posture is
[docs/SECURITY_62443.md](docs/SECURITY_62443.md), which opens by saying what it is
*not*.

## The fleet — heterogeneity on purpose, because plants are museums

| device | protocol | what it is there to break |
|---|---|---|
| `PRESS-01` | Modbus TCP (`pymodbus`) | the normal case |
| `OVEN-02` | Modbus TCP | **word-swapped float32** — because one device always is |
| `PUMP-03` | Modbus TCP | **freezes after 12 s** and keeps answering perfectly |
| `LATHE-04` | framed ASCII over a TCP bridge | the 1998 machine: drops its session every 15 s, corrupts 8% of frames, clock 90 s slow |

## The ledger

180-second soak, two uplink outages, one collector process kill:

| | |
|---|---|
| samples captured to the durable buffer | 2,790 |
| dropped by the bounded-buffer policy | 227 |
| **expected in the historian** | **2,563** |
| **actually in the historian** | **2,563** |
| **loss** | **0** |
| **duplicates created by the deliberate replay drill** | **0** |
| peak buffer depth (bound 600) | 600 |

**The denominator is the claim.** It is the collector's own capture ledger minus
what the bounded-buffer policy *deliberately* dropped — not the number of samples
the devices produced. A collector that was disconnected during a device update
never had that sample to lose, and counting it would inflate the result in the
flattering direction. What this proves is that **nothing captured is lost between
the buffer and the historian**; it does not prove the collector saw everything the
devices did, and that distinction is the difference between a measured claim and a
slogan.

Uniqueness lives at the **destination**: `PRIMARY KEY (collector_id, seq)` with
`INSERT OR IGNORE`. Exactly-once *delivery* does not exist — a crash between
"sent" and "marked sent" is always possible — so the uplink is at-least-once and
the sink is idempotent. The replay drill re-sends the entire buffer on purpose and
inserts nothing.

## The frozen device

`PUMP-03` freezes 12 seconds in and then answers every poll, on time, forever,
with entirely plausible values. No connection drops, no protocol error, nothing
reports a fault — and it accumulates **652 STALE samples against 62 GOOD**.

Neither half of the test works alone:

- **change-only** flags a tank sitting at setpoint, which is the healthiest signal
  on the plant
- **heartbeat-only** sees a frozen device answering perfectly and calls it healthy

It is the combination — *we are being answered, and the answer has not moved for N
scans* — that catches this, which is why the stale window is a per-tag config value
and not a constant: a setpoint that legitimately never moves needs a far longer
window than a vibration reading.

STALE is propagated downstream, never dropped. "68.2" and "68.2, three hours
stale" are different facts and a float cannot carry the difference.

## The word-swapped float, measured rather than asserted

Same two registers from `OVEN-02`, decoded both ways:

| register map says | decoded value | quality flag |
|---|---|---|
| correct (`word_swap=True`) | **68.655 °C** | GOOD |
| wrong (`word_swap=False`) | **4.40435e+09 °C** | **UNCERTAIN** |

Modbus has no float type. It has 16-bit registers, and a float is a convention
both ends must agree on — the bytes are valid either way, so **there is no way to
detect this from the wire**. It is a site-survey fact, which is why `word_swap` is
a field in the register map rather than a heuristic.

The wrong config does not raise an error, drop a connection, or fail a checksum.
It produces a *number*. The only thing between it and a historian full of
confident nonsense is the per-tag `min_plausible` / `max_plausible` range check at
ingest — which is what turns a week of confused analytics into an ingest-time flag.

## The 1998 machine

`LATHE-04`: **11 reconnects, 27 frames rejected on checksum** over 180 seconds.

- **Framing on STX/ETX, not on newlines.** A corrupted byte can *be* a newline, and
  a parser splitting on `\n` cuts a frame in half and then parses both halves as
  data.
- **A frame that fails its XOR checksum is rejected outright.** A frame that fails
  its checksum is not "mostly right".
- **Exponential backoff with jitter.** Without jitter, a plant that loses a switch
  brings every gateway back at the same instant and the reconnect storm looks like
  the outage continuing.

## The bounded buffer

Bound 600 rows, peak depth 600, **227 dropped** by the `priority` policy during the
long outage — and the drops land where the policy says they should:

| tag | priority | captured | dropped |
|---|---|---|---|
| `flow_lpm` | 4 | 645 | **189** |
| `pressure_bar` | 5 | 350 | 11 |
| `temperature_c` | 5 | 863 | 27 |
| `vibration_mms` | 6 | 582 | **0** |
| `part_count` | 10 | 350 | **0** |

That table is also the bug report. The first version sorted by `-priority` and
dropped the *most* important tags first: `part_count` lost 118 rows while
`flow_lpm` lost none. Nothing failed — the buffer stayed bounded and the
zero-loss proof still passed. The only thing that caught it was adding the
per-tag counters and looking.

An unbounded buffer is a lie about the hardware. A gateway with 256 MB of RAM does
not hold a three-day outage, and pretending it does means the failure arrives as an
OOM at 03:00 rather than as a decision somebody made deliberately.

The priority policy is the default because it is the only one that can answer
*which data would you rather lose*: a 1 Hz temperature trend is not worth the same
as the part counter feeding OEE, and oldest-first or newest-first choose between
them by accident. RESULTS.md §4 shows the per-tag drop counts, so the policy is a
measurement rather than a claim about code.

Whatever is dropped is **counted and surfaced**. A silent drop is the same bug as a
silent loss.

## Adding a device is config, not code

```python
DeviceProfile(
    name="GRINDER-05", protocol="modbus", host="10.4.2.71", port=502,
    unit_id=3, scan_ms=500,
    tags=[
        TagSpec("spindle_load_pct", 40, "float32", units="%",
                word_swap=True, deadband=1.0,
                min_plausible=0, max_plausible=150),
    ])
```

Nine lines. The collector, the buffer, the quality classifier and the uplink are
untouched.

## A bug worth keeping

The first Modbus implementation used `ModbusSequentialDataBlock` +
`ModbusSlaveContext`, the API every pymodbus tutorial shows. In pymodbus 3.15 those
still import — under new names (`ModbusDeviceContext`, `devices=`) — but the
context **deep-copies the data block at construction**, so mutating the block
afterwards updates nothing and `async_setValues` returns exception code 6.

The symptom: a server that serves its initial values forever. In this project that
looks *exactly like* the frozen device the collector is supposed to detect — the
test fixture had accidentally become the thing under test. The fix is the modern
`SimData` / `SimDevice` API, where `server.async_setValues(...)` is the runtime
write path.

## What is NOT built (the other 80%)

1. **The soak is 180 seconds, not 24 hours.** The spec asks for a 24-hour chaos
   soak and this is not one. The failure modes exercised are the same; the ones a
   long soak finds — buffer file growth, WAL checkpoint behaviour, memory creep,
   clock changes, log rotation — are exactly the ones this does not.
2. **No resource limits.** The spec asks for the collector to run in a constrained
   container (256 MB RAM, 0.5 CPU) with the numbers measured inside that envelope.
   It runs unconstrained on a desktop. **There is no tags/sec figure within a
   stated resource envelope**, which is one of the spec's headline metrics, and
   quoting a throughput number measured without the constraint would be
   meaningless.
3. **No OPC-UA device here.** The spec asks for OPC-UA subscriptions alongside
   Modbus. That is built in DATA-1 of this portfolio — including the
   monitored-item queue-size trap — and is deliberately not duplicated. Within
   *this* project, OPC-UA is missing.
4. **No TLS, no authentication, no config signing.** See SECURITY_62443.md §5 for
   the full honest list. The zones-and-conduits design is documented and the
   outbound-only property is real (there is no listener), but no firewall enforces
   anything: every device and the collector run on `127.0.0.1`.
5. **No ops dashboard and no runbooks.** Per-device health, buffer depth, uplink
   lag and quality-flag rates are all computed and land in `results.json`. Nothing
   displays them and the three standard incident runbooks are not written.
6. **No clock-skew monitoring here.** `LATHE-04` is configured 90 s slow and the
   collector records both `device_ts` and `collector_ts`, but nothing analyses the
   difference. That analysis is in DATA-1.
7. **No store-and-forward across a real restart.** The "crash" closes and reopens
   the SQLite handle in-process; it does not kill and restart the OS process, so
   WAL recovery is exercised only partially.
8. **Deadband is configured but not used to suppress transmission.** Every poll is
   recorded regardless of whether the value moved by more than the deadband, so
   the bandwidth argument for deadbands is made in the docstrings and not measured.

## Layout

```
src/devices.py    device simulators: Modbus register maps, the ASCII frame format,
                  the frozen device, the process model behind the registers
src/drivers.py    protocol drivers behind one interface; backoff with jitter
src/collector.py  config-driven acquisition, quality classification, WAL buffer,
                  bounded overflow policy, at-least-once uplink, idempotent historian
run_soak.py       the chaos soak; writes docs/RESULTS.md
docs/SECURITY_62443.md   zones and conduits, and what is not implemented
```
