"""Signing tests.

The primary test is RFC9421 Appendix B.2.6: given the spec's exact private key,
request, and covered components, the output must match the spec's signature
byte-for-byte. This is the only non-circular correctness test for signing.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from escarp.identity.keypair import generate_keypair, load_keypair
from escarp.identity.signing import (
    _build_signature_base,
    sign_request,
)

# ---------------------------------------------------------------------------
# RFC9421 Appendix B test fixtures
# ---------------------------------------------------------------------------

# B.1.4: test-key-ed25519 private key (PKCS#8 PEM, no encryption)
RFC9421_PRIVATE_KEY_PEM = b"""-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIJ+DYvh6SEqVTm50DFtMDoQikTmiCqirVv9mWG9qfSnF
-----END PRIVATE KEY-----"""

# B.2.6: the request being signed
RFC9421_REQUEST = httpx.Request(
    method="POST",
    url="https://example.com/foo?param=Value&Pet=dog",
    headers={
        "Date": "Tue, 20 Apr 2021 02:07:55 GMT",
        "Content-Type": "application/json",
        "Content-Digest": (
            "sha-512=:WZDPaVn/7XgHaAy8pmojAkGWoRx2UFChF41A2svX+T"
            "aPm+AbwAgBWnrIiYllu7BNNyealdVLvRwEmTHWXvJwew==:"
        ),
        "Content-Length": "18",
    },
    content=b'{"hello": "world"}',
)

# B.2.6: covered components, created, keyid
RFC9421_COVERED = ("date", "@method", "@path", "@authority", "content-type", "content-length")
RFC9421_CREATED = 1618884473
RFC9421_KEYID = "test-key-ed25519"
RFC9421_LABEL = "sig-b26"

# B.2.6: expected signature base (verbatim from RFC, lines joined by \n)
RFC9421_EXPECTED_SIG_BASE = (
    '"date": Tue, 20 Apr 2021 02:07:55 GMT\n'
    '"@method": POST\n'
    '"@path": /foo\n'
    '"@authority": example.com\n'
    '"content-type": application/json\n'
    '"content-length": 18\n'
    '"@signature-params": ("date" "@method" "@path" "@authority" '
    '"content-type" "content-length");created=1618884473;keyid="test-key-ed25519"'
)

# B.2.6: expected Signature header value (the base64 inside :...:)
RFC9421_EXPECTED_SIG_B64 = "wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw=="


@pytest.fixture()
def rfc9421_private_key() -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(RFC9421_PRIVATE_KEY_PEM, password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


# ---------------------------------------------------------------------------
# Signature base construction (deterministic, no crypto — test independently)
# ---------------------------------------------------------------------------

def test_b26_signature_base_exact(rfc9421_private_key: Ed25519PrivateKey) -> None:
    """The signature base for B.2.6 must match the RFC byte-for-byte."""
    sig_base = _build_signature_base(
        RFC9421_REQUEST,
        RFC9421_COVERED,
        RFC9421_CREATED,
        RFC9421_KEYID,
        RFC9421_LABEL,
    )
    assert sig_base.decode("utf-8") == RFC9421_EXPECTED_SIG_BASE, (
        f"Signature base mismatch.\n"
        f"Got:\n{sig_base.decode()}\n\n"
        f"Expected:\n{RFC9421_EXPECTED_SIG_BASE}"
    )


# ---------------------------------------------------------------------------
# Full signing — RFC9421 Appendix B.2.6 test vector
# ---------------------------------------------------------------------------

def test_b26_signature_matches_rfc_vector(rfc9421_private_key: Ed25519PrivateKey) -> None:
    """sign_request output must match RFC9421 B.2.6 byte-for-byte.

    Ed25519 is deterministic (RFC8032) — same key + same message = same signature.
    If this test fails, the signing math is wrong.
    """
    signed = sign_request(
        RFC9421_REQUEST,
        rfc9421_private_key,
        keyid=RFC9421_KEYID,
        created=RFC9421_CREATED,
        covered=RFC9421_COVERED,
        label=RFC9421_LABEL,
    )

    sig_header = signed.headers["Signature"]
    # Header value is: sig-b26=:<base64>:
    assert sig_header.startswith(f"{RFC9421_LABEL}=:"), f"Unexpected Signature header: {sig_header}"
    actual_b64 = sig_header[len(RFC9421_LABEL) + 2:-1]  # strip label=: and trailing :
    assert actual_b64 == RFC9421_EXPECTED_SIG_B64, (
        f"Signature mismatch.\n"
        f"Got:      {actual_b64}\n"
        f"Expected: {RFC9421_EXPECTED_SIG_B64}\n"
        f"This means the signing math or canonicalization is wrong."
    )


def test_b26_signature_input_header(rfc9421_private_key: Ed25519PrivateKey) -> None:
    """Signature-Input header must also match the RFC B.2.6 expected value."""
    signed = sign_request(
        RFC9421_REQUEST,
        rfc9421_private_key,
        keyid=RFC9421_KEYID,
        created=RFC9421_CREATED,
        covered=RFC9421_COVERED,
        label=RFC9421_LABEL,
    )
    expected = (
        'sig-b26=("date" "@method" "@path" "@authority" '
        '"content-type" "content-length");created=1618884473;keyid="test-key-ed25519"'
    )
    assert signed.headers["Signature-Input"] == expected


# ---------------------------------------------------------------------------
# Verification: signed output verifies against the public key
# ---------------------------------------------------------------------------

def test_signed_request_verifies(rfc9421_private_key: Ed25519PrivateKey) -> None:
    """The signature in the signed request verifies against the public key.

    This is NOT a substitute for the B.2.6 vector test — it's a separate
    property: our verifier agrees with our signer. Both must pass.
    """
    public_key: Ed25519PublicKey = rfc9421_private_key.public_key()

    signed = sign_request(
        RFC9421_REQUEST,
        rfc9421_private_key,
        keyid=RFC9421_KEYID,
        created=RFC9421_CREATED,
        covered=RFC9421_COVERED,
        label=RFC9421_LABEL,
    )

    sig_header = signed.headers["Signature"]
    b64 = sig_header[len(RFC9421_LABEL) + 2:-1]
    raw_sig = base64.b64decode(b64)

    sig_base = _build_signature_base(
        RFC9421_REQUEST,
        RFC9421_COVERED,
        RFC9421_CREATED,
        RFC9421_KEYID,
        RFC9421_LABEL,
    )

    # Will raise InvalidSignature if wrong — no assertion needed
    public_key.verify(raw_sig, sig_base)


# ---------------------------------------------------------------------------
# Keypair generation and persistence
# ---------------------------------------------------------------------------

def test_generate_keypair_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_keypair saves to disk; load_keypair reloads the same key."""
    monkeypatch.setattr(
        "escarp.identity.keypair._keys_dir",
        lambda: tmp_path / "keys",
    )

    kp = generate_keypair("test-agent")
    assert kp.name == "test-agent"

    kp2 = load_keypair("test-agent")
    assert kp2.name == "test-agent"
    # Same public key bytes = same key pair
    assert kp.public_key_pem() == kp2.public_key_pem()


def test_generate_keypair_files_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_keypair writes both private.pem and public.pem."""
    monkeypatch.setattr(
        "escarp.identity.keypair._keys_dir",
        lambda: tmp_path / "keys",
    )
    generate_keypair("test-agent")
    key_dir = tmp_path / "keys" / "test-agent"
    assert (key_dir / "private.pem").exists()
    assert (key_dir / "public.pem").exists()


def test_load_keypair_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "escarp.identity.keypair._keys_dir",
        lambda: tmp_path / "keys",
    )
    with pytest.raises(FileNotFoundError):
        load_keypair("nonexistent")


def test_two_generated_keypairs_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "escarp.identity.keypair._keys_dir",
        lambda: tmp_path / "keys",
    )
    kp1 = generate_keypair("agent-1")
    kp2 = generate_keypair("agent-2")
    assert kp1.public_key_pem() != kp2.public_key_pem()


def test_unsupported_component_raises() -> None:
    key = Ed25519PrivateKey.generate()
    req = httpx.Request("GET", "https://example.com/")
    with pytest.raises(ValueError, match="Unsupported covered component"):
        sign_request(req, key, keyid="k", created=1000, covered=("@bogus",))
