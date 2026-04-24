"""Ed25519 keypair generation and persistence.

Keys are stored as PEM files under ~/.config/escarp/keys/<name>/.
No encryption at rest for v0 — keyring integration is v0.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _keys_dir() -> Path:
    return Path.home() / ".config" / "escarp" / "keys"


@dataclass(frozen=True)
class AgentKeypair:
    name: str
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    def public_key_pem(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def private_key_pem(self) -> bytes:
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )


def generate_keypair(name: str) -> AgentKeypair:
    """Generate a new Ed25519 keypair and persist it to disk."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    keypair = AgentKeypair(name=name, private_key=private_key, public_key=public_key)
    _save_keypair(keypair)
    return keypair


def load_keypair(name: str) -> AgentKeypair:
    """Load an existing keypair from disk. Raises FileNotFoundError if absent."""
    key_dir = _keys_dir() / name
    private_pem = (key_dir / "private.pem").read_bytes()
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError(f"Key {name!r} is not an Ed25519 private key")
    return AgentKeypair(
        name=name,
        private_key=private_key,
        public_key=private_key.public_key(),
    )


def _save_keypair(keypair: AgentKeypair) -> None:
    key_dir = _keys_dir() / keypair.name
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / "private.pem").write_bytes(keypair.private_key_pem())
    (key_dir / "public.pem").write_bytes(keypair.public_key_pem())
