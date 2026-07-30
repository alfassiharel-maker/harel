"""Envelope encryption tests.

The security property that matters most here is the AAD binding: a ciphertext must
be undecryptable anywhere except the exact row and column it was written for.
Without it, an attacker with write access to `provider_connections` pastes another
athlete's token ciphertext into their own row and the server decrypts and uses it —
full account takeover through a write primitive. `test_ciphertext_is_bound_to_*`
is that attack, and it must fail.
"""

from __future__ import annotations

import base64
import os
import unittest

from backend.core.crypto import (
    AAD,
    DEK_BYTES,
    Ciphertext,
    DecryptionFailed,
    EnvelopeCipher,
    LocalKeyProvider,
)
from backend.core.errors import ConfigurationError

KEY_A = base64.b64encode(b"A" * DEK_BYTES).decode()
KEY_B = base64.b64encode(b"B" * DEK_BYTES).decode()

TOKEN_AAD = AAD(table="training.provider_connections", column="access_token_ct", row_id="row-1")


def _cipher(active: str = "k1", keys: dict[str, str] | None = None) -> EnvelopeCipher:
    provider = LocalKeyProvider.from_base64(keys or {"k1": KEY_A}, active, allow=True)
    return EnvelopeCipher(provider)


class LocalKeyProviderTests(unittest.TestCase):
    def test_refuses_when_not_permitted(self) -> None:
        # The guard that stops a config-file KEK reaching production, where it would
        # be unauditable on use and unrevokable.
        with self.assertRaises(ConfigurationError):
            LocalKeyProvider.from_base64({"k1": KEY_A}, "k1", allow=False)

    def test_rejects_wrong_key_length(self) -> None:
        short = base64.b64encode(b"too-short").decode()
        with self.assertRaises(ConfigurationError):
            LocalKeyProvider.from_base64({"k1": short}, "k1", allow=True)

    def test_rejects_active_key_not_in_keyset(self) -> None:
        with self.assertRaises(ConfigurationError):
            LocalKeyProvider.from_base64({"k1": KEY_A}, "k2", allow=True)

    def test_rejects_non_base64_material(self) -> None:
        with self.assertRaises(ConfigurationError):
            LocalKeyProvider.from_base64({"k1": "not base64!!"}, "k1", allow=True)


class RoundTripTests(unittest.TestCase):
    def test_round_trip_returns_the_plaintext(self) -> None:
        cipher = _cipher()
        secret = "garmin-access-token-value"
        encrypted = cipher.encrypt_str(secret, TOKEN_AAD)
        self.assertEqual(cipher.decrypt_str(encrypted, TOKEN_AAD), secret)

    def test_key_id_is_recorded_for_rotation(self) -> None:
        # Stored per row so a KEK can be retired without a re-encryption outage.
        encrypted = _cipher().encrypt(b"x", TOKEN_AAD)
        self.assertEqual(encrypted.key_id, "k1")

    def test_ciphertext_does_not_contain_the_plaintext(self) -> None:
        secret = b"a-very-recognisable-token"
        encrypted = _cipher().encrypt(secret, TOKEN_AAD)
        self.assertNotIn(secret, encrypted.blob)

    def test_same_plaintext_encrypts_differently_each_time(self) -> None:
        # A fresh DEK and nonce per call. Deterministic ciphertext would let an
        # observer tell that two athletes hold the same token value.
        cipher = _cipher()
        first = cipher.encrypt(b"same", TOKEN_AAD)
        second = cipher.encrypt(b"same", TOKEN_AAD)
        self.assertNotEqual(first.blob, second.blob)

    def test_empty_plaintext_round_trips(self) -> None:
        cipher = _cipher()
        self.assertEqual(cipher.decrypt(cipher.encrypt(b"", TOKEN_AAD), TOKEN_AAD), b"")


class AadBindingTests(unittest.TestCase):
    """The attack these tests exist to prevent: moving a ciphertext between rows."""

    def test_ciphertext_is_bound_to_its_row(self) -> None:
        cipher = _cipher()
        victim = cipher.encrypt_str("victim-token", TOKEN_AAD)
        attacker_row = AAD(
            table="training.provider_connections", column="access_token_ct", row_id="row-2"
        )
        with self.assertRaises(DecryptionFailed):
            cipher.decrypt(victim, attacker_row)

    def test_ciphertext_is_bound_to_its_column(self) -> None:
        cipher = _cipher()
        access = cipher.encrypt_str("access-token", TOKEN_AAD)
        refresh_column = AAD(
            table="training.provider_connections", column="refresh_token_ct", row_id="row-1"
        )
        with self.assertRaises(DecryptionFailed):
            cipher.decrypt(access, refresh_column)

    def test_ciphertext_is_bound_to_its_table(self) -> None:
        cipher = _cipher()
        token = cipher.encrypt_str("token", TOKEN_AAD)
        other_table = AAD(table="rewards.payouts", column="access_token_ct", row_id="row-1")
        with self.assertRaises(DecryptionFailed):
            cipher.decrypt(token, other_table)

    def test_aad_components_cannot_be_confused_by_concatenation(self) -> None:
        # Length-prefixed encoding: without it, ("ab","c") and ("a","bc") would
        # produce identical AAD and two distinct locations would be interchangeable.
        left = AAD(table="ab", column="c", row_id="r").to_bytes()
        right = AAD(table="a", column="bc", row_id="r").to_bytes()
        self.assertNotEqual(left, right)


class TamperingTests(unittest.TestCase):
    def test_flipped_ciphertext_byte_fails_authentication(self) -> None:
        cipher = _cipher()
        encrypted = cipher.encrypt(b"sensitive", TOKEN_AAD)
        mutated = bytearray(encrypted.blob)
        mutated[-1] ^= 0x01
        with self.assertRaises(DecryptionFailed):
            cipher.decrypt(Ciphertext(key_id=encrypted.key_id, blob=bytes(mutated)), TOKEN_AAD)

    def test_truncated_blob_is_rejected_not_crashed(self) -> None:
        cipher = _cipher()
        encrypted = cipher.encrypt(b"sensitive", TOKEN_AAD)
        for cut in (0, 1, 2, 5):
            with self.assertRaises(DecryptionFailed):
                cipher.decrypt(Ciphertext(key_id=encrypted.key_id, blob=encrypted.blob[:cut]), TOKEN_AAD)

    def test_unknown_format_version_is_rejected(self) -> None:
        cipher = _cipher()
        encrypted = cipher.encrypt(b"x", TOKEN_AAD)
        mutated = bytes([99]) + encrypted.blob[1:]
        with self.assertRaises(DecryptionFailed):
            cipher.decrypt(Ciphertext(key_id=encrypted.key_id, blob=mutated), TOKEN_AAD)

    def test_wrong_kek_cannot_unwrap(self) -> None:
        written = _cipher("k1", {"k1": KEY_A}).encrypt(b"secret", TOKEN_AAD)
        # Same key id, different material — a restored-from-the-wrong-vault scenario.
        reader = _cipher("k1", {"k1": KEY_B})
        with self.assertRaises(DecryptionFailed):
            reader.decrypt(written, TOKEN_AAD)

    def test_missing_kek_is_a_configuration_fault(self) -> None:
        written = _cipher("k1", {"k1": KEY_A}).encrypt(b"secret", TOKEN_AAD)
        reader = _cipher("k2", {"k2": KEY_B})
        # Distinct from DecryptionFailed: recoverable by restoring the key, so it
        # must not be reported as a possible attack.
        with self.assertRaises(ConfigurationError):
            reader.decrypt(written, TOKEN_AAD)


class RotationTests(unittest.TestCase):
    def test_rewrap_moves_a_ciphertext_to_the_active_key(self) -> None:
        old = _cipher("k1", {"k1": KEY_A})
        written = old.encrypt_str("token", TOKEN_AAD)

        # Both keys held during rotation, k2 now active.
        rotating = _cipher("k2", {"k1": KEY_A, "k2": KEY_B})
        rewrapped = rotating.rewrap(written, TOKEN_AAD)

        self.assertEqual(rewrapped.key_id, "k2")
        self.assertEqual(rotating.decrypt_str(rewrapped, TOKEN_AAD), "token")

    def test_old_ciphertexts_still_readable_during_rotation(self) -> None:
        # The property that makes rotation a background job rather than an outage.
        written = _cipher("k1", {"k1": KEY_A}).encrypt_str("token", TOKEN_AAD)
        rotating = _cipher("k2", {"k1": KEY_A, "k2": KEY_B})
        self.assertEqual(rotating.decrypt_str(written, TOKEN_AAD), "token")

    def test_rewrap_detects_a_misplaced_ciphertext(self) -> None:
        # Re-encrypting rather than re-wrapping the DEK in place means a rotation
        # sweep also verifies every ciphertext is in the row it claims.
        cipher = _cipher("k1", {"k1": KEY_A})
        written = cipher.encrypt_str("token", TOKEN_AAD)
        wrong_row = AAD(table="training.provider_connections", column="access_token_ct", row_id="row-9")
        with self.assertRaises(DecryptionFailed):
            cipher.rewrap(written, wrong_row)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_short(self) -> None:
        encrypted = _cipher().encrypt(b"x", TOKEN_AAD)
        self.assertEqual(encrypted.fingerprint, encrypted.fingerprint)
        self.assertEqual(len(encrypted.fingerprint), 16)

    def test_fingerprint_differs_between_ciphertexts(self) -> None:
        cipher = _cipher()
        a = cipher.encrypt(b"x", TOKEN_AAD)
        b = cipher.encrypt(b"y", TOKEN_AAD)
        self.assertNotEqual(a.fingerprint, b.fingerprint)


class LargePayloadTests(unittest.TestCase):
    def test_handles_a_payload_larger_than_one_block(self) -> None:
        cipher = _cipher()
        payload = os.urandom(10_000)
        self.assertEqual(cipher.decrypt(cipher.encrypt(payload, TOKEN_AAD), TOKEN_AAD), payload)


if __name__ == "__main__":
    unittest.main()
