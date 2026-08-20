# Runbooks — the three incidents this collector actually has

A runbook is not documentation. It is what somebody follows at 03:00 with a
production line down, so each of these leads with the CHECK THAT DISCRIMINATES
rather than with background.

---

## 1. A device has gone silent

**Symptom:** quality flags for one device are `BAD`, or its sample count stopped
advancing while its neighbours kept going.

**The check that discriminates:** is it the DEVICE or the NETWORK?

```
1. Does any other device on the same switch/subnet still report?
yes -> device-side problem, go to 3
no  -> network segment, go to 2
2. Ping the device IP from the gateway.
responds -> TCP/application layer: check the device's port is listening,
and check whether the PLC is in program mode (a PLC in
program mode answers ping and refuses Modbus)
silent   -> physical/switch. Escalate to network. STOP -- do not restart
the collector; it is not the collector.
3. Check the collector's per-device state: is it in backoff?
Backoff with growing intervals is CORRECT behaviour against a dead device,
not a fault. Do not restart the collector to "clear" it.
```

**What NOT to do:** restart the collector. The buffer is durable and the collector
is not the failure; restarting loses nothing but proves nothing and costs the
reconnect backoff. The one thing a restart *does* fix is a wedged socket, and
that shows as a device that is silent while ping and port checks both succeed.

**Data impact:** none for the duration of the buffer, provided the uplink is up.
Samples for other devices continue. The silent device leaves a genuine gap, and
the gap is real data — do not backfill it with interpolation.

---

## 2. The uplink is down

**Symptom:** buffer depth climbing monotonically; historian row count static.

**Check:**

```
1. Buffer depth vs the bound. Depth / max_buffer_rows is the clock you are
racing. At the current capture rate, time to overflow = (bound - depth) /
capture_rate. Compute it before doing anything else -- it decides whether
this is an hour's problem or a minute's.
2. Is the historian reachable from the gateway? (Not from your laptop. The
gateway's route is the one that matters, and it is frequently different.)
3. If the outage will exceed the buffer, the overflow policy decides what
survives. Verify which policy is configured BEFORE overflow, not after --
`priority` drops lowest-priority tags first, and if the priorities were never
set, everything is equal and it degrades to oldest-first.
```

**What NOT to do:** raise the buffer bound to "buy time" on a running gateway.
The bound is sized to the hardware; raising it converts a controlled data loss
into an OOM kill, which loses the buffer entirely — including everything already
captured.

**Data impact:** none until overflow. After overflow, exactly the rows the policy
chose to drop, and `stats.dropped_overflow` counts them. That count belongs in the
incident report; silent loss is what makes an outage unauditable.

---

## 3. A device is frozen (answering, but not changing)

**Symptom:** quality flags going `STALE` for one device while it stays connected.
No errors anywhere.

**This is the dangerous one**, because every other layer reports health. The
device answers every poll, on time, with values in range.

**Check:**

```
1. Confirm STALE is real and not a legitimately constant tag: does the process
have any reason to be steady right now (setpoint hold, machine stopped)?
Check a tag on the SAME device that should be moving.
2. If several tags on one device are all frozen at once, the device is frozen.
A single frozen tag is a sensor; all tags frozen is the controller.
3. Power-cycle is usually the fix for a frozen scanner card, and it MUST be
coordinated with operations -- the machine may be mid-cycle.
```

**Data impact — the part that matters:** every `STALE` sample is retained and
flagged, not discarded. Downstream analytics must filter on quality. If a
consumer ignores the quality flag, the frozen period looks like a period of
exceptional process stability, and that is how a frozen sensor becomes a
"process improvement" in somebody's quarterly review.

**After recovery:** the gap between the last GOOD sample and recovery is not
missing data — it is data known to be wrong. Mark the interval, do not backfill.
