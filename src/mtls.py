"""Mutual TLS for the collector's uplink, and the refusals that prove it works.

README item 4 said "no TLS and no authentication ... every device and the
collector still run on 127.0.0.1 with nothing enforcing a boundary". Loopback is
not a boundary; it is the absence of one that happens to be hard to reach.

WHY MUTUAL AND NOT SERVER-ONLY. Ordinary TLS answers "is this the server I meant
to talk to". On a plant network the question that matters more is the other one:
"is this gateway one of ours". A collector that verifies the uplink and then
sends data to it has protected the data in flight and authenticated nobody --
anything that can reach the port can push readings in. Client certificates make
the gateway's identity a property of the connection, which is the only place it
cannot be forgotten.

WHAT A TEST OF TLS HAS TO DO. Showing that a valid client connects proves almost
nothing: a server with verification switched off passes that test. The evidence
is the REFUSALS, so `refusal_matrix()` runs six cases and the four that must fail
are the point:

    valid client, correct hostname        accept
    no client certificate                 refuse -- this is the one that catches
                                          a server built with CERT_NONE
    client signed by a different CA       refuse
    expired client certificate            refuse
    server hostname mismatch              refuse (client-side check)
    client that does not trust our CA     refuse (client-side check)

Certificates are generated in-process with `cryptography` and live in a temp
directory. No OpenSSL invocation, no checked-in key material, nothing to leak
and nothing to rotate.

WHAT THIS STILL IS NOT: there is no CA infrastructure here -- no revocation, no
OCSP, no renewal, no HSM, and the CA private key is in the same process as
everything it signs. IEC 62443 SL-2 wants a managed identity lifecycle and this
is a demonstration that the transport can carry and enforce identities, not that
anybody is managing them. `docs/SECURITY_62443.md` §5 is updated rather than
retired.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import socket
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SE-1 shopfloor"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_ca(cn: str = "SE-1 plant CA", days: int = 2) -> dict:
    """A short-lived in-process CA.

    P-256 rather than RSA: key generation is milliseconds instead of hundreds,
    which matters when the test suite mints a fresh hierarchy per run rather
    than shipping fixture keys. Shipping fixture keys is how a repository ends
    up with a private key in its history.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                           critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return {"key": key, "cert": cert, "cn": cn}


def issue(ca: dict, cn: str, *, server: bool = False, dns: list | None = None,
          not_before: dt.datetime | None = None,
          not_after: dt.datetime | None = None) -> dict:
    """Issue a leaf. `not_after` in the past produces an expired certificate,
    which is a case the refusal matrix needs and which no amount of correct
    configuration can produce on demand otherwise."""
    now = dt.datetime.now(dt.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    b = (x509.CertificateBuilder()
         .subject_name(_name(cn)).issuer_name(ca["cert"].subject)
         .public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(not_before or (now - dt.timedelta(minutes=5)))
         .not_valid_after(not_after or (now + dt.timedelta(days=1)))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                        critical=True)
         .add_extension(x509.ExtendedKeyUsage([
             x509.ExtendedKeyUsageOID.SERVER_AUTH if server
             else x509.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
    if dns:
        b = b.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(d) for d in dns]),
            critical=False)
    return {"key": key, "cert": b.sign(ca["key"], hashes.SHA256()), "cn": cn}


def write_pem(bundle: dict, directory, stem: str) -> dict:
    d = pathlib.Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    cert_p, key_p = d / f"{stem}.crt", d / f"{stem}.key"
    cert_p.write_bytes(bundle["cert"].public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(bundle["key"].private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return {"cert": str(cert_p), "key": str(key_p)}


def build_pki(directory, server_dns: str = "collector.plant.local") -> dict:
    """One CA, one server, three clients, and a rogue CA to test against."""
    ca = make_ca()
    rogue = make_ca("Somebody else's CA")
    now = dt.datetime.now(dt.timezone.utc)
    out = {
        "ca": write_pem(ca, directory, "ca"),
        "rogue_ca": write_pem(rogue, directory, "rogue-ca"),
        "server": write_pem(issue(ca, server_dns, server=True,
                                  dns=[server_dns, "localhost"]),
                            directory, "server"),
        "client": write_pem(issue(ca, "gateway-01"), directory, "client"),
        "expired": write_pem(
            issue(ca, "gateway-expired",
                  not_before=now - dt.timedelta(days=3),
                  not_after=now - dt.timedelta(days=1)),
            directory, "expired"),
        "rogue_client": write_pem(issue(rogue, "gateway-rogue"),
                                  directory, "rogue-client"),
        "server_dns": server_dns,
    }
    return out


def server_context(pki: dict, require_client_cert: bool = True) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # TLS 1.2 and below are refused outright rather than merely deprecated. A
    # minimum version is a setting somebody can lower under pressure; refusing
    # to build the context without it is not.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(pki["server"]["cert"], pki["server"]["key"])
    if require_client_cert:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(pki["ca"]["cert"])
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def client_context(pki: dict, client: str = "client",
                   trust: str = "ca", present_cert: bool = True) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(pki[trust]["cert"])
    if present_cert:
        ctx.load_cert_chain(pki[client]["cert"], pki[client]["key"])
    return ctx


class Uplink:
    """A TLS uplink that accepts framed readings and records who sent them.

    The peer's common name comes from the certificate, not from the payload.
    That is the whole point of client authentication: a gateway id in the
    message body is a claim, and a gateway id in the peer certificate is
    established before the first byte of body is read.
    """

    def __init__(self, pki: dict, require_client_cert: bool = True):
        self.pki = pki
        self.ctx = server_context(pki, require_client_cert)
        self.received: list = []
        self.rejected: list = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                raw, addr = self._sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(raw,),
                             daemon=True).start()

    def _handle(self, raw) -> None:
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as e:
            self.rejected.append(str(e))
            try:
                raw.close()
            except OSError:
                pass
            return
        try:
            peer = conn.getpeercert() or {}
            cn = None
            for rdn in peer.get("subject", ()):
                for k, v in rdn:
                    if k == "commonName":
                        cn = v
            conn.settimeout(2.0)
            data = conn.recv(65536)
            self.received.append({"peer_cn": cn, "bytes": len(data),
                                  "body": data.decode("utf-8", "replace")})
            conn.sendall(b"OK")
        except (ssl.SSLError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def send(pki: dict, port: int, body: bytes = b"temp_C=61.2", *,
         client: str = "client", trust: str = "ca", present_cert: bool = True,
         server_hostname: str | None = None, timeout: float = 3.0) -> dict:
    ctx = client_context(pki, client=client, trust=trust,
                         present_cert=present_cert)
    host = server_hostname or pki["server_dns"]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as conn:
                conn.sendall(body)
                reply = conn.recv(64)
                return {"ok": True, "reply": reply.decode(),
                        "tls_version": conn.version(),
                        "cipher": conn.cipher()[0] if conn.cipher() else None}
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "kind": "CertificateVerification", "error": str(e)}
    except ssl.SSLError as e:
        return {"ok": False, "kind": "SSL", "error": str(e)}
    except OSError as e:
        return {"ok": False, "kind": "Transport", "error": str(e)}


CASES = [
    ("valid gateway, correct hostname", {}, True),
    ("no client certificate", {"present_cert": False}, False),
    ("client signed by a different CA", {"client": "rogue_client"}, False),
    ("expired client certificate", {"client": "expired"}, False),
    ("wrong server hostname", {"server_hostname": "not-the-collector"}, False),
    ("client does not trust our CA", {"trust": "rogue_ca"}, False),
]


def refusal_matrix(directory) -> dict:
    """Every case, against one server. The refusals are the evidence."""
    pki = build_pki(directory)
    up = Uplink(pki)
    rows = []
    try:
        for label, kw, expect_ok in CASES:
            r = send(pki, up.port, f"from:{label}".encode(), **kw)
            rows.append({"case": label, "expected": "accept" if expect_ok else "refuse",
                         "accepted": bool(r.get("ok")),
                         "as_expected": bool(r.get("ok")) == expect_ok,
                         "kind": r.get("kind"),
                         "detail": (r.get("error") or "")[:130],
                         "tls_version": r.get("tls_version"),
                         "cipher": r.get("cipher")})
        accepted_cns = [x["peer_cn"] for x in up.received]
    finally:
        up.close()
    return {"rows": rows,
            "all_as_expected": all(r["as_expected"] for r in rows),
            "n_refused": sum(1 for r in rows if not r["accepted"]),
            "accepted_peer_cns": accepted_cns,
            "server_rejections": len(up.rejected)}


def unauthenticated_server_check(directory) -> dict:
    """The control: the same client cases against a server with CERT_NONE.

    Without this, "the valid client connected" is evidence of nothing -- a
    server that verifies nobody also accepts the valid client. This shows the
    identical test passing against a server that authenticates nothing, which is
    exactly why the refusals rather than the acceptance are the result.
    """
    pki = build_pki(directory)
    up = Uplink(pki, require_client_cert=False)
    try:
        no_cert = send(pki, up.port, b"anonymous", present_cert=False)
        rogue = send(pki, up.port, b"rogue", client="rogue_client")
    finally:
        up.close()
    return {"accepts_anonymous": bool(no_cert.get("ok")),
            "accepts_rogue": bool(rogue.get("ok")),
            "peer_cns": [x["peer_cn"] for x in up.received],
            "why": ("a CERT_NONE server accepts the valid client too, so a test "
                    "that only checks the happy path cannot tell the two "
                    "servers apart")}
