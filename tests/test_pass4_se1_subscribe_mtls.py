"""Pass 4: a push-capable driver contract, and mutual TLS.

The two items the README named. Both were named with the right diagnosis, which
is why the tests here are about the diagnosis rather than about the code: does
the new contract actually stop calling an idle machine dead, and does the TLS
setup actually refuse anybody.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import ssl
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import mtls                       # noqa: E402
import subscribe as S             # noqa: E402


# ---------------------------------------------------------------------------
# the subscription contract
# ---------------------------------------------------------------------------

def test_a_repeated_write_publishes_nothing():
    """The behaviour that broke the poll-shaped contract, pinned so a future
    change to the simulator cannot quietly remove the problem being solved."""
    dev = S.ChangeDrivenDevice({"t": 60.0})
    assert dev.write("t", 61.0) is True
    assert dev.write("t", 61.0) is False


def test_the_old_contract_calls_an_idle_healthy_device_dead():
    r = asyncio.run(S.write_repeat_experiment())
    assert r["repeat_wrote_nothing"] is True
    assert r["old_contract_called_it_down"] == 1
    assert r["new_contract_called_it_down"] == 0


def test_liveness_is_three_valued_and_keepalive_is_the_useful_one():
    dev = S.ChangeDrivenDevice({"t": 60.0}, keepalive_s=0.5)
    drv = S.ChangeDrivenDriver(dev, keepalive_s=0.5, missed_allowed=3)

    async def go():
        await drv.subscribe()
        first = await drv.fetch(now=0.0)          # the initial publish
        quiet = await drv.fetch(now=0.1)          # inside the deadline, no news
        alive = await drv.fetch(now=0.6)          # a keep-alive publish is due
        return first, quiet, alive

    first, quiet, alive = asyncio.run(go())
    assert first.liveness == S.FRESH and first.readings
    assert quiet.liveness == S.KEEPALIVE and not quiet.readings
    assert alive.liveness == S.KEEPALIVE
    assert all(f.healthy for f in (first, quiet, alive))


def test_silence_past_the_deadline_is_the_only_thing_that_means_down():
    dev = S.ChangeDrivenDevice({"t": 60.0}, keepalive_s=0.5)
    drv = S.ChangeDrivenDriver(dev, keepalive_s=0.5, missed_allowed=3)

    async def go():
        await drv.subscribe()
        await drv.fetch(now=0.0)
        dev.alive = False
        inside = await drv.fetch(now=drv.deadline_s - 0.01)
        outside = await drv.fetch(now=drv.deadline_s + 0.01)
        return inside, outside

    inside, outside = asyncio.run(go())
    assert inside.liveness == S.KEEPALIVE, "declared dead inside its own deadline"
    assert outside.liveness == S.SILENT


def test_the_deadline_is_not_the_keepalive_interval():
    """Setting them equal declares a device dead on the first late packet. A
    deadline is a statement about tolerance for jitter."""
    drv = S.ChangeDrivenDriver(S.ChangeDrivenDevice({"t": 1.0}),
                               keepalive_s=1.0, missed_allowed=3)
    assert drv.deadline_s == pytest.approx(4.0)
    assert drv.deadline_s > drv.keepalive_s


def test_an_idle_device_is_healthy_throughout_and_a_dead_one_is_caught():
    r = asyncio.run(S.compare_contracts(steady_s=1.2, tick_s=0.05,
                                        keepalive_s=0.3, missed_allowed=3))
    assert r["new_contract_false_down"] == 0
    assert r["old_contract_false_down_frac"] > 0.8
    assert r["detection_within_deadline"] is True
    assert r["keepalives_seen"] > 0


def test_the_polled_adapter_never_manufactures_a_keepalive():
    """A polled transport gives no liveness signal of its own, and the adapter
    must not invent one -- reporting KEEPALIVE for a polled device would be the
    same lie in the other direction."""

    class _Fake:
        profile = type("P", (), {"name": "fake"})()

        def __init__(self):
            self.answers = [{"t": 1.0}, None, {"t": 2.0}]

        async def connect(self):
            return True

        async def poll(self):
            return self.answers.pop(0)

        async def close(self):
            pass

    ad = S.PolledAdapter(_Fake())

    async def go():
        await ad.subscribe()
        return [(await ad.fetch()).liveness for _ in range(3)]

    assert asyncio.run(go()) == [S.FRESH, S.SILENT, S.FRESH]


def test_a_fetch_carries_quality_per_reading_not_per_fetch():
    dev = S.ChangeDrivenDevice({"a": 1.0, "b": 2.0})
    drv = S.ChangeDrivenDriver(dev)

    async def go():
        await drv.subscribe()
        return await drv.fetch(now=0.0)

    f = asyncio.run(go())
    assert {r.tag for r in f.readings} == {"a", "b"}
    assert all(r.quality == S.GOOD for r in f.readings)
    assert f.values() == {"a": 1.0, "b": 2.0}


# ---------------------------------------------------------------------------
# mutual TLS
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return mtls.build_pki(tmp_path_factory.mktemp("pki"))


def test_the_ca_is_a_ca_and_the_leaves_are_not(pki):
    from cryptography import x509
    ca = x509.load_pem_x509_certificate(
        pathlib.Path(pki["ca"]["cert"]).read_bytes())
    leaf = x509.load_pem_x509_certificate(
        pathlib.Path(pki["client"]["cert"]).read_bytes())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not leaf.extensions.get_extension_for_class(
        x509.BasicConstraints).value.ca


def test_the_expired_certificate_really_is_expired(pki):
    from cryptography import x509
    c = x509.load_pem_x509_certificate(
        pathlib.Path(pki["expired"]["cert"]).read_bytes())
    assert c.not_valid_after_utc < dt.datetime.now(dt.timezone.utc)


def test_a_valid_gateway_connects_and_is_identified_by_its_certificate(pki):
    up = mtls.Uplink(pki)
    try:
        r = mtls.send(pki, up.port, b"temp_C=61.2")
        assert r["ok"] is True
        assert r["tls_version"] == "TLSv1.3"
        assert up.received and up.received[0]["peer_cn"] == "gateway-01"
        assert up.received[0]["body"] == "temp_C=61.2"
    finally:
        up.close()


@pytest.mark.parametrize("label,kw", [
    ("no client certificate", {"present_cert": False}),
    ("client signed by a different CA", {"client": "rogue_client"}),
    ("expired client certificate", {"client": "expired"}),
    ("wrong server hostname", {"server_hostname": "not-the-collector"}),
    ("client does not trust our CA", {"trust": "rogue_ca"}),
])
def test_every_invalid_case_is_refused(pki, label, kw):
    up = mtls.Uplink(pki)
    try:
        r = mtls.send(pki, up.port, b"x", **kw)
        assert r["ok"] is False, label
    finally:
        up.close()


def test_the_full_matrix_behaves(tmp_path):
    m = mtls.refusal_matrix(tmp_path)
    assert m["all_as_expected"] is True
    assert m["n_refused"] == 5
    assert m["accepted_peer_cns"] == ["gateway-01"]


def test_a_server_that_verifies_nobody_accepts_everybody(tmp_path):
    """The control that makes the matrix mean something. A CERT_NONE server
    passes the happy-path test too, so acceptance is not evidence -- only the
    refusals are."""
    c = mtls.unauthenticated_server_check(tmp_path)
    assert c["accepts_anonymous"] is True
    assert c["accepts_rogue"] is True
    assert c["peer_cns"] == [None, None]


def test_tls_1_2_is_refused_not_merely_deprecated(pki):
    """A minimum version is a setting somebody can lower under pressure. This
    asserts the contexts are actually built with 1.3 as the floor."""
    s = mtls.server_context(pki)
    c = mtls.client_context(pki)
    assert s.minimum_version == ssl.TLSVersion.TLSv1_3
    assert c.minimum_version == ssl.TLSVersion.TLSv1_3
    assert c.check_hostname is True and c.verify_mode == ssl.CERT_REQUIRED
    assert s.verify_mode == ssl.CERT_REQUIRED


def test_no_key_material_is_checked_in():
    """The reason the PKI is generated per run. A repository with a private key
    in its history has one forever."""
    root = pathlib.Path(__file__).resolve().parents[1]
    tracked = [p for p in root.rglob("*")
               if p.suffix in (".key", ".pem", ".pfx", ".p12")
               and ".git" not in p.parts and "out" not in p.parts]
    assert tracked == [], tracked
