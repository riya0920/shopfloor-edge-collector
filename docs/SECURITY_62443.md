# Security posture — IEC 62443-aware design

**Scope statement first, because it is the honest part: this demonstrates
62443-*aware architecture*. It is not a certification, not an assessment, and not
a compliance claim.** No security level has been targeted or verified, no
threat model has been formally reviewed, and nothing here has been tested by
anyone qualified to test it. What follows is the design reasoning a collector
should be able to show a security architect, and the list of what is missing.

---

## 1. Zones and conduits

IEC 62443 organises a system into **zones** (groupings of assets with a common
security requirement) connected by **conduits** (the controlled communication
paths between them). The point of the model is that you defend the boundary, and
you can only defend a boundary you have drawn.

```
┌─────────────────────────────── DEVICE ZONE (OT) ───────────────────────────────┐
│                                                                                │
│   PRESS-01      OVEN-02       PUMP-03        LATHE-04                          │
│   Modbus TCP    Modbus TCP    Modbus TCP     ASCII over TCP bridge             │
│   :15020        :15021        :15022         :15023                            │
│                                                                                │
│   No authentication. No encryption. No integrity check beyond a XOR checksum   │
│   on one device. This is not a criticism of the devices -- it is what the      │
│   protocols ARE, and a design that assumes otherwise is designing for a plant  │
│   that does not exist.                                                         │
└────────────────────────────────────┬───────────────────────────────────────────┘
                                     │  CONDUIT 1: polling
                                     │  collector -> device, ALWAYS outbound
                                     │  no device ever initiates to the collector
┌────────────────────────────────────▼───────────────────────────────────────────┐
│                        COLLECTOR ZONE (edge / DMZ)                             │
│                                                                                │
│   GW-CELL-01                                                                   │
│   - durable WAL buffer (bounded)                                               │
│   - quality classification                                                     │
│   - NO INBOUND LISTENER                                                        │
└────────────────────────────────────┬───────────────────────────────────────────┘
                                     │  CONDUIT 2: uplink
                                     │  collector -> historian, ALWAYS outbound
                                     │  TLS + client cert (NOT IMPLEMENTED, §5)
┌────────────────────────────────────▼───────────────────────────────────────────┐
│                              IT ZONE (enterprise)                              │
│   historian, analytics, dashboards                                             │
└────────────────────────────────────────────────────────────────────────────────┘
```

## 2. The collector never accepts an inbound connection

This is the load-bearing architectural decision, and it is worth being precise
about *why* rather than treating it as a slogan.

**Attack surface.** A listening socket is code that processes bytes from whoever
can reach it, before authentication. Every listener is a parser, and parsers are
where remote code execution lives. A collector with no listener has no
pre-authentication attack surface from the IT side at all — not a small one,
none.

**Firewall unidirectionality.** A firewall rule permitting `edge → IT` on one port
is a fundamentally different object from one permitting `IT → edge`. The first
can be enforced with a stateful rule that a compromised IT host cannot use to
initiate anything; the second is a hole that exists whether or not anyone is using
it. Data diodes exist because some sites want this property in hardware.

**The Purdue model, stated plainly.** Purdue puts control (Level 1–2) below
manufacturing operations (Level 3) below enterprise (Level 4), with the strong
claim that *traffic should not originate from a higher level and terminate at a
lower one*. The reason is blunt: a compromised enterprise laptop is common, and a
PLC that stops is a physical event. Making all connections outbound-from-OT means
a compromise at Level 4 has no path to initiate anything at Level 1.

### The exception case, and how to serve it without breaking the invariant

Real deployments need remote configuration, firmware updates, and occasionally
"restart the collector". The naive answer is a management port, and that gives
the whole property back.

Two patterns keep the invariant:

1. **Pull-based configuration.** The collector polls a configuration endpoint on a
   schedule, fetches a signed bundle, verifies the signature, and applies it. The
   connection is outbound; the control flow is inbound. Latency is the polling
   interval, which is the price.
2. **Brokered command channel.** The collector holds an outbound long-lived
   connection to a broker (MQTT, or a gRPC stream) and receives commands over it.
   Still outbound-initiated, still no listener, and now low-latency. The cost is
   that the broker becomes a trusted component and must be treated as one.

Neither is implemented here. The collector is poll-only and has no configuration
channel at all, which is the *most* restrictive option and also the least useful.

## 3. A tampered register map is a process-safety issue

This is the framing that matters and the one that gets a collector taken seriously
by people who own the plant rather than the network.

The register map says "holding register 0–1 is a float32, big-endian, engineering
units °C, plausible range −20 to 200". If an attacker — or a careless engineer —
changes the scale factor, the historian fills with values that are wrong in a
*plausible* way. Downstream:

- an OEE number moves and nobody knows why
- an SPC chart re-baselines onto the wrong process centre
- a predictive-maintenance model retrains on the corrupted feed and its alarm
  threshold moves with it
- an operator who has learned to trust the display makes a decision on it

None of that is an IT incident. Nobody's data is exfiltrated and no system is
unavailable. It is a **process-safety and product-quality** issue that arrives
through an IT-shaped door, and it is why configuration integrity belongs in the
same conversation as the firewall.

The controls that follow from taking that seriously:

| control | status |
|---|---|
| configuration is signed, and signature verified before load | **not implemented** |
| configuration changes are recorded in an append-only audit log | **not implemented** |
| plausibility limits per tag, enforced at ingest | **implemented** — see the `UNCERTAIN` flag and the word-order probe in RESULTS.md |
| the collector refuses a config it cannot verify, rather than falling back | **not implemented** |

The plausibility limits are the one that is real, and they are worth their cost:
they are what turns a silently wrong value into a flagged one.

## 4. Secrets

Currently: **there are none, because there is no authentication anywhere.** The
Modbus devices have no concept of a credential, the ASCII device has no concept of
a credential, and the uplink is a local SQLite file.

For a real deployment, the rules that would apply:

- no secret in the config file, no secret in the image, no secret in an
  environment variable that a `ps` can read
- client certificate for the uplink, private key in a TPM or secure element where
  the hardware has one
- credential rotation without a site visit, which in practice means the pull-based
  config channel from §2 has to exist first

## 5. What is NOT implemented — the honest list

1. **No TLS anywhere.** The uplink writes to a local file. "We would use TLS" is
   the entire security section of most portfolio projects and it is not a design.
2. **No authentication or authorisation** on any path.
3. **No config signing, no config audit log, no config integrity check.** Named in
   §3 as the thing that matters most and not built.
4. **No network segmentation is actually enforced.** Every device and the
   collector run on `127.0.0.1` in one process tree. The zone diagram in §1
   describes a design, and this deployment does not implement it — the outbound-
   only property is a property of the *code* (there is no listener) and not of any
   firewall.
5. **No security event logging.** Failed connections are counted for operational
   reasons; nothing is logged for a SOC and there is no alerting path.
6. **No secure boot, no signed images, no supply-chain attestation.**
7. **No target security level.** 62443-3-3 defines SL 1–4 against seven foundational
   requirements. No SL has been targeted, so no SL has been met.
8. **No threat model document, and no review by anyone qualified.**

## 6. What this project does demonstrate

Not nothing, but a short list, and stating it precisely is the point:

- an architecture where the collector **initiates every connection** and runs no
  inbound listener, with the reasoning above rather than as an accident
- **quality flags including `UNCERTAIN`** propagated to the historian, so a value
  that is implausible is marked rather than silently trusted — which is a security
  control as much as a data-quality one
- **per-tag plausibility limits** as first-class configuration, measured in
  RESULTS.md against a deliberately wrong word-order config
- a **bounded buffer with an explicit, counted overflow policy**, so resource
  exhaustion is a decision rather than a crash
- **framing and checksum validation** on the ASCII protocol, with corrupt frames
  rejected rather than parsed optimistically

That list is short on purpose. A security section that claims more than it built
is worse than one that claims nothing, because the first thing a security
architect does with a document like this is check one claim.
