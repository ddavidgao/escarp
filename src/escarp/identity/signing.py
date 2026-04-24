"""RFC9421 HTTP message signing for Web Bot Auth.

Implements the minimal subset needed for Web Bot Auth:
covered components: "date", "@method", "@path", "@authority",
                    "content-type", "content-length" (all optional per-call)
parameters: created, keyid
algorithm: ed25519 (always)
label: sig1 (always)

Signature-Input uses RFC8941 Structured Fields serialization.
Signature base uses the canonicalization from RFC9421 Section 2.5.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Components we support. Derived components start with "@".
SUPPORTED_COMPONENTS = frozenset([
    "date",
    "content-type",
    "content-length",
    "content-digest",
    "accept",
    "@method",
    "@path",
    "@authority",
    "@target-uri",
    "@scheme",
    "@query",
])


def _component_value(request: httpx.Request, component: str) -> str:
    """Extract and canonicalize a single covered component value per RFC9421 §2."""
    if component == "@method":
        return request.method.upper()
    if component == "@path":
        parsed = urlparse(str(request.url))
        return parsed.path or "/"
    if component == "@authority":
        parsed = urlparse(str(request.url))
        # Include port only if non-default per RFC9421 §2.2.3
        host = parsed.hostname or ""
        port = parsed.port
        scheme = parsed.scheme
        if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
            return f"{host}:{port}"
        return host
    if component == "@target-uri":
        return str(request.url)
    if component == "@scheme":
        return urlparse(str(request.url)).scheme
    if component == "@query":
        parsed = urlparse(str(request.url))
        return f"?{parsed.query}" if parsed.query else "?"

    # Regular header field — lowercase, single value, stripped
    raw = request.headers.get(component)
    if raw is None:
        raise ValueError(f"Required covered component {component!r} not present in request")
    value: str = raw
    return value.strip()


def _build_signature_base(
    request: httpx.Request,
    covered: Sequence[str],
    created: int,
    keyid: str,
    label: str = "sig1",
) -> bytes:
    """Build the signature base string per RFC9421 §2.5.

    Each covered component line is: "<component-id>": <value>\\n
    The final line is always the @signature-params line.
    """
    lines: list[str] = []

    for component in covered:
        value = _component_value(request, component)
        lines.append(f'"{component}": {value}')

    # @signature-params line — must be last, per RFC9421 §2.5
    component_ids = " ".join(f'"{c}"' for c in covered)
    sig_params = f'({component_ids});created={created};keyid="{keyid}"'
    lines.append(f'"@signature-params": {sig_params}')

    return "\n".join(lines).encode("utf-8")


def _build_signature_input(
    covered: Sequence[str],
    created: int,
    keyid: str,
    label: str = "sig1",
) -> str:
    """Build the Signature-Input header value per RFC9421 §4.1."""
    component_ids = " ".join(f'"{c}"' for c in covered)
    return f'{label}=({component_ids});created={created};keyid="{keyid}"'


def sign_request(
    request: httpx.Request,
    private_key: Ed25519PrivateKey,
    keyid: str,
    created: int,
    covered: Sequence[str] = ("@method", "@authority", "@path"),
    label: str = "sig1",
) -> httpx.Request:
    """Sign an httpx.Request per RFC9421, returning a new request with
    Signature-Input and Signature headers added.

    Args:
        request: The request to sign. Must already have all covered headers set.
        private_key: Ed25519 private key.
        keyid: Key identifier string, published at the key directory endpoint.
        created: Unix timestamp (int) for the `created` parameter.
        covered: Ordered list of component identifiers to cover.
        label: Signature label (default "sig1").
    """
    for c in covered:
        if c not in SUPPORTED_COMPONENTS:
            raise ValueError(f"Unsupported covered component: {c!r}")

    sig_base = _build_signature_base(request, covered, created, keyid, label)
    raw_sig = private_key.sign(sig_base)
    sig_b64 = base64.b64encode(raw_sig).decode("ascii")

    sig_input_value = _build_signature_input(covered, created, keyid, label)
    sig_value = f"{label}=:{sig_b64}:"

    # Build new headers: preserve existing, add signature headers
    merged = {k: v for k, v in request.headers.items()}
    merged["Signature-Input"] = sig_input_value
    merged["Signature"] = sig_value

    return httpx.Request(
        method=request.method,
        url=request.url,
        headers=merged,
        content=request.content,
    )
