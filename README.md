# SE-1 — Shop-Floor Data Collection System (edge, buffered, secure)

**Status: complete.** Real Modbus TCP and a framed-ASCII protocol, a
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

## Built in the second pass — see [docs/EXTENSIONS.md](docs/EXTENSIONS.md)

`python extend.py` — three gaps this README previously named:

- **Throughput within a stated resource envelope**, which is one of the spec's
  headline metrics and was absent. 16.8 → 54.0 tags/sec as the scan rate tightens,
  with peak RSS flat at ~69 MB and growth under 1 MB across the run — the
  measurement that checks the bounded buffer actually bounds something.
- **The durability/throughput tradeoff, measured — and it refuted my own
  hypothesis.** I assumed fsync-per-sample was the bottleneck. Relaxing durability
  all the way to `synchronous=OFF` buys only **~1.07×**, so the bottleneck is the
  sequential scan loop instead. **Do not trade away a correctness property to fix a
  performance problem you have not profiled.**
- **Signed config updates.** A tampered register map is a process-safety issue: a
  scale factor changed from 1.0 to 0.1 makes a furnace at 720 °C report 72 °C to
  every consumer, confidently, with nothing erroring. Legitimate updates accepted,
  tampered ones rejected, and **replay of an older correctly-signed config
  rejected** — the rollback case a signature check alone accepts.
- **[docs/RUNBOOKS.md](docs/RUNBOOKS.md)** for the three standard incidents, each
  leading with the check that discriminates and each with a *what NOT to do*.

## Completed in the third pass — see [docs/COMPLETION.md](docs/COMPLETION.md)

```bash
python complete.py    # ~2 min; writes COMPLETION.md and out/ops.html
```

- **A throughput number inside a stated envelope**, which is the spec's headline
  metric and the thing the README said was missing:
  **395 tags/s on one pinned thread, peak
  219 MB against a 256 MB target**,
  growth +2.65 MB/min. It is **checked, not
  enforced** — there is no container runtime and no cgroups here — and the
  distinction is stated rather than glossed. A checked envelope still catches
  unbounded growth, which is the failure that actually kills gateways.
- **Deadband suppression, measured.** At a deadband of 0.5,
  **76% of messages suppressed for
  4.1% of the signal's SD** in RMS error, with a
  7-unit step still detected **immediately**. Scored with a
  zero-order hold, because that is what a historian does with
  report-by-exception data; interpolating would flatter the deadband by
  inventing a smoothness no consumer applies.
- **Clock skew, split into offset and drift.** LATHE-04's configured
  90 s is a *constant* offset — annoying and
  correctable. An injected drifting clock is caught separately at
  **+14 s/hour** if present. That separation is the
  whole value: a drifting clock runs at a different *rate*, so a correction is
  stale the moment it is applied, and a monitor that only computes a mean cannot
  tell the two apart. Either way the rule is **record the offset, never rewrite
  the stored timestamps.**
- **Store-and-forward across a real process restart.**
  **500 rows buffered, 500
  recovered by a new process, zero loss: True.** The earlier pass's
  "crash" closed and reopened the SQLite handle *in process*, which exercises
  reopening a file rather than WAL recovery. This kills a child process with
  `os._exit`, skipping atexit, destructors and any buffered write.
- **An OPC-UA leg**, so the collector is not Modbus-and-ASCII only — and it found
  the impedance mismatch worth finding. See below.
- **An ops page** at `out/ops.html`, self-contained, with per-device health, the
  envelope, deadband suppression and three runbooks. Each runbook's most
  important line is its **do-not**, because that is the one an engineer needs at
  03:00 and the one that never gets written down.

### The OPC-UA leg found the mismatch, which is why it was worth adding

4 notifications from 4 writes — but that is one
initial value plus three changes. **One write repeated the previous value and
produced nothing at all**, because OPC-UA pushes on *change* and Modbus and the
ASCII device are *polled*.

So a `Driver` interface built around polling has to hide a subscription behind a
poll, and a collector that reads "no data" as "device down" will declare a
healthy machine dead the moment its temperature stops moving — which on an idle
machine is most of the weekend. The fix is a keep-alive and a way to distinguish
*no change* from *no answer*. **The interface here does not have one**, and that
is the honest state of it.

## Built in the fourth pass — see [docs/SUBSCRIPTIONS_AND_TLS.md](docs/SUBSCRIPTIONS_AND_TLS.md)

```bash
python run_pass4.py    # ~5 s
```

The two items the list below named, both of which were named with the right
diagnosis and neither of which had been acted on.

- **A driver contract that models subscriptions.** One fetch now answers two
  questions the poll shape could only answer together: *what the device said*
  (readings, each with its own quality) and *whether the device is there*
  (liveness, derived from **contact** and never from whether a value moved). The
  subscription carries a keep-alive — an empty publish when there is no news —
  because silence past a deadline is the only evidence of death either transport
  can offer.
- **And the measurement is the point.** A healthy device that simply has nothing
  to say, sampled 44 times: the old contract calls it DOWN on
  **43 of 44 ticks**
  (98%), the new one on
  **0**. Kill it for real and the new contract
  notices in 1.58 s against its own 2.0 s
  deadline.
- **Liveness is three-valued because the useful state is the middle one.**
  `FRESH` / `KEEPALIVE` / `SILENT`. Collapsing `KEEPALIVE` into `FRESH` throws
  away how old the newest value is; collapsing it into `SILENT` is the original
  bug. `PolledAdapter` wraps every existing driver unchanged and **never**
  reports `KEEPALIVE`, because a polled transport gives no liveness signal and
  inventing one would be the same lie in the other direction.
- **Mutual TLS on the uplink.** Loopback is not a boundary, it is the absence of
  one that happens to be hard to reach. Ordinary TLS answers *is this the server
  I meant to talk to*; on a plant network the question that matters more is *is
  this gateway one of ours*. One CA, a server certificate and three client
  certificates are minted in-process per run — no checked-in key material, which
  a test asserts, because a repository with a private key in its history has one
  forever.
- **The refusals are the result, not the acceptance.** Six cases: a valid
  gateway connects and is identified as `gateway-01` **from its
  certificate rather than its payload**; no client certificate, a client signed
  by a different CA, an expired certificate, a hostname mismatch and a client
  that does not trust our CA are all refused. TLS 1.3, with 1.2 and below
  refused rather than deprecated.

### The control that makes the matrix mean anything

A server with verification switched off passes the happy-path test too. Run the
same clients against a `CERT_NONE` server and it accepts a client with **no
certificate** (`True`) and one signed by a **different CA**
(`True`), with peer common names `[None, None]`. Both
connections succeed and neither has an identity — which is exactly what a test
that only checks the happy path cannot distinguish from the real thing.

## Also in the fifth pass — the contract wired into an acquisition loop

Pass 4 built the driver contract and measured it in isolation; the not-built list
recorded that it was not wired in, and that doing so *would change the health path
and the buffer's back-pressure logic*. It is wired now, and the change lands in
exactly two places:

- **`Collector.classify` learned about liveness.** STALE means *we are being
  answered and the answer has not moved*. For a device that publishes on change,
  not moving is the normal state — so a confirmed keep-alive suppresses STALE.
  Without that the classifier calls a healthy idle subscribed device STALE for
  the same reason the old contract called it DOWN: it reads "no change" as "no
  answer".
- **`Collector.observe_liveness` / `device_health`** track whether a device is
  *there*, separately from what it last *said*. Only SILENT is DOWN.

`subscribe.run_device` is `run_soak.poll_device` with three changes and no
others: it calls `fetch()`, it reports liveness every tick whether or not a
reading arrived, and it marks tags BAD on SILENT rather than on "no data this
tick". The old loop had that last one as "connection failed", which is the same
thing for a polled device and a different thing entirely for a subscribed one.

Same idle-but-healthy device, 40 ticks, both loops:

| | old loop | new loop |
|---|---:|---:|
| rows written to the historian | 40 | **1** |
| of those, BAD | 39 | **0** |
| device health | **DOWN** | **UP** |

**98% of the old loop's ticks write a BAD row for
a device that is working.** And the row count is the second finding: the old loop
writes one row per tag per tick whatever happens, the new one writes only when
something changed — 40 rows against 1 for the
same period. Report-by-exception only reports by exception if the loop can tell
the difference between nothing-to-say and nothing-there.

## What is NOT built

1. **The soak is a minute, not 24 hours.** It is rate-measured inside a resource
   envelope, so the memory figure means something. The failure modes a real soak
   finds — WAL growth over days, log rotation, a daylight-saving change — need
   hours and are not here.
2. **The envelope is checked, not enforced.** No container runtime, no cgroups on
   this platform. The process is pinned to one thread and its RSS is observed;
   nothing stops it exceeding the target, so this does not prove the collector
   survives being squeezed. An allocator behaves differently under real pressure.
3. **No real devices.** Modbus, ASCII and OPC-UA endpoints are all simulated in
   process, and the change-driven device in `subscribe.py` is a stand-in that is
   faithful about exactly one behaviour: writing the same value publishes
   nothing. A real server has session timeouts, republish requests and
   revised-publishing-interval negotiation, and none of that is modelled.
4. **`run_soak.py` still uses the old poll loop.** The contract is wired into
   `Collector` and `subscribe.run_device` runs on it, but the chaos soak — which
   is where the 0-loss/0-dup and throughput numbers come from — has not been
   migrated. Those numbers therefore describe the polled path, and re-running
   them through the new loop is the remaining work.
5. **There is no CA infrastructure.** No revocation, no OCSP, no renewal, no
   HSM, and the CA private key lives in the same process as everything it signs.
   IEC 62443 SL-2 wants a managed identity lifecycle; this shows the transport
   can carry and enforce identities, not that anybody is managing them.
6. **The devices themselves are still unauthenticated.** mTLS protects the
   collector's uplink. Modbus and the ASCII protocol have no security model at
   all — that is a property of those protocols, and the real-world answer is
   network segmentation rather than anything a driver can do.

## Layout

```
src/devices.py    device simulators: Modbus register maps, the ASCII frame format,
                  the frozen device, the process model behind the registers
src/drivers.py    protocol drivers behind one interface; backoff with jitter
src/collector.py  config-driven acquisition, quality classification, WAL buffer,
                  bounded overflow policy, at-least-once uplink, idempotent historian
src/subscribe.py  push-capable driver contract: readings and liveness, separately
src/mtls.py       in-process PKI, mutual-TLS uplink, and the refusal matrix
run_soak.py       the chaos soak; writes docs/RESULTS.md
run_pass4.py      the subscription contract and mTLS; writes the pass-4 doc
docs/SECURITY_62443.md   zones and conduits, and what is not implemented
```
