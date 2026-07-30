"""Envelope encryption for the highest-value secrets in the database.

Provider OAuth tokens open an athlete's entire Garmin account. Payout destination
references and MFA secrets are comparable. `docs/06` §5 requires all three to be
AES-256-GCM, envelope-encrypted under a KMS-held key, with the key id stored per
row so a key can be rotated without a re-encryption outage.

Three properties this module exists to guarantee:

**Real envelope encryption, not a single global key.** Each ciphertext carries its
own data key (DEK), which is itself encrypted by a key-encryption key (KEK) held
outside the database. Rotating the KEK therefore means re-wrapping a small DEK per
row, not re-encrypting every payload — the difference between a background job and
a maintenance window.

**Ciphertexts are bound to their location.** Every operation takes an
`aad` (additional authenticated data) naming the table, row and column the
ciphertext belongs to. GCM authenticates it without encrypting it, so a ciphertext
copied from one row to another **fails to decrypt**. Without this, an attacker with
write access to `provider_connections` could paste another athlete's token
ciphertext into their own row and have the server happily decrypt and use it. That
is a full account takeover through a write primitive, and the AAD closes it.

**The key provider is an interface, and the local one refuses production.** Nothing
above this module knows whether a KEK came from a config value or from AWS KMS.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.core.errors import ConfigurationError

__all__ = [
    "AAD",
    "DEK_BYTES",
    "NONCE_BYTES",
    "Ciphertext",
    "DecryptionFailed",
    "EnvelopeCipher",
    "KeyProvider",
    "LocalKeyProvider",
]

# AES-256: a 32-byte data key. GCM: a 96-bit nonce, which is the size the mode was
# designed around — a 12-byte nonce is used directly, anything else is hashed
# internally by the construction and buys nothing.
DEK_BYTES = 32
NONCE_BYTES = 12

# Blob layout version. A leading version byte means a future format change is a
# branch on read rather than an un-decryptable column.
_FORMAT_V1 = 1


class DecryptionFailed(Exception):
    """Authentication failed: wrong key, corrupted blob, or mismatched AAD.

    Deliberately does not say which. A caller cannot act differently on the three,
    and distinguishing them in a log line tells an attacker probing with moved
    ciphertexts whether they guessed the location right.
    """


@dataclass(frozen=True, slots=True)
class AAD:
    """Names the exact column and row a ciphertext is allowed to live in.

    Included in the GCM tag, so moving a ciphertext elsewhere makes it undecryptable.
    """

    table: str
    column: str
    row_id: str

    def to_bytes(self) -> bytes:
        # Length-prefixed rather than delimiter-joined: a row id containing the
        # delimiter would otherwise let two distinct locations produce identical
        # AAD, which is exactly the ambiguity this is meant to remove.
        parts = (self.table.encode(), self.column.encode(), self.row_id.encode())
        return b"".join(struct.pack("!H", len(p)) + p for p in parts)


@dataclass(frozen=True, slots=True)
class Ciphertext:
    """What gets stored: an opaque blob plus the id of the KEK that wrapped its DEK."""

    key_id: str
    blob: bytes

    @property
    def fingerprint(self) -> str:
        """A short, non-reversible tag for logs and audit rows.

        Lets an operator correlate "this row" with "that incident" without the
        ciphertext itself appearing in a log sink.
        """
        return hashlib.sha256(self.blob).hexdigest()[:16]


@runtime_checkable
class KeyProvider(Protocol):
    """Wraps and unwraps data keys. The KEK never leaves the implementation."""

    @property
    def active_key_id(self) -> str:
        """The key id new ciphertexts are wrapped under."""
        ...

    def wrap(self, dek: bytes) -> tuple[str, bytes]:
        """Encrypt a DEK under the active KEK. Returns `(key_id, wrapped)`."""
        ...

    def unwrap(self, key_id: str, wrapped: bytes) -> bytes:
        """Decrypt a DEK previously wrapped under `key_id`.

        Must still succeed for retired-but-not-destroyed keys, or rotation breaks
        every row not yet re-wrapped.
        """
        ...


class LocalKeyProvider:
    """KEKs from configuration. Local and CI only.

    Refuses to run in a deployed environment: a KEK in an environment variable is
    readable by anything that can read the process environment, is not audited on
    use, and cannot be revoked. Those are the three things a KMS is for.
    """

    def __init__(self, keys: dict[str, bytes], active_key_id: str, *, allow: bool) -> None:
        if not allow:
            raise ConfigurationError(
                "LocalKeyProvider is not permitted outside local/ci. Configure a "
                "KMS-backed key provider."
            )
        if active_key_id not in keys:
            raise ConfigurationError(f"active key id {active_key_id!r} is not among the configured keys")
        for key_id, key in keys.items():
            if len(key) != DEK_BYTES:
                raise ConfigurationError(
                    f"KEK {key_id!r} is {len(key)} bytes; AES-256 requires {DEK_BYTES}"
                )
        self._keys = dict(keys)
        self._active = active_key_id

    @classmethod
    def from_base64(cls, encoded: dict[str, str], active_key_id: str, *, allow: bool) -> LocalKeyProvider:
        try:
            keys = {k: base64.b64decode(v, validate=True) for k, v in encoded.items()}
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("KEK material is not valid base64") from exc
        return cls(keys, active_key_id, allow=allow)

    @property
    def active_key_id(self) -> str:
        return self._active

    def wrap(self, dek: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(NONCE_BYTES)
        wrapped = AESGCM(self._keys[self._active]).encrypt(nonce, dek, b"dek-wrap")
        return self._active, nonce + wrapped

    def unwrap(self, key_id: str, wrapped: bytes) -> bytes:
        key = self._keys.get(key_id)
        if key is None:
            # A row wrapped under a key we no longer hold. Recoverable only by
            # restoring the key, so say so precisely — this one is an operational
            # fault, not an attack signal.
            raise ConfigurationError(f"no KEK available for key id {key_id!r}")
        nonce, body = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        try:
            return AESGCM(key).decrypt(nonce, body, b"dek-wrap")
        except InvalidTag as exc:
            raise DecryptionFailed("could not unwrap data key") from exc


class EnvelopeCipher:
    """Encrypts and decrypts column values. The only place AES is called."""

    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    def encrypt(self, plaintext: bytes, aad: AAD) -> Ciphertext:
        dek = os.urandom(DEK_BYTES)
        key_id, wrapped = self._keys.wrap(dek)
        nonce = os.urandom(NONCE_BYTES)
        body = AESGCM(dek).encrypt(nonce, plaintext, aad.to_bytes())

        # version | wrapped-DEK length | wrapped DEK | nonce | ciphertext+tag
        blob = struct.pack("!BH", _FORMAT_V1, len(wrapped)) + wrapped + nonce + body
        return Ciphertext(key_id=key_id, blob=blob)

    def encrypt_str(self, plaintext: str, aad: AAD) -> Ciphertext:
        return self.encrypt(plaintext.encode("utf-8"), aad)

    def decrypt(self, ciphertext: Ciphertext, aad: AAD) -> bytes:
        blob = ciphertext.blob
        header = struct.calcsize("!BH")
        if len(blob) < header:
            raise DecryptionFailed("ciphertext is truncated")
        version, wrapped_len = struct.unpack("!BH", blob[:header])
        if version != _FORMAT_V1:
            raise DecryptionFailed(f"unsupported ciphertext format version {version}")

        offset = header + wrapped_len
        wrapped, rest = blob[header:offset], blob[offset:]
        if len(wrapped) != wrapped_len or len(rest) < NONCE_BYTES:
            raise DecryptionFailed("ciphertext is truncated")

        dek = self._keys.unwrap(ciphertext.key_id, wrapped)
        nonce, body = rest[:NONCE_BYTES], rest[NONCE_BYTES:]
        try:
            return AESGCM(dek).decrypt(nonce, body, aad.to_bytes())
        except InvalidTag as exc:
            # Reached when the key is right but the AAD is not — i.e. this
            # ciphertext does not belong in the row it was read from.
            raise DecryptionFailed("ciphertext failed authentication") from exc

    def decrypt_str(self, ciphertext: Ciphertext, aad: AAD) -> str:
        return self.decrypt(ciphertext, aad).decode("utf-8")

    def rewrap(self, ciphertext: Ciphertext, aad: AAD) -> Ciphertext:
        """Re-encrypt under the currently active KEK.

        The rotation path. Decrypting and re-encrypting is deliberate rather than
        unwrapping and re-wrapping the DEK in place: it re-verifies the AAD, so a
        rotation sweep also detects any ciphertext sitting in the wrong row.
        """
        return self.encrypt(self.decrypt(ciphertext, aad), aad)
