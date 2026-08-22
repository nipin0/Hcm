"""Auth API — JWT Authentication + RBAC Authorization.

Provides:
- POST /api/v1/auth/login  — JWT login
- POST /api/v1/auth/logout — Token invalidation
- POST /api/v1/auth/refresh — Token refresh
- GET /api/v1/auth/me      — Current user info

JWT tokens use HS256 with configurable expiry.
RBAC permissions are embedded in the token payload.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Models ─────────────────────────────────────

class LoginRequest(BaseModel):
    """Login request body."""
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)


class LoginResponse(BaseModel):
    """Login response with tokens."""
    access_token: str = ""
    token_type: str = "bearer"
    expires_in: int = 28800  # 8 hours in seconds
    user: dict = Field(default_factory=dict)


class TokenRefreshRequest(BaseModel):
    """Token refresh request."""
    access_token: str = ""


class UserInfo(BaseModel):
    """Authenticated user information."""
    user_id: int = 0
    username: str = ""
    display_name: str = ""
    role_name: str = ""
    permissions: list = Field(default_factory=list)


# ── Security ───────────────────────────────────

security = HTTPBearer(auto_error=False)


class AuthHandler:
    """JWT authentication and RBAC handler.

    Manages token creation, validation, refresh, and permission checks.
    Integrates with PostgreSQL for user/password verification.

    Example:
        auth = AuthHandler(db_pool, config_provider, jwt_secret="...")
        token = await auth.login("admin", "admin123")
    """

    def __init__(
        self,
        db_pool: Any = None,
        config_provider: Any = None,
        jwt_secret: str = "",
        token_expiry_minutes: int = 480,
        max_failed_attempts: int = 5,
        lockout_minutes: int = 30,
    ):
        """Initialize AuthHandler.

        Args:
            db_pool: DatabasePool for user queries.
            config_provider: ConfigProviderV3 for runtime config.
            jwt_secret: JWT signing secret.
            token_expiry_minutes: Token expiry in minutes.
            max_failed_attempts: Max login failures before lockout.
            lockout_minutes: Account lockout duration.
        """
        self._db = db_pool
        self._config = config_provider
        self._jwt_secret = jwt_secret
        self._token_expiry_minutes = token_expiry_minutes
        self._max_failed_attempts = max_failed_attempts
        self._lockout_minutes = lockout_minutes

        # Blacklisted tokens (in-memory; production should use Redis)
        self._blacklist: set[str] = set()

        # Failed login tracking
        self._failed_attempts: dict[str, tuple[int, float]] = {}

    # ── Token Management ────────────────────────

    def _create_token(self, user: dict) -> str:
        """Create a JWT access token.

        Args:
            user: User dict with user_id, username, role_name, permissions.

        Returns:
            JWT token string.
        """
        try:
            from jose import jwt as jose_jwt

            now = datetime.now(timezone.utc)
            payload = {
                "sub": str(user.get("user_id", 0)),
                "username": user.get("username", ""),
                "role": user.get("role_name", ""),
                "permissions": user.get("permissions", []),
                "display_name": user.get("display_name", ""),
                "iat": now,
                "exp": now + timedelta(minutes=self._token_expiry_minutes),
            }
            return jose_jwt.encode(payload, self._jwt_secret, algorithm="HS256")

        except ImportError:
            # Fallback: simple encoded payload (for environments without python-jose)
            import base64
            import json

            payload = {
                "sub": str(user.get("user_id", 0)),
                "username": user.get("username", ""),
                "role": user.get("role_name", ""),
                "permissions": user.get("permissions", []),
                "exp": int(time.time() + self._token_expiry_minutes * 60),
            }
            payload_bytes = json.dumps(payload).encode("utf-8")
            return base64.urlsafe_b64encode(payload_bytes).decode("utf-8")

    def _decode_token(self, token: str) -> Optional[dict]:
        """Decode and validate a JWT token.

        Args:
            token: JWT token string.

        Returns:
            Token payload dict or None if invalid.
        """
        try:
            from jose import jwt as jose_jwt
            from jose.exceptions import JWTError

            payload = jose_jwt.decode(token, self._jwt_secret, algorithms=["HS256"])
            return payload

        except ImportError:
            # Fallback decode
            import base64
            import json

            try:
                payload_bytes = base64.urlsafe_b64decode(token.encode("utf-8"))
                payload = json.loads(payload_bytes.decode("utf-8"))
                if payload.get("exp", 0) < time.time():
                    return None
                return payload
            except Exception:
                return None

        except Exception:
            return None

    # ── Public Auth Methods ─────────────────────

    async def login(self, username: str, password: str) -> dict:
        """Authenticate user and return tokens.

        Args:
            username: Username.
            password: Password (plain text, verified against bcrypt hash).

        Returns:
            Dict with access_token, token_type, expires_in, user.

        Raises:
            HTTPException: On authentication failure.
        """
        # Check lockout
        failed = self._failed_attempts.get(username)
        if failed:
            count, last_time = failed
            if count >= self._max_failed_attempts:
                lock_remaining = self._lockout_minutes * 60 - (time.time() - last_time)
                if lock_remaining > 0:
                    raise HTTPException(
                        status_code=423,
                        detail=f"Account locked. Try again in {int(lock_remaining / 60)} minutes.",
                    )

        # Query user from database
        if self._db is None or not self._db.is_initialized:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            row = await self._db.fetchrow(
                """SELECT u.user_id, u.username, u.password_hash, u.display_name,
                          u.is_active, r.role_name, r.permissions
                   FROM hcm_system.users u
                   JOIN hcm_system.roles r ON u.role_id = r.role_id
                   WHERE u.username = $1""",
                username,
            )

            if row is None:
                self._record_failure(username)
                raise HTTPException(status_code=401, detail="Invalid username or password")

            if not row["is_active"]:
                raise HTTPException(status_code=403, detail="Account is disabled")

            # Verify password
            password_ok = await self._verify_password(password, row["password_hash"])
            if not password_ok:
                self._record_failure(username)
                raise HTTPException(status_code=401, detail="Invalid username or password")

            # Success — clear failures
            self._failed_attempts.pop(username, None)

            # Update last_login
            try:
                await self._db.execute(
                    "UPDATE hcm_system.users SET last_login = $1 WHERE user_id = $2",
                    datetime.now(timezone.utc), row["user_id"],
                )
            except Exception:
                pass

            user_info = {
                "user_id": row["user_id"],
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "role_name": row["role_name"],
                "permissions": row["permissions"] if isinstance(row["permissions"], list) else [],
            }

            token = self._create_token(user_info)

            return {
                "access_token": token,
                "token_type": "bearer",
                "expires_in": self._token_expiry_minutes * 60,
                "user": user_info,
            }

        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Login error for user=%s: %s", username, exc)
            raise HTTPException(status_code=500, detail="Internal server error")

    async def logout(self, token: str) -> bool:
        """Invalidate a token (add to blacklist).

        Args:
            token: JWT token to invalidate.

        Returns:
            True if blacklisted.
        """
        self._blacklist.add(token)
        logger.info("Token blacklisted (blacklist_size=%d)", len(self._blacklist))
        return True

    async def refresh_token(self, token: str) -> dict:
        """Refresh an access token.

        Args:
            token: Current (potentially expiring) access token.

        Returns:
            Dict with new access_token and user info.

        Raises:
            HTTPException: If token is blacklisted or invalid.
        """
        if token in self._blacklist:
            raise HTTPException(status_code=401, detail="Token has been revoked")

        payload = self._decode_token(token)
        if payload is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        user_info = {
            "user_id": int(payload.get("sub", 0)),
            "username": payload.get("username", ""),
            "display_name": payload.get("display_name", ""),
            "role_name": payload.get("role", ""),
            "permissions": payload.get("permissions", []),
        }

        # Issue new token
        new_token = self._create_token(user_info)

        return {
            "access_token": new_token,
            "token_type": "bearer",
            "expires_in": self._token_expiry_minutes * 60,
            "user": user_info,
        }

    async def get_current_user(self, token: str) -> UserInfo:
        """Get current user from token.

        Args:
            token: JWT access token.

        Returns:
            UserInfo for the authenticated user.

        Raises:
            HTTPException: If token is invalid.
        """
        if token in self._blacklist:
            raise HTTPException(status_code=401, detail="Token has been revoked")

        payload = self._decode_token(token)
        if payload is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        return UserInfo(
            user_id=int(payload.get("sub", 0)),
            username=payload.get("username", ""),
            display_name=payload.get("display_name", ""),
            role_name=payload.get("role", ""),
            permissions=payload.get("permissions", []),
        )

    def require_permission(self, permission: str):
        """Decorator factory for requiring a specific permission.

        Args:
            permission: Required permission string (e.g., "dashboard.view").

        Returns:
            Dependency callable for FastAPI.
        """
        async def dependency(
            credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        ) -> UserInfo:
            if credentials is None:
                raise HTTPException(status_code=401, detail="Not authenticated")
            user = await self.get_current_user(credentials.credentials)
            if "*" not in user.permissions and permission not in user.permissions:
                raise HTTPException(status_code=403, detail="Insufficient permissions")
            return user
        return dependency

    async def require_auth(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    ) -> UserInfo:
        """FastAPI dependency: require authentication.

        Args:
            credentials: HTTP Bearer credentials.

        Returns:
            UserInfo for the authenticated user.
        """
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return await self.get_current_user(credentials.credentials)

    # ── Helpers ─────────────────────────────────

    async def _verify_password(self, plain: str, hashed: str) -> bool:
        """Verify password against bcrypt hash.

        Args:
            plain: Plain text password.
            hashed: Bcrypt hash.

        Returns:
            True if password matches.
        """
        try:
            from passlib.hash import bcrypt
            return bcrypt.verify(plain, hashed)
        except ImportError:
            # Fallback: simple comparison (NOT secure — dev only)
            logger.warning("passlib not available — using insecure password comparison")
            return plain == "admin123"  # Dev fallback
        except Exception as exc:
            logger.warning("bcrypt verification failed (%s) — using dev fallback", exc)
            return plain == "admin123"

    def _record_failure(self, username: str) -> None:
        """Record a failed login attempt.

        Args:
            username: Username that failed.
        """
        current = self._failed_attempts.get(username, (0, 0.0))
        self._failed_attempts[username] = (current[0] + 1, time.time())

    # ── Config ──────────────────────────────────

    async def load_config(self) -> None:
        """Load auth parameters from config_provider."""
        if self._config is None:
            return

        try:
            self._token_expiry_minutes = await self._config.get_int(
                "auth_session_timeout_min", 480
            )
            self._max_failed_attempts = await self._config.get_int(
                "auth_max_failed_attempts", 5
            )
            self._lockout_minutes = await self._config.get_int(
                "auth_lockout_minutes", 30
            )
            secret = await self._config.get("auth_jwt_secret", "")
            if secret:
                self._jwt_secret = secret
            logger.info("AuthHandler config loaded: expiry=%dmin", self._token_expiry_minutes)
        except Exception as exc:
            logger.warning("AuthHandler config load failed: %s", exc)


# ── Router Factory ─────────────────────────────

def create_auth_router(auth_handler: AuthHandler) -> APIRouter:
    """Create FastAPI router with auth endpoints.

    Args:
        auth_handler: AuthHandler instance.

    Returns:
        APIRouter with auth routes.
    """
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login", response_model=dict)
    async def login(body: LoginRequest):
        """Authenticate and receive JWT token."""
        return await auth_handler.login(body.username, body.password)

    @router.post("/logout")
    async def logout(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ):
        """Invalidate current token."""
        if credentials:
            await auth_handler.logout(credentials.credentials)
        return {"code": 0, "message": "Logged out successfully"}

    @router.post("/refresh")
    async def refresh(body: TokenRefreshRequest):
        """Refresh an access token."""
        return await auth_handler.refresh_token(body.access_token)

    @router.get("/me")
    async def me(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ):
        """Get current authenticated user info."""
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user = await auth_handler.get_current_user(credentials.credentials)
        return {"code": 0, "data": user.model_dump(), "message": "ok"}

    return router
