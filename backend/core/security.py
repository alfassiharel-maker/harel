"""Security primitives: password hashing, access tokens, refresh tokens.

Deliberately narrow. Anything policy-shaped (lockout thresholds, rotation rules)
lives in the identity service; this module only provides correct primitives.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.core.config import Settings
from backend.core.context import Principal
from backend.core.errors import ConfigurationError, Unauthenticated

__all__ = [
    "AccessToken",
    "PasswordService",
    "TokenService",
    "constant_time_equals",
    "generate_refresh_token",
    "hash_refresh_token",
]

# ES256 only. Never a symmetric algorithm: with HS256 the verification key is the
# signing key, so anything able to verify a token can also mint one.
_JWT_ALGORITHM = "ES256"

_REFRESH_TOKEN_BYTES = 32


class PasswordService:
    """Argon2id hashing.

    Argon2id is memory-hard, which is what makes offline cracking of a leaked
    hash expensive; bcrypt is not. Parameters are the argon2-cffi defaults, which
    track current guidance — pinning our own numbers here would mean they silently
    become stale.
    """

    def __init__(self) -> None:
        self._hasher = PasswordHasher()
        # Precomputed so `verify_dummy` costs the same as a real verification.
        # Built once at construction, not per call, so it is not on the hot path.
        self._dummy_hash = self._hasher.hash("timing-equalisation-placeholder")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, stored_hash: str, password: str) -> bool:
        try:
            self._hasher.verify(stored_hash, password)
            return True
        except (VerifyMismatchError, InvalidHashError, ValueError):
            return False

    def verify_dummy(self, password: str) -> None:
        """Burn the same CPU as a real verification, and discard the result.

        Called when no account matches the submitted email. Without it, a
        non-existent account returns measurably faster than a wrong password, and
        the login endpoint becomes an account-existence oracle (docs/06 §2, T9).
        """
        # The dummy hash never matches a real password, so a mismatch is the
        # expected outcome — suppressed specifically rather than with a blanket
        # `except Exception`, so a genuinely broken hasher still surfaces.
        with contextlib.suppress(VerifyMismatchError, InvalidHashError, ValueError):
            self._hasher.verify(self._dummy_hash, password)

    def needs_rehash(self, stored_hash: str) -> bool:
        """True when the hash was made with weaker parameters than we now use.

        Callers should transparently upgrade on a successful login; that is the
        only moment the plaintext is available.
        """
        try:
            return self._hasher.check_needs_rehash(stored_hash)
        except (InvalidHashError, ValueError):
            return False


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    expires_at: dt.datetime
    jti: str


class TokenService:
    """Signs and verifies short-lived access tokens.

    Asymmetric (ES256) so that a future extracted service can verify tokens with
    the public key alone and never hold signing material.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._private_pem, self._public_pem = self._load_or_generate_keys(settings)

    @staticmethod
    def _load_or_generate_keys(settings: Settings) -> tuple[bytes, bytes]:
        private_path = settings.jwt_private_key_path
        public_path = settings.jwt_public_key_path

        if private_path and public_path:
            if not private_path.exists() or not public_path.exists():
                raise ConfigurationError(
                    f"JWT key paths configured but missing: {private_path}, {public_path}"
                )
            return private_path.read_bytes(), public_path.read_bytes()

        if not settings.allows_ephemeral_jwt_keys:
            raise ConfigurationError(
                "JWT_PRIVATE_KEY_PATH and JWT_PUBLIC_KEY_PATH are required outside "
                "local and CI. An ephemeral key invalidates every session on "
                "restart and cannot be used for incident analysis."
            )

        # Local/CI only: a per-process keypair. Sessions do not survive a restart,
        # which is correct for a throwaway environment.
        key = ec.generate_private_key(ec.SECP256R1())
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return private_pem, public_pem

    def create_access_token(self, principal: Principal) -> AccessToken:
        now = dt.datetime.now(dt.UTC)
        expires_at = now + dt.timedelta(seconds=self._settings.access_token_ttl_seconds)
        jti = str(uuid.uuid4())
        claims: dict[str, object] = {
            "sub": str(principal.user_id),
            "role": principal.role,
            "iss": self._settings.jwt_issuer,
            "aud": self._settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": jti,
        }
        if principal.org_id is not None:
            claims["org"] = str(principal.org_id)
        token = jwt.encode(claims, self._private_pem, algorithm=_JWT_ALGORITHM)
        return AccessToken(token=token, expires_at=expires_at, jti=jti)

    def decode_access_token(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._public_pem,
                # Explicit allowlist. Passing the algorithm from the token's own
                # header is the classic algorithm-confusion vulnerability.
                algorithms=[_JWT_ALGORITHM],
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
                options={
                    "require": ["sub", "exp", "iat", "iss", "aud", "role"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise Unauthenticated("Access token has expired.") from exc
        except jwt.InvalidTokenError as exc:
            # Covers bad signature, wrong audience/issuer, malformed token and
            # missing required claims. The reason is deliberately not echoed: it
            # tells an attacker which check they failed.
            raise Unauthenticated("Access token is invalid.") from exc

        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise Unauthenticated("Access token is invalid.") from exc

        org_raw = claims.get("org")
        org_id: uuid.UUID | None = None
        if org_raw is not None:
            try:
                org_id = uuid.UUID(str(org_raw))
            except ValueError as exc:
                raise Unauthenticated("Access token is invalid.") from exc

        role = str(claims["role"])
        if role not in ("athlete", "coach", "partner", "admin", "support"):
            raise Unauthenticated("Access token is invalid.")

        return Principal(user_id=user_id, role=role, org_id=org_id)


def generate_refresh_token() -> tuple[str, bytes]:
    """Return `(raw_token, sha256_digest)`.

    The raw token goes to the client exactly once and is never stored. Only the
    digest is persisted, so a database disclosure does not hand over live sessions
    (docs/06 §5). SHA-256 without a work factor is correct here: the token is 256
    bits of entropy, so there is no guessable input to slow down.
    """
    raw = secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


def constant_time_equals(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)
