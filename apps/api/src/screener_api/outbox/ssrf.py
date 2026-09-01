"""Destination validation for outbound webhooks.

A webhook URL is supplied by a tenant and fetched by our infrastructure, which
is the definition of server-side request forgery. The interesting targets are
not on the internet:

    http://169.254.169.254/latest/meta-data/iam/security-credentials/
    http://metadata.google.internal/computeMetadata/v1/
    http://localhost:5432
    http://10.0.0.7/internal-admin

Three checks, and the third is the one that is usually missing.

1. **Scheme and shape.** HTTPS only. A plaintext webhook carries a signed
   payload over a network we do not control.
2. **Resolve, then judge the addresses.** Not the hostname — `localtest.me`
   and a thousand other public names resolve to 127.0.0.1. Every address the
   name resolves to must be global unicast.
**Known gap, stated rather than papered over.** Between validation and the
request, DNS can change its answer: the name resolves to a public address for
the check and to 169.254.169.254 for the connection. That is DNS rebinding, and
this module does **not** close it. Validation and connection are separate
resolutions, so the window exists.

The standard defence is to connect to the address that was validated and carry
the original hostname for TLS. The approach available here — rewriting the URL
to the IP and passing `extensions={"sni_hostname": host}` — was measured before
being trusted, and a deliberately wrong SNI hostname still completed the
handshake and returned 200. Certificate verification did not follow the
extension. Adopting it would have traded a narrow timing window for "any
certificate is accepted", which is a worse control than none, so it was not
adopted.

What remains standing: the attacker must control DNS for a name they registered,
with a TTL short enough to change the answer between two resolutions
milliseconds apart, and the payload they would reach the metadata service with
is a signed JSON document containing identifiers. Real, narrow, and recorded in
ADR-0018 rather than described as solved.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


class DestinationRefusedError(Exception):
    """The URL is not somewhere this system will send a request."""


# Ports that are not webhooks. Blocking them is defence in depth — the address
# checks above should already have stopped anything reachable here — but a
# tenant pointing a "webhook" at a database port is a signal worth refusing on.
BLOCKED_PORTS = frozenset(
    {22, 23, 25, 445, 465, 587, 993, 995, 1433, 3306, 5432, 6379, 9200, 11211, 27017}
)

MAX_URL_LENGTH = 2048


@dataclass(frozen=True)
class Destination:
    """A validated URL plus the address it resolved to at validation time."""

    url: str
    host: str
    port: int
    address: str

    @property
    def is_ipv6(self) -> bool:
        return ":" in self.address


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    # `is_global` alone is not enough: it is False for private ranges but the
    # named checks below cover the cases that carry credentials, and being
    # explicit about them documents what is actually being defended against.
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 — cloud instance metadata
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def validate(url: str, *, resolver: object = None, allow_private: bool = False) -> Destination:
    """Return a Destination, or raise DestinationRefusedError.

    ``resolver`` exists so the tests can drive every branch without a network
    and without depending on what a public DNS server says today.

    ``allow_private`` turns off the address check so the relay can be shown
    working against a container on the compose network. It is wired to a
    setting the process refuses to accept outside dev, and `/readyz` reports
    its state — a flag that disables a security control must be visible from
    outside, not discoverable only by reading someone's .env.
    """
    if len(url) > MAX_URL_LENGTH:
        raise DestinationRefusedError("url is too long")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise DestinationRefusedError(f"scheme {parsed.scheme!r} is not https")
    if not parsed.hostname:
        raise DestinationRefusedError("url has no host")
    if parsed.username or parsed.password:
        # "https://user:pass@evil.example/" — credentials in a URL are a
        # phishing shape, and some clients send them to the wrong host.
        raise DestinationRefusedError("credentials in the url are not accepted")

    host = parsed.hostname
    port = parsed.port or 443
    if port in BLOCKED_PORTS:
        raise DestinationRefusedError(f"port {port} is not a webhook port")

    if allow_private:
        # The address check is off, and resolution exists only to feed it.
        # Resolving anyway would make a reserved-TLD host in a dev compose file
        # fail for a reason that has nothing to do with the control.
        return Destination(url=url, host=host, port=port, address="")

    addresses = _resolve(host, port, resolver)
    if not addresses:
        raise DestinationRefusedError(f"{host} does not resolve")

    # EVERY address must be acceptable, not merely the first. A name that
    # returns one public and one private address would otherwise pass here and
    # connect to the private one on a retry.
    for address in addresses:
        if not _is_public(address):
            raise DestinationRefusedError(f"{host} resolves to non-public address {address}")

    return Destination(url=url, host=host, port=port, address=addresses[0])


def _resolve(host: str, port: int, resolver: object) -> list[str]:
    if resolver is not None:
        return list(resolver(host, port))  # type: ignore[operator]
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise DestinationRefusedError(f"{host} does not resolve: {exc.strerror}") from exc
    # sockaddr is (host, port) for IPv4 and (host, port, flowinfo, scopeid)
    # for IPv6; the address is element zero either way.
    return [str(info[4][0]) for info in infos]
