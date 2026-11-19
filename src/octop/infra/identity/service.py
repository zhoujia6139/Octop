"""Password/MFA accounts and one active executing device per cloud user.

Opaque tokens have separate purposes, hashed persistence and live revocation
checks. Every security transition locks the user row before reading authority.
The synchronous service is called off the event loop by HTTP adapters.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, cast

from cryptography.fernet import Fernet, InvalidToken

from octop.infra.auth.mfa.recovery import generate_codes, normalize_code
from octop.infra.auth.mfa.totp import generate_secret, otpauth_uri, verify_code
from octop.infra.crypto.fernet import PURPOSE_MFA, get_fernet
from octop.infra.db.repos.cloud_identity import (
    CloudIdentityRepo,
    IdentityDevice,
    IdentitySession,
    IdentityTransaction,
    IdentityUser,
    SessionKind,
)
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.identity.contracts import (
    DeviceGrant,
    DeviceView,
    IdentityView,
    LoginView,
    MfaEnroll,
    RecoveryView,
    TokenView,
    UserView,
)
from octop.infra.users.password import hash_password, validate_password_policy, verify_password
from octop.infra.utils.ulid import new_ulid

ACCOUNT_TTL = 900
DEVICE_TTL = 300
REFRESH_TTL = 30 * 86400
CHALLENGE_TTL = 300


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def fail(code: ErrorCode = ErrorCode.AUTH_FAILED) -> OctopError:
    return OctopError.localized(code)


@dataclass(frozen=True)
class Principal:
    user: IdentityUser
    session: IdentitySession


class CloudIdentity:
    def __init__(
        self, repo: CloudIdentityRepo, secret_repo: SecretRepo, *, cloud_id: str, timezone: str
    ) -> None:
        self.repo = repo
        self.cipher: Fernet = get_fernet(secret_repo, PURPOSE_MFA)
        self.cloud_id = cloud_id
        self.timezone = timezone
        # Equal-cost password verification also for nonexistent users.
        self._dummy_hash = hash_password(secrets.token_urlsafe(32))

    def _view(self, tx: IdentityTransaction, user: IdentityUser) -> UserView:
        mfa = tx.mfa(user.user_id)
        return UserView(
            user_id=user.user_id,
            username=user.username,
            role=cast(Literal["admin", "user"], user.role),
            enabled=bool(user.enabled),
            mfa_enabled=mfa is not None and mfa.secret_enc is not None,
        )

    def _identity(self, tx: IdentityTransaction, principal: Principal) -> IdentityView:
        return IdentityView(
            cloud_id=self.cloud_id,
            user=self._view(tx, principal.user),
            device_id=principal.session.device_id,
            binding_generation=principal.session.generation,
            timezone=self.timezone,
        )

    def _issue(
        self,
        tx: IdentityTransaction,
        user: IdentityUser,
        kind: SessionKind,
        *,
        device: IdentityDevice | None = None,
    ) -> TokenView:
        tx.prune_sessions(user.user_id, int(time.time()))
        token = f"{kind}_" + secrets.token_urlsafe(32)
        ttl = ACCOUNT_TTL if kind == "account" else DEVICE_TTL
        session = IdentitySession(
            digest(token),
            user.user_id,
            kind,
            user.security_version,
            device.device_id if device else None,
            device.generation if device else None,
            int(time.time()) + ttl,
            0,
        )
        tx.add_session(session)
        return TokenView(
            access_token=token,
            expires_in=ttl,
            identity=self._identity(tx, Principal(user, session)),
        )

    def _principal(
        self, tx: IdentityTransaction, token: str, kind: SessionKind | None = None
    ) -> Principal:
        # First locate the owner, then lock and re-read: revocation may have run
        # while the row lock was being acquired (especially on PostgreSQL).
        preliminary = tx.session(digest(token))
        if preliminary is None:
            raise fail()
        user = tx.user(user_id=preliminary.user_id)
        session = tx.session(digest(token))
        if (
            user is None
            or session is None
            or session.expires_at <= int(time.time())
            or session.security_version != user.security_version
        ):
            raise fail()
        if not user.enabled:
            raise fail(ErrorCode.USER_DISABLED)
        if (kind is not None and session.kind != kind) or (
            kind is None and session.kind == "challenge"
        ):
            raise fail()
        if session.kind == "device":
            device = tx.device(user.user_id)
            if (
                not device
                or device.revoked
                or device.device_id != session.device_id
                or device.generation != session.generation
                or user.binding_generation != device.generation
                or device.refresh_expires_at <= int(time.time())
            ):
                raise fail(ErrorCode.DEVICE_REVOKED)
        return Principal(user, session)

    @contextmanager
    def authorized_device(self, token: str) -> Iterator[tuple[IdentityTransaction, Principal]]:
        """Hold live binding authority through a model reservation/sync commit.

        Future P3/P5 writes must use this transaction, so replacement cannot
        race between checking generation and committing privileged work.
        """
        with self.repo.transaction() as tx:
            yield tx, self._principal(tx, token, "device")

    @contextmanager
    def authorized_session(self, token: str) -> Iterator[tuple[IdentityTransaction, Principal]]:
        """Read authority for account or device, excluding MFA challenges."""
        with self.repo.transaction() as tx:
            yield tx, self._principal(tx, token)

    @contextmanager
    def authorized_account(
        self, token: str, *, admin: bool = False
    ) -> Iterator[tuple[IdentityTransaction, Principal]]:
        """Transaction-scoped account authorization for independent cloud services."""
        with self.repo.transaction() as tx:
            if admin:
                tx.lock_bootstrap()
            yield tx, self._admin(tx, token) if admin else self._principal(tx, token, "account")

    def authenticate(self, token: str, *, kind: SessionKind | None = None) -> IdentityView:
        with self.repo.transaction() as tx:
            return self._identity(tx, self._principal(tx, token, kind))

    def _admin(self, tx: IdentityTransaction, token: str) -> Principal:
        principal = self._principal(tx, token, "account")
        if principal.user.role != "admin":
            raise fail(ErrorCode.FORBIDDEN)
        return principal

    def bootstrap(self, username: str, password: str) -> UserView:
        validate_password_policy(password)
        hashed = hash_password(password)
        # Serialize the empty-DB check across workers via the persistent key.
        with self.repo.transaction() as tx:
            tx.lock_bootstrap()
            if tx.users():
                raise fail(ErrorCode.DATABASE_NOT_EMPTY)
            return self._view(tx, tx.create_user(username.lower(), hashed, "admin"))

    def create_user(self, token: str, username: str, password: str, role: str) -> UserView:
        validate_password_policy(password)
        with self.repo.transaction() as tx:
            tx.lock_bootstrap()
            self._admin(tx, token)
            if tx.user(username=username.lower()):
                raise fail(ErrorCode.USERNAME_TAKEN)
            return self._view(tx, tx.create_user(username.lower(), hash_password(password), role))

    def users(self, token: str) -> list[UserView]:
        with self.repo.transaction() as tx:
            self._admin(tx, token)
            return [self._view(tx, user) for user in tx.users()]

    def update_user(self, token: str, user_id: int, *, enabled: bool, role: str) -> UserView:
        with self.repo.transaction() as tx:
            tx.lock_bootstrap()
            admin = self._admin(tx, token)
            user = tx.user(user_id=user_id)
            if user is None:
                raise fail(ErrorCode.NOT_FOUND)
            if user_id == admin.user.user_id and (not enabled or role != "admin"):
                raise fail(ErrorCode.FORBIDDEN)
            tx.update_user(user_id, enabled=enabled, role=role, password_hash=user.password_hash)
            updated = tx.user(user_id=user_id)
            assert updated
            return self._view(tx, updated)

    def login(self, username: str, password: str) -> LoginView:
        key = digest(username.lower())
        result: LoginView | None = None
        error = ErrorCode.AUTH_FAILED
        with self.repo.transaction() as tx:
            attempts = tx.login_attempt(key, int(time.time()))
            if attempts >= 5:
                error = ErrorCode.LOGIN_LOCKED
            else:
                user = tx.user(username=username.lower())
                valid = verify_password(password, user.password_hash if user else self._dummy_hash)
                if valid and user:
                    if not user.enabled:
                        error = ErrorCode.USER_DISABLED
                    else:
                        tx.clear_attempts(key)
                        mfa = tx.mfa(user.user_id)
                        if mfa and mfa.secret_enc:
                            tx.prune_sessions(user.user_id, int(time.time()))
                            token = "challenge_" + secrets.token_urlsafe(32)
                            tx.add_session(
                                IdentitySession(
                                    digest(token),
                                    user.user_id,
                                    "challenge",
                                    user.security_version,
                                    None,
                                    None,
                                    int(time.time()) + CHALLENGE_TTL,
                                    0,
                                )
                            )
                            result = LoginView(mfa_token=token)
                        else:
                            result = LoginView(session=self._issue(tx, user, "account"))
        # Failed-attempt updates must commit before the HTTP error is raised.
        if result is None:
            raise fail(error)
        return result

    def _factor(self, tx: IdentityTransaction, user_id: int, code: str) -> bool:
        mfa = tx.mfa(user_id)
        if not mfa or not mfa.secret_enc:
            return False
        try:
            secret = self.cipher.decrypt(bytes(mfa.secret_enc)).decode()
        except (InvalidToken, ValueError, UnicodeError):
            return False
        step = verify_code(secret, code, last_used_step=mfa.last_step)
        if step is not None:
            tx.use_step(user_id, step)
            return True
        needle = normalize_code(code)
        if len(needle) == 8:
            for code_id, hashed in tx.recovery_codes(user_id):
                if verify_password(needle, hashed):
                    tx.use_recovery(code_id)
                    return True
        return False

    def verify_mfa(self, token: str, code: str) -> TokenView:
        result = None
        locked = False
        with self.repo.transaction() as tx:
            principal = self._principal(tx, token, "challenge")
            key = digest("mfa:" + str(principal.user.user_id))
            locked = tx.login_attempt(key, int(time.time())) >= 5
            if not locked and principal.session.attempts < 5:
                tx.challenge_attempt(principal.session.token_hash)
                if self._factor(tx, principal.user.user_id, code):
                    tx.clear_attempts(key)
                    tx.consume_session(principal.session.token_hash)
                    result = self._issue(tx, principal.user, "account")
        if result is None:
            raise fail(ErrorCode.MFA_LOCKED if locked else ErrorCode.MFA_INVALID)
        return result

    def begin_mfa(self, token: str) -> MfaEnroll:
        with self.repo.transaction() as tx:
            principal = self._principal(tx, token, "account")
            mfa = tx.mfa(principal.user.user_id)
            if mfa and mfa.secret_enc:
                raise fail(ErrorCode.FORBIDDEN)
            secret = generate_secret()
            tx.pending_mfa(
                principal.user.user_id, self.cipher.encrypt(secret.encode()), int(time.time()) + 600
            )
            return MfaEnroll(
                secret=secret,
                otpauth_uri=otpauth_uri(secret=secret, username=principal.user.username),
            )

    def confirm_mfa(self, token: str, code: str) -> RecoveryView:
        result = None
        with self.repo.transaction() as tx:
            user = self._principal(tx, token, "account").user
            mfa = tx.mfa(user.user_id)
            if (
                mfa
                and not mfa.secret_enc
                and mfa.pending_enc
                and mfa.pending_expires_at
                and mfa.pending_expires_at > int(time.time())
                and mfa.pending_attempts < 5
            ):
                tx.pending_attempt(user.user_id)
                secret = self.cipher.decrypt(bytes(mfa.pending_enc)).decode()
                if verify_code(secret, code, last_used_step=None) is not None:
                    codes = generate_codes()
                    tx.enable_mfa(user.user_id)
                    tx.set_recovery(user.user_id, [hash_password(normalize_code(c)) for c in codes])
                    tx.update_user(
                        user.user_id, enabled=True, role=user.role, password_hash=user.password_hash
                    )
                    result = RecoveryView(codes=codes)
        if result is None:
            raise fail(ErrorCode.MFA_INVALID)
        return result

    def _password_proof(
        self, tx: IdentityTransaction, user: IdentityUser, password: str, code: str | None
    ) -> bool:
        key = digest("proof:" + str(user.user_id))
        if tx.login_attempt(key, int(time.time())) >= 5:
            return False
        if not verify_password(password, user.password_hash):
            return False
        mfa = tx.mfa(user.user_id)
        if mfa and mfa.secret_enc and not self._factor(tx, user.user_id, code or ""):
            return False
        tx.clear_attempts(key)
        return True

    def disable_mfa(self, token: str, password: str, code: str) -> None:
        valid = False
        with self.repo.transaction() as tx:
            user = self._principal(tx, token, "account").user
            valid = self._password_proof(tx, user, password, code)
            if valid:
                tx.disable_mfa(user.user_id)
                tx.update_user(
                    user.user_id, enabled=True, role=user.role, password_hash=user.password_hash
                )
        if not valid:
            raise fail(ErrorCode.MFA_INVALID)

    def change_password(
        self, token: str, password: str, new_password: str, code: str | None
    ) -> None:
        validate_password_policy(new_password, old_password=password)
        valid = False
        with self.repo.transaction() as tx:
            user = self._principal(tx, token, "account").user
            valid = self._password_proof(tx, user, password, code)
            if valid:
                tx.update_user(
                    user.user_id,
                    enabled=True,
                    role=user.role,
                    password_hash=hash_password(new_password),
                )
        if not valid:
            raise fail()

    def bind(self, token: str, label: str, replace: bool) -> DeviceGrant:
        with self.repo.transaction() as tx:
            user = self._principal(tx, token, "account").user
            previous = tx.device(user.user_id)
            if (
                previous
                and not previous.revoked
                and previous.refresh_expires_at > int(time.time())
                and not replace
            ):
                raise fail(ErrorCode.DEVICE_BINDING_EXISTS)
            generation = tx.advance_binding(user.user_id)
            refresh = "refresh_" + secrets.token_urlsafe(32)
            now = int(time.time())
            device = IdentityDevice(
                new_ulid(),
                user.user_id,
                label,
                generation,
                digest(refresh),
                now + REFRESH_TTL,
                0,
                now,
            )
            tx.put_device(device)
            return DeviceGrant(
                session=self._issue(tx, user, "device", device=device),
                refresh_token=refresh,
                refresh_expires_in=REFRESH_TTL,
            )

    def refresh(self, refresh: str) -> TokenView:
        with self.repo.transaction() as tx:
            user_id = tx.refresh_user(digest(refresh))
            user = tx.user(user_id=user_id) if user_id is not None else None
            device = tx.device(user_id) if user_id is not None else None
            if (
                not user
                or not device
                or not secrets.compare_digest(device.refresh_hash, digest(refresh))
                or device.revoked
                or device.generation != user.binding_generation
                or device.refresh_expires_at <= int(time.time())
            ):
                raise fail(ErrorCode.DEVICE_REVOKED)
            if not user.enabled:
                raise fail(ErrorCode.USER_DISABLED)
            tx.seen(user.user_id, int(time.time()))
            return self._issue(tx, user, "device", device=device)

    def devices(self, token: str) -> list[DeviceView]:
        with self.repo.transaction() as tx:
            user = self._principal(tx, token, "account").user
            d = tx.device(user.user_id)
            return (
                [
                    DeviceView(
                        device_id=d.device_id,
                        label=d.label,
                        binding_generation=d.generation,
                        revoked=bool(d.revoked),
                        last_seen_at=d.last_seen_at,
                    )
                ]
                if d
                else []
            )

    def revoke(self, token: str, device_id: str) -> None:
        with self.repo.transaction() as tx:
            principal = self._principal(tx, token)
            device = tx.device(principal.user.user_id)
            if not device or device.device_id != device_id:
                raise fail(ErrorCode.NOT_FOUND)
            tx.revoke_device(principal.user.user_id)

    def logout(self, token: str) -> None:
        with self.repo.transaction() as tx:
            principal = self._principal(tx, token)
            if principal.session.kind == "device":
                tx.revoke_device(principal.user.user_id)
            else:
                tx.consume_session(principal.session.token_hash)
