"""TLS peer inspection — detect interception rather than silently accepting it.

Why this exists
---------------
The obvious fix for `CERTIFICATE_VERIFY_FAILED` is to turn verification off, or
to install whatever CA the network hands you. Both are wrong on a pipeline that
carries a Vantage tool key and trading signals: the first accepts a MITM
silently, the second legitimises it for every host on the device.

Observed on this network (2026-09-21): `fomopulse.app` and
`omokoda.duckdns.org` were both re-signed by

    CN=FGT70FTK23012743, O=Fortinet, L=Sunnyvale, ST=California

— a FortiGate appliance performing SSL deep inspection — while github.com,
cloudflare.com, google.com, 1.1.1.1 and pypi.org presented their real issuers.

So the engine does three things:
  1. connects with verification ON and reports the failure plainly;
  2. inspects the peer chain *without trusting it* to say WHO interrupted;
  3. refuses to proceed and never falls back to an unverified connection.

Local storage is written before any network push, so a refusal loses no data —
it only refuses to transmit over a link it cannot trust.
"""

from __future__ import annotations

import datetime
import socket
import ssl
from dataclasses import dataclass, field

# Issuers known to be network-interception appliances rather than public CAs.
# A match here is not proof of malice — plenty of organisations inspect their own
# traffic — but it always means the connection is not end-to-end, and on this
# device the appliance is not one we control.
INTERCEPTION_MARKERS = (
    "fortinet",
    "fortigate",
    "fgt",
    "palo alto",
    "paloalto",
    "checkpoint",
    "check point",
    "sophos",
    "zscaler",
    "blue coat",
    "bluecoat",
    "netskope",
    "cisco umbrella",
    "sonicwall",
    "watchguard",
    "barracuda",
    "squid",
    "mitmproxy",
)

PUBLIC_CA_HINTS = (
    "let's encrypt", "lets encrypt", "isrg",
    "digicert", "globalSign", "sectigo", "comodo",
    "google trust services", "amazon", "entrust",
    "godaddy", "starfield", "thawte", "verisign",
    "cloudflare", "ssl.com", "actalis", "buypass",
    "certum", "d-ttrust", "geotrust", "rapidssl",
    "microsoft", "apple", "usertrust", "certainly",
    "harica", "telia", "swissSign", "izenpe",
)


@dataclass
class PeerCert:
    host: str
    peer_ip: str = ""
    tls_version: str = ""
    subject: str = ""
    issuer: str = ""
    not_before: str = ""
    not_after: str = ""
    sans: list[str] = field(default_factory=list)
    expired: bool = False
    not_yet_valid: bool = False


@dataclass
class TlsVerdict:
    host: str
    verified: bool
    intercepted: bool = False
    interceptor: str = ""
    error: str = ""
    cert: PeerCert | None = None
    reason: str = ""

    @property
    def safe(self) -> bool:
        """Usable only if verification actually passed. Interception makes a
        connection unusable regardless of whether it would have verified —
        a re-signed link is not end-to-end even when the device trusts the
        signer."""
        return self.verified and not self.intercepted

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "verified": self.verified,
            "intercepted": self.intercepted,
            "interceptor": self.interceptor,
            "safe": self.safe,
            "reason": self.reason,
            "error": self.error,
            "cert": self.cert.__dict__ if self.cert else None,
        }


def _parse_cert(host: str, der: bytes, tls_version: str, peer_ip: str) -> PeerCert:
    pc = PeerCert(host=host, tls_version=tls_version, peer_ip=peer_ip)
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        c = x509.load_der_x509_certificate(der, default_backend())
        pc.subject = c.subject.rfc4514_string()
        pc.issuer = c.issuer.rfc4514_string()
        now = datetime.datetime.now(datetime.timezone.utc)
        pc.not_before = c.not_valid_before_utc.isoformat()
        pc.not_after = c.not_valid_after_utc.isoformat()
        pc.expired = now > c.not_valid_after_utc
        pc.not_yet_valid = now < c.not_valid_before_utc
        try:
            pc.sans = [
                e.value.get_values_for_type(x509.DNSName)
                for e in c.extensions
                if isinstance(e, x509.SubjectAlternativeName)
            ][0][:8]
        except Exception:  # noqa: BLE001
            pass
    except ImportError:
        pc.issuer = "(cryptography not installed — issuer unreadable)"
    return pc


def inspect(host: str, port: int = 443, timeout: int = 15) -> TlsVerdict:
    """Verify the peer properly AND identify an interceptor if there is one.

    Two separate connections on purpose: the first does real verification so the
    failure is genuine; the second disables it only to *read* the chain for
    attribution. Nothing is ever sent over the second.
    """
    v = TlsVerdict(host=host, verified=False)

    # 1. a real, verifying connection
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as w:
                der = w.getpeercert(binary_form=True)
                v.verified = True
                v.cert = _parse_cert(host, der, w.version(), w.getpeername()[0])
                v.reason = "verification passed against the system trust store"
                return v
    except ssl.SSLCertVerificationError as e:
        v.error = f"CERTIFICATE_VERIFY_FAILED: {e.verify_message if hasattr(e, 'verify_message') else e}"
    except Exception as e:  # noqa: BLE001
        v.error = f"{type(e).__name__}: {e}"

    # 2. attribution: read the chain without trusting it, send nothing
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as w:
                der = w.getpeercert(binary_form=True)
                cert = _parse_cert(host, der, w.version(), w.getpeername()[0])
                v.cert = cert
                iss = cert.issuer.lower()
                if any(m in iss for m in INTERCEPTION_MARKERS):
                    v.intercepted = True
                    serial = ""
                    for part in cert.issuer.split(","):
                        if part.strip().upper().startswith("CN=FGT") or "FGT" in part:
                            serial = part.strip()
                    v.interceptor = serial or cert.issuer[:120]
                    v.reason = (
                        "TLS is being re-signed by a network inspection appliance — "
                        "the connection is NOT end-to-end"
                    )
                elif not any(h in iss for h in PUBLIC_CA_HINTS):
                    v.reason = (
                        "issuer is not a recognised public CA — treat as untrusted "
                        "even though it is not a known appliance"
                    )
                else:
                    v.reason = (
                        "issuer looks like a public CA but the chain did not verify — "
                        "the device trust store is likely stale or incomplete"
                    )
                if cert.expired:
                    v.reason += f"; certificate EXPIRED {cert.not_after}"
                if cert.not_yet_valid:
                    v.reason += f"; certificate not valid until {cert.not_before} (check the clock)"
    except Exception as e:  # noqa: BLE001
        v.reason = v.reason or f"chain unreadable: {type(e).__name__}"

    return v


def inspect_many(hosts: list[str]) -> list[TlsVerdict]:
    return [inspect(h) for h in hosts]


def preflight(base_urls: list[str]) -> tuple[bool, list[TlsVerdict]]:
    """Check every endpoint the engine needs before doing real work.

    Returns (all_safe, verdicts). An unsafe endpoint is a refusal, not a
    warning — the engine will still scan and store locally, it just will not
    transmit to a link that is not end-to-end.
    """
    hosts = []
    for u in base_urls:
        h = u.split("://", 1)[-1].split("/")[0].split(":")[0]
        if h and h not in hosts:
            hosts.append(h)
    verdicts = inspect_many(hosts)
    return all(v.safe for v in verdicts), verdicts
