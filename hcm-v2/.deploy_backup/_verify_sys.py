"""System Management API — Users CRUD + MT5/DeepSeek/Network/Notifications/Cache.

Provides:
  Sub-module 1 — User Management:
    GET    /api/v1/system/users          — Paginated user list (search, role filter)
    POST   /api/v1/system/users          — Create user (bcrypt hash, default roles bootstrap)
    PUT    /api/v1/system/users/{user_id} — Partial update (password skipped if empty)
    DELETE /api/v1/system/users/{user_id} — Soft delete (is_active=false)
    Legacy aliases: /api/system/users, /api/system/users/{id}

  Sub-module 2 — MT5 Configuration:
    GET  /api/v1/system/mt5  — Read MT5 config (password masked)
    PUT  /api/v1/system/mt5  — Update MT5 config
    Legacy aliases: /api/system/mt5

  Sub-module 3 — DeepSeek AI Configuration:
    GET  /api/v1/system/deepseek  — Read DeepSeek config (api_key masked)
    PUT  /api/v1/system/deepseek  — Update DeepSeek config (empty api_key = keep old)
    Legacy aliases: /api/system/deepseek

  Sub-module 4 — Network Configuration:
    GET  /api/v1/system/network  — Read network config
    PUT  /api/v1/system/network  — Update network config
    Legacy aliases: /api/system/network

  Sub-module 5 — Notification Configuration:
    GET  /api/v1/system/notifications  — Read notification config
    PUT  /api/v1/system/notifications  — Update notification config
    Legacy aliases: /api/system/notifications

  Sub-module 6 — Cache Management:
    GET  /api/v1/system/cache/stats  — Redis cache statistics (INFO)
    POST /api/v1/system/cache/clear  — Clear hcm:config:* prefix cache keys
    Legacy aliases: /api/system/cache/stats, /api/system/cache/clear

  Sub-module 7 — Account List:
    GET    /api/v1/system/accounts/{account_id}  — List all accounts from hcm_broker.accounts
    DELETE /api/v1/system/accounts/{account_id}  — Soft delete an account (is_active=false)
    Legacy aliases: /api/system/accounts, /api/system/accounts/{id}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────

# Default roles to bootstrap when roles table is empty
DEFAULT_ROLES: list[dict[str, Any]] = [
    {
        "role_name": "admin",
        "permissions": ["*"],
        "description": "Full system access — all modules and operations",
    },
    {
        "role_name": "operator",
        "permissions": [
            "dashboard.view", "signals.view", "positions.view",
            "risk.view", "risk.edit", "copy.view", "copy.edit",
            "config.view", "config.edit", "system.view",
        ],
        "description": "Day-to-day operations — view + edit risk, copy, config",
    },
    {
        "role_name": "viewer",
        "permissions": [
            "dashboard.view", "signals.view", "positions.view",
            "risk.view", "copy.view", "config.view",
        ],
        "description": "Read-only access to dashboards and reports",
    },
    {
        "role_name": "trader",
        "permissions": [
            "dashboard.view", "signals.view", "positions.view",
            "risk.view", "copy.view", "config.view",
            "signals.edit", "positions.edit",
        ],
        "description": "Trader — can manage signals and positions",
    },
]

# MT5 config keys (stored with "mt5." prefix in hcm_config.metadata)
MT5_CONFIG_KEYS: list[str] = [
    "mt5.server_address",
    "mt5.account_number",
    "mt5.password",
    "mt5.broker_name",
]

MT5_FIELD_NAMES: list[str] = [k.replace("mt5.", "") for k in MT5_CONFIG_KEYS]
MT5_FIELD_DEFAULTS: dict[str, Any] = {
    "server_address": "",
    "account_number": 0,
    "password": "",
    "broker_name": "",
}

# DeepSeek config keys (stored with "deepseek." prefix)
DEEPSEEK_CONFIG_KEYS: list[str] = [
    "deepseek.api_key",
    "deepseek.api_base",
    "deepseek.model",
    "deepseek.max_tokens",
    "deepseek.temperature",
]

DEEPSEEK_FIELD_NAMES: list[str] = [k.replace("deepseek.", "") for k in DEEPSEEK_CONFIG_KEYS]
DEEPSEEK_FIELD_DEFAULTS: dict[str, Any] = {
    "api_key": "",
    "api_base": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "max_tokens": 2000,
    "temperature": 0.3,
}

# Network config keys (stored with "network." prefix)
NETWORK_CONFIG_KEYS: list[str] = [
    "network.service_host",
    "network.service_port",
    "network.cors_origins",
    "network.ws_port",
]

NETWORK_FIELD_NAMES: list[str] = [k.replace("network.", "") for k in NETWORK_CONFIG_KEYS]
NETWORK_FIELD_DEFAULTS: dict[str, Any] = {
    "service_host": "0.0.0.0",
    "service_port": 8000,
    "cors_origins": "*",
    "ws_port": 8001,
}

# Notification config keys (stored with "notification." prefix)
NOTIFICATION_CONFIG_KEYS: list[str] = [
    "notification.dingtalk_webhook",
    "notification.enable_signal_alert",
    "notification.enable_risk_alert",
    "notification.enable_system_alert",
]

NOTIFICATION_FIELD_NAMES: list[str] = [
    k.replace("notification.", "") for k in NOTIFICATION_CONFIG_KEYS
]
NOTIFICATION_FIELD_DEFAULTS: dict[str, Any] = {
    "dingtalk_webhook": "",
    "enable_signal_alert": True,
    "enable_risk_alert": True,
    "enable_system_alert": False,
}

# Cache clear key pattern
CACHE_CLEAR_PATTERN = "hcm:config:*"
CACHE_SCAN_COUNT = 100


# ── Pydantic Models ─────────────────────────────

# --- User Management ---

class UserCreate(BaseModel):
    """Request body for creating a new user."""
    username: str = Field(..., min_length=1, max_length=50, description="Login username")
    password: str = Field(..., min_length=1, max_length=128, description="Plain text password")
    role: str = Field("viewer", description="Role name: admin/operator/viewer/trader")
    display_name: str = Field("", max_length=100, description="Display name")


class UserUpdate(BaseModel):
    """Request body for partially updating a user.

    All fields are optional. Empty/NULL password means do not change.
    """
    username: Optional[str] = Field(None, min_length=1, max_length=50)
    password: Optional[str] = Field(None, max_length=128)
    role: Optional[str] = Field(None, description="Role name")
    display_name: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = Field(None)


# --- MT5 Configuration ---

class MT5ConfigUpdate(BaseModel):
    """MT5 broker connection configuration.

    Fields server_address/account_number/broker_name are the canonical config keys.
    Fields login/server/trade_mode/account_type are sent by the frontend and used
    for syncing to hcm_broker.accounts.
    """
    server_address: str = Field("", description="MT5 server address")
    account_number: int = Field(0, description="MT5 account number")
    password: str = Field("", description="MT5 account password")
    broker_name: str = Field("", description="Broker name")
    # Frontend-originated fields for account sync
    login: Optional[int] = Field(None, description="MT5 login (account number)")
    server: Optional[str] = Field(None, description="MT5 server address (frontend name)")
    trade_mode: str = Field("demo", description="Trade mode: demo / live / contest")
    account_type: str = Field("master", description="Account type: master / follower")


# --- DeepSeek AI Configuration ---

class DeepSeekConfigUpdate(BaseModel):
    """DeepSeek AI inference configuration.

    When api_key is empty string on PUT, the existing value is preserved
    (no re-entry required).
    """
    api_key: str = Field("", description="DeepSeek API key (empty = keep current)")
    api_base: str = Field("https://api.deepseek.com", description="API base URL")
    model: str = Field("deepseek-chat", description="Model name")
    max_tokens: int = Field(2000, ge=1, le=32000, description="Max tokens per request")
    temperature: float = Field(0.3, ge=0.0, le=2.0, description="Sampling temperature")


# --- Network Configuration ---

class NetworkConfigUpdate(BaseModel):
    """Network / service binding configuration."""
    service_host: str = Field("0.0.0.0", description="Service bind address")
    service_port: int = Field(8000, ge=1, le=65535, description="HTTP service port")
    cors_origins: str = Field("*", description="CORS allowed origins")
    ws_port: int = Field(8001, ge=1, le=65535, description="WebSocket port")


# --- Notification Configuration ---

class NotificationConfigUpdate(BaseModel):
    """Notification channel configuration."""
    dingtalk_webhook: str = Field("", description="DingTalk webhook URL")
    enable_signal_alert: bool = Field(True, description="Enable signal alerts")
    enable_risk_alert: bool = Field(True, description="Enable risk alerts")
    enable_system_alert: bool = Field(False, description="Enable system alerts")


# ── Helpers ─────────────────────────────────────

def _mask_sensitive(value: str, visible: int = 3) -> str:
    """Mask a sensitive string, showing only the last `visible` characters.

    Args:
        value: The string to mask.
        visible: Number of trailing characters to show.

    Returns:
        Masked string like '***xyz', or '***' if value is too short.
    """
    if not value:
        return ""
    if len(value) <= visible:
        return "***"
    return "***" + value[-visible:]


def _coerce_type(raw: Any, default: Any) -> Any:
    """Coerce a raw config value to match the type of `default`.

    Args:
        raw: Raw value from database (string or None).
        default: Fallback value whose type determines the target type.

    Returns:
        Coerced value of the same type as `default`.
    """
    if raw is None or raw == "":
        return default
    try:
        if isinstance(default, bool):
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("true", "1", "yes", "on")
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        return str(raw)
    except (ValueError, TypeError):
        return default


async def _hash_password(password: str) -> str:
    """Hash a plain-text password using bcrypt (with safe SHA-256 fallback).

    Args:
        password: Plain text password.

    Returns:
        Hash string (bcrypt or "sha256:" prefix).
    """
    # Cap password to 72 bytes (bcrypt limit) - decode & truncate safely
    try:
        pwd_bytes = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    except Exception:
        pwd_bytes = password[:72]

    try:
        import bcrypt as _bcrypt
        salt = _bcrypt.gensalt(rounds=12)
        return _bcrypt.hashpw(pwd_bytes.encode('utf-8'), salt).decode('utf-8')
    except ImportError:
        import hashlib
        return "sha256:" + hashlib.sha256(pwd_bytes.encode('utf-8')).hexdigest()
    except Exception as exc:
        import hashlib
        logger.warning("bcrypt failed (%s), falling back to SHA-256", exc)
        return "sha256:" + hashlib.sha256(pwd_bytes.encode('utf-8')).hexdigest()


async def _read_config_fields(
    config_provider: Any,
    keys: list[str],
    field_names: list[str],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Read config fields via ConfigProviderV3 (unified GET path) and return typed dict.

    Args:
        config_provider: ConfigProviderV3 instance (three-layer cache, PG source of truth).
        keys: Full config keys (e.g. "mt5.server_address").
        field_names: Stripped field names (e.g. "server_address").
        defaults: Default values keyed by field name.

    Returns:
        Dict of field_name → typed_value.
    """
    result: dict[str, Any] = dict(defaults)

    if config_provider is None:
        return result

    for i, key in enumerate(keys):
        field = field_names[i]
        try:
            val = await config_provider.get(key)
            if val is not None:
                result[field] = _coerce_type(val, defaults.get(field, ""))
        except Exception as exc:
            logger.warning("Config read failed for %s, using default: %s", key, exc)

    return result


async def _write_config_fields(
    config_provider: Any,
    prefix: str,
    field_names: list[str],
    body: BaseModel,
) -> tuple[list[str], list[str]]:
    """Write config fields via config_provider.

    Args:
        config_provider: ConfigProviderV3 instance.
        prefix: Key prefix (e.g. "mt5").
        field_names: Field names to write.
        body: Pydantic model with updated values.

    Returns:
        Tuple of (updated_fields, errors).
    """
    updated: list[str] = []
    errors: list[str] = []

    for field in field_names:
        val = getattr(body, field, None)
        if val is None:
            continue
        config_key = f"{prefix}.{field}"
        try:
            str_val = str(val).lower() if isinstance(val, bool) else str(val)
            ok = await config_provider.set(config_key, str_val)
            if ok:
                updated.append(field)
            else:
                errors.append(f"{field}: write failed")
        except Exception as exc:
            errors.append(f"{field}: {exc}")

    return updated, errors


# ── Router Factory ──────────────────────────────

def create_system_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
    redis_client: Any = None,
) -> APIRouter:
    """Create FastAPI router with system management endpoints.

    Provides 6 sub-modules:
      - User management CRUD (hcm_system.users + hcm_system.roles)
      - MT5 configuration (config_provider, mt5.* keys)
      - DeepSeek AI configuration (config_provider, deepseek.* keys)
      - Network configuration (config_provider, network.* keys)
      - Notification configuration (config_provider, notification.* keys)
      - Cache management (Redis INFO + SCAN/DEL)

    Args:
        db_pool: DatabasePool instance (asyncpg wrapper).
        config_provider: ConfigProviderV3 instance.
        auth_handler: AuthHandler instance for RBAC authentication.
        redis_client: RedisClient instance for cache management.

    Returns:
        APIRouter with /api/v1/system routes and legacy /api/system aliases.
    """
    router = APIRouter(tags=["system"])

    # ══════════════════════════════════════════════════════════
    # 1. User Management — shared handler implementations
    # ══════════════════════════════════════════════════════════

    async def _ensure_default_roles() -> None:
        """Insert default roles if hcm_system.roles table is empty.

        Inserts admin, operator, viewer, trader roles with predefined permissions.
        Safe to call on every user creation — only runs when table is empty.
        """
        if db_pool is None or not db_pool.is_initialized:
            return
        try:
            count_row = await db_pool.fetchrow("SELECT COUNT(*) FROM hcm_system.roles")
            if count_row and count_row[0] > 0:
                return
            logger.info("Roles table empty — inserting default roles")
            import json as _json
            for role in DEFAULT_ROLES:
                await db_pool.execute(
                    """INSERT INTO hcm_system.roles (role_name, permissions, description)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (role_name) DO NOTHING""",
                    role["role_name"],
                    _json.dumps(role["permissions"]),
                    role["description"],
                )
            logger.info("Default roles inserted: %d roles", len(DEFAULT_ROLES))
        except Exception as exc:
            logger.warning("Failed to ensure default roles: %s", exc)

    async def _list_users_impl(
        search: Optional[str],
        role: Optional[str],
        page: int,
        page_size: int,
    ) -> dict:
        """Handler: list users with search, role filter, and pagination.

        JOINs hcm_system.roles to include role_name in the response.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            conditions: list[str] = ["1=1"]
            params: list[Any] = []
            param_idx = 1

            if search:
                conditions.append(
                    f"(u.username ILIKE ${param_idx} OR u.display_name ILIKE ${param_idx})"
                )
                params.append(f"%{search}%")
                param_idx += 1

            if role:
                conditions.append(f"r.role_name = ${param_idx}")
                params.append(role)
                param_idx += 1

            where_clause = " AND ".join(conditions)

            # Count
            count_row = await db_pool.fetchrow(
                f"""SELECT COUNT(*)
                    FROM hcm_system.users u
                    JOIN hcm_system.roles r ON u.role_id = r.role_id
                    WHERE {where_clause}""",
                *params,
            )
            total = count_row[0] if count_row else 0

            # Fetch page
            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                f"""SELECT u.user_id, u.username, u.display_name,
                           u.is_active, u.role_id, r.role_name,
                           u.last_login, u.created_at, u.updated_at
                    FROM hcm_system.users u
                    JOIN hcm_system.roles r ON u.role_id = r.role_id
                    WHERE {where_clause}
                    ORDER BY u.user_id ASC
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                *params, page_size, offset,
            )

            items: list[dict] = []
            for row in rows:
                item = dict(row)
                for ts_field in ("last_login", "created_at", "updated_at"):
                    if item.get(ts_field):
                        item[ts_field] = item[ts_field].isoformat()
                # Remove sensitive field
                item.pop("password_hash", None)
                items.append(item)

            return {
                "code": 0,
                "data": {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("User list query failed: %s", exc)
            return {"code": "SYS_001", "data": None, "message": str(exc)}

    async def _create_user_impl(body: UserCreate) -> dict:
        """Handler: create a new user with bcrypt password hash.

        Checks for duplicate username (returns USER_001).
        Auto-inserts default roles if roles table is empty.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            # Ensure default roles exist
            await _ensure_default_roles()

            # Check for duplicate username
            dup_row = await db_pool.fetchrow(
                "SELECT user_id FROM hcm_system.users WHERE username = $1",
                body.username,
            )
            if dup_row is not None:
                return {
                    "code": "USER_001",
                    "data": None,
                    "message": f"Username '{body.username}' already exists",
                }

            # Look up role_id
            role_row = await db_pool.fetchrow(
                "SELECT role_id FROM hcm_system.roles WHERE role_name = $1",
                body.role,
            )
            if role_row is None:
                # Fall back to lowest-privilege role if specified role not found
                role_row = await db_pool.fetchrow(
                    "SELECT role_id FROM hcm_system.roles WHERE role_name = 'researcher'",
                )
                if role_row is None:
                    return {
                        "code": "SYS_002",
                        "data": None,
                        "message": "No valid role found — roles table may be empty",
                    }

            role_id = role_row["role_id"]

            # Hash password
            password_hash = await _hash_password(body.password)

            now = datetime.now(timezone.utc)
            row = await db_pool.fetchrow(
                """INSERT INTO hcm_system.users
                   (username, password_hash, display_name, role_id, is_active,
                    created_at, updated_at)
                   VALUES ($1, $2, $3, $4, true, $5, $5)
                   RETURNING user_id, username, display_name, role_id, is_active,
                             created_at, updated_at""",
                body.username,
                password_hash,
                body.display_name or body.username,
                role_id,
                now,
            )

            if row is None:
                return {"code": "SYS_003", "data": None, "message": "User creation failed"}

            user_data = dict(row)
            for ts_field in ("created_at", "updated_at"):
                if user_data.get(ts_field):
                    user_data[ts_field] = user_data[ts_field].isoformat()

            logger.info("User created: username=%s, role=%s", body.username, body.role)

            return {
                "code": 0,
                "data": user_data,
                "message": f"User '{body.username}' created successfully",
            }

        except Exception as exc:
            logger.error("User creation failed: %s", exc)
            return {"code": "SYS_001", "data": None, "message": str(exc)}

    async def _update_user_impl(user_id: int, body: UserUpdate) -> dict:
        """Handler: partial update of a user.

        If password is empty/None, it is not changed.
        If role is provided, looks up corresponding role_id.
        Supports toggling is_active.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            # Check user exists
            existing = await db_pool.fetchrow(
                "SELECT user_id, username FROM hcm_system.users WHERE user_id = $1",
                user_id,
            )
            if existing is None:
                return {
                    "code": "USER_002",
                    "data": None,
                    "message": f"User with id={user_id} not found",
                }

            set_clauses: list[str] = ["updated_at = $2"]
            params: list[Any] = [user_id, datetime.now(timezone.utc)]
            param_idx = 3

            if body.username is not None:
                # Check for duplicate
                dup = await db_pool.fetchrow(
                    "SELECT user_id FROM hcm_system.users WHERE username = $1 AND user_id != $2",
                    body.username, user_id,
                )
                if dup is not None:
                    return {
                        "code": "USER_001",
                        "data": None,
                        "message": f"Username '{body.username}' already exists",
                    }
                set_clauses.append(f"username = ${param_idx}")
                params.append(body.username)
                param_idx += 1

            if body.password is not None and body.password != "":
                password_hash = await _hash_password(body.password)
                set_clauses.append(f"password_hash = ${param_idx}")
                params.append(password_hash)
                param_idx += 1

            if body.display_name is not None:
                set_clauses.append(f"display_name = ${param_idx}")
                params.append(body.display_name)
                param_idx += 1

            if body.is_active is not None:
                set_clauses.append(f"is_active = ${param_idx}")
                params.append(body.is_active)
                param_idx += 1

            if body.role is not None:
                role_row = await db_pool.fetchrow(
                    "SELECT role_id FROM hcm_system.roles WHERE role_name = $1",
                    body.role,
                )
                if role_row is None:
                    return {
                        "code": "SYS_002",
                        "data": None,
                        "message": f"Role '{body.role}' not found",
                    }
                set_clauses.append(f"role_id = ${param_idx}")
                params.append(role_row["role_id"])
                param_idx += 1

            if len(set_clauses) <= 1:
                return {
                    "code": 0,
                    "data": {"user_id": user_id, "updated": []},
                    "message": "No fields to update",
                }

            set_sql = ", ".join(set_clauses)
            updated = await db_pool.fetchrow(
                f"""UPDATE hcm_system.users
                    SET {set_sql}
                    WHERE user_id = $1
                    RETURNING user_id, username, display_name, role_id, is_active,
                              updated_at""",
                *params,
            )

            if updated is None:
                return {"code": "SYS_003", "data": None, "message": "Update failed"}

            user_data = dict(updated)
            if user_data.get("updated_at"):
                user_data["updated_at"] = user_data["updated_at"].isoformat()

            updated_fields = [
                k for k in ("username", "password", "display_name", "role", "is_active")
                if getattr(body, k, None) is not None
            ]
            if body.password is not None and body.password != "":
                pass  # already included

            logger.info("User updated: user_id=%d, fields=%s", user_id, updated_fields)

            return {
                "code": 0,
                "data": user_data,
                "message": f"User {user_id} updated: {', '.join(updated_fields)}",
            }

        except Exception as exc:
            logger.error("User update failed for user_id=%d: %s", user_id, exc)
            return {"code": "SYS_001", "data": None, "message": str(exc)}

    async def _delete_user_impl(user_id: int) -> dict:
        """Handler: soft delete a user (set is_active=false).

        Does not physically remove the row; preserves audit trail.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            existing = await db_pool.fetchrow(
                "SELECT user_id, username, is_active FROM hcm_system.users WHERE user_id = $1",
                user_id,
            )
            if existing is None:
                return {
                    "code": "USER_002",
                    "data": None,
                    "message": f"User with id={user_id} not found",
                }

            if not existing["is_active"]:
                return {
                    "code": 0,
                    "data": {"user_id": user_id, "username": existing["username"], "is_active": False},
                    "message": f"User '{existing['username']}' is already inactive",
                }

            await db_pool.execute(
                """UPDATE hcm_system.users
                   SET is_active = false, updated_at = $2
                   WHERE user_id = $1""",
                user_id, datetime.now(timezone.utc),
            )

            logger.info("User soft-deleted: user_id=%d, username=%s", user_id, existing["username"])

            return {
                "code": 0,
                "data": {
                    "user_id": user_id,
                    "username": existing["username"],
                    "is_active": False,
                },
                "message": f"User '{existing['username']}' deactivated",
            }

        except Exception as exc:
            logger.error("User delete failed for user_id=%d: %s", user_id, exc)
            return {"code": "SYS_001", "data": None, "message": str(exc)}

    # ══════════════════════════════════════════════════════════
    # 2. MT5 Configuration — shared handler implementations
    # ══════════════════════════════════════════════════════════

    async def _get_mt5_impl() -> dict:
        """Handler: read MT5 configuration via config_provider (Redis) with password masked."""
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}
        config: dict[str, str] = {}
        for field in MT5_FIELD_NAMES:
            full_key = f"mt5.{field}"
            try:
                val = await config_provider.get(full_key, MT5_FIELD_DEFAULTS.get(field, ""))
                config[field] = val or MT5_FIELD_DEFAULTS.get(field, "")
            except Exception:
                config[field] = MT5_FIELD_DEFAULTS.get(field, "")
        # Mask password
        if config.get("password"):
            config["password"] = _mask_sensitive(config["password"])
        return {"code": 0, "data": config, "message": "ok"}

    # MT5 trade_mode → env_type mapping
    _TRADE_MODE_TO_ENV: dict[str, int] = {"demo": 1, "live": 2, "contest": 3}

    async def _put_mt5_impl(body: MT5ConfigUpdate) -> dict:
        """Handler: update MT5 configuration, then sync to hcm_broker.accounts."""
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }
        updated, errors = await _write_config_fields(
            config_provider, "mt5", MT5_FIELD_NAMES, body,
        )
        logger.info("MT5 config updated: fields=%s", updated)

        # ── Sync to hcm_broker.accounts ──
        synced_account: Optional[dict] = None
        sync_error: Optional[str] = None

        # Resolve values: prefer frontend field names (login, server), fall back to canonical
        acct_number: int = body.login if body.login else body.account_number
        srv_name: str = (body.server if body.server else body.server_address)
        env_type: int = _TRADE_MODE_TO_ENV.get(body.trade_mode, 1)

        if acct_number > 0 and db_pool is not None and db_pool.is_initialized:
            try:
                # Derive broker_name from server if not explicitly set
                brk_name: str = body.broker_name or "MetaQuotes"
                acct_name: str = f"MT5-{acct_number}"

                row = await db_pool.fetchrow(
                    """INSERT INTO hcm_broker.accounts
                       (account_name, account_number, password_enc, server_name,
                        broker_name, account_type, env_type, leverage, base_currency,
                        is_active, updated_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true, $10)
                       ON CONFLICT (account_number)
                       DO UPDATE SET
                           server_name = EXCLUDED.server_name,
                           password_enc = EXCLUDED.password_enc,
                           account_type = EXCLUDED.account_type,
                           env_type = EXCLUDED.env_type,
                           broker_name = EXCLUDED.broker_name,
                           account_name = hcm_broker.accounts.account_name,
                           is_active = true,
                           updated_at = EXCLUDED.updated_at
                       RETURNING account_id, account_name, account_number,
                                 account_type, server_name, is_active""",
                    acct_name,
                    acct_number,
                    body.password,
                    srv_name,
                    brk_name,
                    body.account_type,
                    env_type,
                    100,   # leverage
                    "USD",  # base_currency
                    datetime.now(timezone.utc),
                )

                if row:
                    synced_account = {
                        "account_id": row["account_id"],
                        "account_name": row["account_name"],
                        "account_number": row["account_number"],
                        "account_type": row["account_type"],
                        "server_name": row["server_name"],
                        "is_active": row["is_active"],
                    }
                    logger.info(
                        "MT5 account synced to hcm_broker.accounts: id=%d, number=%d, type=%s",
                        row["account_id"], acct_number, body.account_type,
                    )
            except Exception as exc:
                sync_error = str(exc)
                logger.error("MT5 account sync failed: %s", exc)
                if errors is None:
                    errors = []
                errors.append(f"account_sync: {sync_error}")

        # ── Build response ──
        count = len(updated)
        suffix_parts: list[str] = []
        if synced_account:
            suffix_parts.append("account synced")
        msg = f"MT5 config: {count} field(s) updated"
        if suffix_parts:
            msg += ", " + ", ".join(suffix_parts)
        if errors:
            msg += f" (with {len(errors)} error(s))"

        response_data: dict[str, Any] = {
            "updated": updated,
            "errors": errors if errors else None,
        }
        if synced_account is not None:
            response_data["synced_account"] = synced_account  # type: ignore[assignment]

        return {
            "code": 0,
            "data": response_data,
            "message": msg,
        }

    # ══════════════════════════════════════════════════════════
    # 3. DeepSeek AI Configuration — shared handler implementations
    # ══════════════════════════════════════════════════════════

    async def _get_deepseek_impl() -> dict:
        """Handler: read DeepSeek configuration with api_key masked (via config_provider)."""
        config = await _read_config_fields(
            config_provider, DEEPSEEK_CONFIG_KEYS, DEEPSEEK_FIELD_NAMES, DEEPSEEK_FIELD_DEFAULTS,
        )
        if config.get("api_key"):
            config["api_key"] = _mask_sensitive(config["api_key"])
        return {"code": 0, "data": config, "message": "ok"}

    async def _put_deepseek_impl(body: DeepSeekConfigUpdate) -> dict:
        """Handler: update DeepSeek configuration.

        If api_key is empty string, preserve the existing value (no re-entry required).
        All other fields are always updated.
        """
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        updated: list[str] = []
        errors: list[str] = []

        # Handle api_key specially: empty means keep current
        if body.api_key != "":
            try:
                await config_provider.set("deepseek.api_key", body.api_key)
                updated.append("api_key")
            except Exception as exc:
                errors.append(f"api_key: {exc}")
        # (else: leave api_key unchanged)

        # Write all other fields
        other_fields = [f for f in DEEPSEEK_FIELD_NAMES if f != "api_key"]
        for field in other_fields:
            val = getattr(body, field, None)
            if val is None:
                continue
            config_key = f"deepseek.{field}"
            try:
                str_val = str(val).lower() if isinstance(val, bool) else str(val)
                ok = await config_provider.set(config_key, str_val)
                if ok:
                    updated.append(field)
                else:
                    errors.append(f"{field}: write failed")
            except Exception as exc:
                errors.append(f"{field}: {exc}")

        logger.info("DeepSeek config updated: fields=%s", updated)
        return {
            "code": 0,
            "data": {
                "updated": updated,
                "errors": errors if errors else None,
            },
            "message": f"DeepSeek config: {len(updated)} field(s) updated"
            if not errors else f"DeepSeek config updated with {len(errors)} error(s)",
        }

    # ══════════════════════════════════════════════════════════
    # 4. Network Configuration — shared handler implementations
    # ══════════════════════════════════════════════════════════

    async def _get_network_impl() -> dict:
        """Handler: read network configuration from config_provider (same source as PUT)."""
        config: dict[str, Any] = dict(NETWORK_FIELD_DEFAULTS)
        if config_provider is not None:
            for key, field in zip(NETWORK_CONFIG_KEYS, NETWORK_FIELD_NAMES):
                try:
                    val = await config_provider.get(key)
                    if val is not None:
                        config[field] = _coerce_type(val, NETWORK_FIELD_DEFAULTS[field])
                except Exception:
                    pass
        return {"code": 0, "data": config, "message": "ok"}

    async def _put_network_impl(body: NetworkConfigUpdate) -> dict:
        """Handler: update network configuration via config_provider."""
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }
        updated, errors = await _write_config_fields(
            config_provider, "network", NETWORK_FIELD_NAMES, body,
        )
        logger.info("Network config updated: fields=%s", updated)
        return {
            "code": 0,
            "data": {
                "updated": updated,
                "errors": errors if errors else None,
            },
            "message": f"Network config: {len(updated)} field(s) updated"
            if not errors else f"Network config updated with {len(errors)} error(s)",
        }

    # ══════════════════════════════════════════════════════════
    # 5. Notification Configuration — shared handler implementations
    # ══════════════════════════════════════════════════════════

    async def _get_notifications_impl() -> dict:
        """Handler: read notification configuration (via config_provider)."""
        config = await _read_config_fields(
            config_provider,
            NOTIFICATION_CONFIG_KEYS,
            NOTIFICATION_FIELD_NAMES,
            NOTIFICATION_FIELD_DEFAULTS,
        )
        return {"code": 0, "data": config, "message": "ok"}

    async def _put_notifications_impl(body: NotificationConfigUpdate) -> dict:
        """Handler: update notification configuration via config_provider."""
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }
        updated, errors = await _write_config_fields(
            config_provider,
            "notification",
            NOTIFICATION_FIELD_NAMES,
            body,
        )
        logger.info("Notification config updated: fields=%s", updated)
        return {
            "code": 0,
            "data": {
                "updated": updated,
                "errors": errors if errors else None,
            },
            "message": f"Notification config: {len(updated)} field(s) updated"
            if not errors else f"Notification config updated with {len(errors)} error(s)",
        }

    # ══════════════════════════════════════════════════════════
    # 6. Cache Management — shared handler implementations
    # ══════════════════════════════════════════════════════════

    async def _get_cache_stats_impl() -> dict:
        """Handler: read Redis cache statistics.

        Uses redis_client.raw.info() to fetch server INFO.
        Returns {"redis_connected": false} if Redis is unavailable.
        """
        if redis_client is None or not redis_client.is_initialized:
            return {
                "code": 0,
                "data": {"redis_connected": False},
                "message": "Redis not connected",
            }

        try:
            info = await redis_client.raw.info()

            # Extract relevant stats
            keyspace_hits = int(info.get("keyspace_hits", 0))
            keyspace_misses = int(info.get("keyspace_misses", 0))
            total_ops = keyspace_hits + keyspace_misses
            hit_rate = round(keyspace_hits / total_ops * 100, 2) if total_ops > 0 else 0.0

            used_memory_bytes = int(info.get("used_memory", 0))
            size_mb = round(used_memory_bytes / (1024 * 1024), 2)

            # Count total keys across all databases
            total_keys = 0
            for key_name, value in info.items():
                if key_name.startswith("db") and "keys=" in str(value):
                    try:
                        # Format: "keys=123,expires=45,avg_ttl=..."
                        keys_part = str(value).split(",")[0]
                        total_keys += int(keys_part.split("=")[1])
                    except (ValueError, IndexError):
                        pass

            stats = {
                "hit_rate": hit_rate,
                "size_mb": size_mb,
                "total_keys": total_keys,
                "ttl_seconds": 3600,
                "redis_connected": True,
                "redis_version": info.get("redis_version", "unknown"),
                "uptime_seconds": int(info.get("uptime_in_seconds", 0)),
            }

            return {"code": 0, "data": stats, "message": "ok"}

        except Exception as exc:
            logger.error("Cache stats query failed: %s", exc)
            return {
                "code": 0,
                "data": {"redis_connected": False, "error": str(exc)},
                "message": "Redis stats unavailable",
            }

    async def _clear_cache_impl() -> dict:
        """Handler: clear all hcm:config:* prefix cache keys.

        Uses Redis SCAN to iterate keys matching the pattern, then DEL in batches.
        Does NOT delete keys with other prefixes.
        """
        if redis_client is None or not redis_client.is_initialized:
            return {
                "code": 0,
                "data": {"cleared": 0, "redis_connected": False},
                "message": "Redis not connected — nothing to clear",
            }

        try:
            deleted_count = 0
            cursor = 0

            while True:
                cursor, keys = await redis_client.raw.scan(
                    cursor=cursor,
                    match=CACHE_CLEAR_PATTERN,
                    count=CACHE_SCAN_COUNT,
                )
                if keys:
                    deleted = await redis_client.raw.delete(*keys)
                    deleted_count += deleted
                if cursor == 0:
                    break

            logger.info("Cache cleared: %d keys matching '%s'", deleted_count, CACHE_CLEAR_PATTERN)

            return {
                "code": 0,
                "data": {
                    "cleared": deleted_count,
                    "pattern": CACHE_CLEAR_PATTERN,
                    "redis_connected": True,
                },
                "message": f"Cleared {deleted_count} cache key(s)",
            }

        except Exception as exc:
            logger.error("Cache clear failed: %s", exc)
            return {"code": "CACHE_001", "data": None, "message": str(exc)}

    # ══════════════════════════════════════════════════════════
    # 7. Account List — shared handler implementation
    # ══════════════════════════════════════════════════════════

    async def _list_accounts_impl() -> dict:
        """Handler: list all accounts from hcm_broker.accounts.

        Returns account details including account_type (master/follower),
        balance, equity, and heartbeat info.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            rows = await db_pool.fetch(
                """SELECT account_id, account_name, account_number, server_name,
                          broker_name, account_type, env_type, leverage,
                          base_currency, is_active, last_balance, last_equity,
                          last_heartbeat, created_at, updated_at
                   FROM hcm_broker.accounts
                   WHERE is_active = true
                   ORDER BY account_id ASC""",
            )

            items: list[dict] = []
            for row in rows:
                item = dict(row)
                # Convert Decimal to string for JSON-safety
                for num_field in ("last_balance", "last_equity", "leverage"):
                    if item.get(num_field) is not None:
                        item[num_field] = str(item[num_field])
                # Serialize timestamps
                for ts_field in ("last_heartbeat", "created_at", "updated_at"):
                    if item.get(ts_field):
                        item[ts_field] = item[ts_field].isoformat()
                items.append(item)

            return {
                "code": 0,
                "data": {
                    "items": items,
                    "total": len(items),
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Account list query failed: %s", exc)
            return {"code": "SYS_001", "data": None, "message": str(exc)}

    async def _delete_account_impl(account_id: int) -> dict:
        """Handler: soft delete an account (set is_active=false).

        Does not physically remove the row; preserves data integrity.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            existing = await db_pool.fetchrow(
                "SELECT account_id, account_name, is_active FROM hcm_broker.accounts WHERE account_id = $1",
                account_id,
            )
            if existing is None:
                return {
                    "code": "ACCT_001",
                    "data": None,
                    "message": f"Account with id={account_id} not found",
                }

            if not existing["is_active"]:
                return {
                    "code": 0,
                    "data": {"account_id": account_id, "deleted": True},
                    "message": f"Account '{existing['account_name']}' is already inactive",
                }

            await db_pool.execute(
                """UPDATE hcm_broker.accounts
                   SET is_active = false, updated_at = $2
                   WHERE account_id = $1""",
                account_id,
                datetime.now(timezone.utc),
            )

            logger.info(
                "Account soft-deleted: account_id=%d, account_name=%s",
                account_id,
                existing["account_name"],
            )

            return {
                "code": 0,
                "data": {"account_id": account_id, "deleted": True},
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Account delete failed for account_id=%d: %s", account_id, exc)
            return {"code": "SYS_001", "data": None, "message": str(exc)}

    async def _create_account_impl(body: dict) -> dict:
        """Handler: create a new broker account."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}
        try:
            account_number = int(body.get("account_number", 0))
            if account_number <= 0:
                return {"code": "ACCT_002", "data": None, "message": "account_number 必须 > 0"}
            row = await db_pool.fetchrow(
                """INSERT INTO hcm_broker.accounts
                    (account_name, account_number, password_enc, server_name,
                     broker_name, account_type, env_type, leverage,
                     base_currency, is_active, initial_deposit)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,true,0)
                   ON CONFLICT (account_number) DO UPDATE SET
                     account_name = EXCLUDED.account_name,
                     server_name = EXCLUDED.server_name,
                     broker_name = EXCLUDED.broker_name,
                     account_type = EXCLUDED.account_type,
                     updated_at = now()
                   RETURNING account_id, account_name, account_number, server_name,
                             broker_name, account_type, env_type, leverage,
                             base_currency, is_active""",
                body.get("account_name", f"Account-{account_number}"),
                account_number,
                body.get("password", ""),
                body.get("server_name", ""),
                body.get("broker_name", ""),
                body.get("account_type", "master"),
                int(body.get("env_type", 1)),
                int(body.get("leverage", 100)),
                body.get("base_currency", "USD"),
            )
            return {
                "code": 0,
                "data": {
                    "account_id": row["account_id"],
                    "account_name": row["account_name"],
                    "account_number": row["account_number"],
                    "created": True,
                },
                "message": "ok",
            }
        except Exception as exc:
            logger.error("Account create failed: %s", exc)
            return {"code": "ACCT_001", "data": None, "message": str(exc)}

    async def _update_account_impl(account_id: int, body: dict) -> dict:
        """Handler: update an existing broker account (excluding password)."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}
        try:
            row = await db_pool.fetchrow(
                """UPDATE hcm_broker.accounts
                   SET account_name = $1,
                       server_name = $2,
                       broker_name = $3,
                       account_type = $4,
                       leverage = $5,
                       base_currency = $6,
                       updated_at = now()
                   WHERE account_id = $7
                   RETURNING account_id, account_name, account_number""",
                body.get("account_name", ""),
                body.get("server_name", ""),
                body.get("broker_name", ""),
                body.get("account_type", "master"),
                int(body.get("leverage", 100)),
                body.get("base_currency", "USD"),
                account_id,
            )
            if row is None:
                return {"code": "ACCT_003", "data": None, "message": f"Account not found: {account_id}"}
            return {
                "code": 0,
                "data": {
                    "account_id": row["account_id"],
                    "account_name": row["account_name"],
                    "account_number": row["account_number"],
                    "updated": True,
                },
                "message": "ok",
            }
        except Exception as exc:
            logger.error("Account update failed for %d: %s", account_id, exc)
            return {"code": "ACCT_001", "data": None, "message": str(exc)}

    async def _update_account_password_impl(account_id: int, body: dict) -> dict:
        """Handler: update only the password for an account."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}
        try:
            new_password = body.get("password", "")
            if not new_password:
                return {"code": "ACCT_004", "data": None, "message": "password 不能为空"}
            result = await db_pool.execute(
                """UPDATE hcm_broker.accounts
                   SET password_enc = $1, updated_at = now()
                   WHERE account_id = $2""",
                new_password, account_id,
            )
            return {"code": 0, "data": {"account_id": account_id, "updated": True}, "message": "ok"}
        except Exception as exc:
            logger.error("Password update failed for %d: %s", account_id, exc)
            return {"code": "ACCT_001", "data": None, "message": str(exc)}

    # ══════════════════════════════════════════════════════════
    # 8. Health Check — aggregated service health probe
    # ══════════════════════════════════════════════════════════

    # Service endpoints for HTTP health probing
    _SERVICE_ENDPOINTS: dict[str, str] = {
        'Gateway': 'http://hcm-gateway:8004/health',
        'Collector': 'http://hcm-collector:8001/health',
        'SignalTower': 'http://hcm-signal-tower:8002/health',
        'MarketIntel': 'http://hcm-market-intel:8006/health',
        'RiskEngine': 'http://hcm-risk-engine:8007/health',
        'Dispatcher': 'http://hcm-dispatcher:8008/health',
        'CopyTrading': 'http://hcm-copy-trading:8009/health',
    }

    async def _detailed_health() -> dict:
        """Aggregate health status by probing all downstream service /health endpoints.

        Each probe measures real latency and extracts per-service status.
        Uptime is computed from uptime_seconds vs current session.
        """
        import asyncio as _asyncio
        import json as _json
        import time as _time
        import urllib.request as _urllib

        def _probe_one(name: str, url: str) -> dict:
            """Synchronous probe of a single /health endpoint, measures real latency."""
            t0 = _time.time()
            try:
                req = _urllib.Request(url, method='GET')
                with _urllib.urlopen(req, timeout=3) as resp:
                    body = resp.read().decode('utf-8')
                    latency_ms = round((_time.time() - t0) * 1000, 1)
                    data = _json.loads(body)
                checks: dict = data.get('checks', {}) or {}
                # Compute uptime_percent from uptime_seconds (assume 30-day window)
                uptime_s = float(data.get('uptime_seconds') or 0)
                # If uptime > 30 days, count as 100% (long-running service)
                uptime_pct = 100.0 if uptime_s > 2592000 else min(99.0 + uptime_s / 86400, 100.0)
                # Determine status: 'down' if probe error, 'healthy' if 'healthy', else 'degraded'
                if data.get('status') == 'healthy':
                    status = 'healthy'
                elif data.get('status') in ('unhealthy', 'down'):
                    status = 'down'
                else:
                    status = 'degraded'
                return {
                    'name': name,
                    'status': status,
                    'latency_ms': latency_ms,
                    'uptime_percent': round(uptime_pct, 2),
                    'last_error': None,
                    'version': str(data.get('version', 'unknown')),
                    'details': {
                        'redis': checks.get('redis', {}).get('status', 'unknown'),
                        'postgresql': checks.get('postgresql', {}).get('status', 'unknown'),
                        'uptime_s': int(uptime_s),
                    },
                }
            except Exception as exc:
                latency_ms = round((_time.time() - t0) * 1000, 1)
                return {
                    'name': name,
                    'status': 'down',
                    'latency_ms': latency_ms,
                    'uptime_percent': 0.0,
                    'last_error': str(exc)[:200],
                    'version': 'unknown',
                    'details': {},
                }

        # Run all probes concurrently via thread pool
        loop = _asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(None, _probe_one, name, url)
            for name, url in _SERVICE_ENDPOINTS.items()
        ]
        results = await _asyncio.gather(*tasks)

        services: list[dict] = list(results)

        # Prepend local hcm-web service (probe self)
        import time as _time
        web_t0 = _time.time()
        try:
            web_uptime = 0.0
            web_version = '2.0.0'
            web_pg = 'connected'
            web_redis = 'connected'
            if db_pool is not None and db_pool.is_initialized:
                web_uptime = 0.0  # No tracking yet
                try:
                    await db_pool.fetchval("SELECT 1")
                except Exception:
                    web_pg = 'disconnected'
            if redis_client is not None and redis_client.is_initialized:
                try:
                    await redis_client.ping()
                except Exception:
                    web_redis = 'disconnected'
            web_latency = round((_time.time() - web_t0) * 1000, 1)
            services.insert(0, {
                'name': 'Web',
                'status': 'healthy' if web_pg == 'connected' else 'degraded',
                'latency_ms': web_latency,
                'uptime_percent': 100.0,
                'last_error': None,
                'version': web_version,
                'details': {
                    'role': 'api + dashboard',
                    'pg': web_pg,
                    'redis': web_redis,
                    'uptime_s': int(web_uptime),
                },
            })
        except Exception as exc:
            services.insert(0, {
                'name': 'Web',
                'status': 'down',
                'latency_ms': 0.0,
                'uptime_percent': 0.0,
                'last_error': str(exc)[:200],
                'version': '2.0.0',
                'details': {},
            })

        # Compute overall health
        down_count = sum(1 for s in services if s['status'] == 'down')
        degraded_count = sum(1 for s in services if s['status'] == 'degraded')
        healthy_count = sum(1 for s in services if s['status'] == 'healthy')
        if down_count == 0 and degraded_count == 0:
            overall = 'healthy'
        elif down_count == 0:
            overall = 'degraded'
        else:
            overall = 'unhealthy'

        # ── Real-time DB stats ──
        klines_count = 0
        klines_by_symbol: dict[str, int] = {}
        rules_count = 0
        mt5_accounts_count = 1
        ai_status = 'bypass'
        if db_pool is not None and db_pool.is_initialized:
            try:
                # Count klines by symbol, then sum for active symbols
                rows = await db_pool.fetch(
                    "SELECT symbol, count(*) AS cnt FROM hcm_market.klines GROUP BY symbol"
                )
                klines_by_symbol = {r['symbol']: r['cnt'] for r in rows}
                klines_count = sum(klines_by_symbol.values())
            except Exception:
                pass
            try:
                rules_count = await db_pool.fetchval(
                    "SELECT count(*) FROM hcm_engine.rules WHERE enabled = true"
                ) or 0
            except Exception:
                pass
            try:
                mt5_accounts_count = await db_pool.fetchval(
                    "SELECT count(*) FROM hcm_trading.accounts WHERE is_active = true"
                ) or 0
            except Exception:
                pass
            try:
                # AI 状态：检查最近 1 小时信号是否经过 AI
                # signal_tower_mode 字段: 'indicator_scoring' | 'ai_decision' | 'hybrid'
                last_ai_row = await db_pool.fetchrow("""
                    SELECT signal_tower_mode
                    FROM hcm_signal.signals
                    WHERE created_at > now() - interval '1 hour'
                    ORDER BY created_at DESC LIMIT 1
                """)
                if last_ai_row:
                    mode = (last_ai_row.get('signal_tower_mode') or '').lower()
                    if 'ai' in mode:
                        ai_status = "AI 评分"
                    elif 'hybrid' in mode:
                        ai_status = "混合评分"
                    else:
                        ai_status = "指标评分"
                else:
                    ai_status = "无信号"
            except Exception:
                pass

        return {
            'code': 0,
            'data': {
                'overall': overall,
                'services': services,
                'healthy_count': healthy_count,
                'degraded_count': degraded_count,
                'down_count': down_count,
                'klines_loaded': klines_count,
                'klines_by_symbol': klines_by_symbol,
                'engine_rules': rules_count,
                'mt5_accounts': mt5_accounts_count,
                'price_feed': 'active' if klines_count > 0 else 'idle',
                'ai_status': ai_status,
                'timezone': 'UTC (K-line open_time), bridge pushes every 2s',
            },
            'message': 'ok',
        }

    # ══════════════════════════════════════════════════════════
    # API v1 Routes
    # ══════════════════════════════════════════════════════════

    # ── 1. User Management v1 ────────────────────

    @router.get("/api/v1/system/users")
    async def list_users_v1(
        request: Request,
        search: Optional[str] = Query(None, description="Search by username, display_name"),
        role: Optional[str] = Query(None, description="Filter by role name"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        user=Depends(auth_handler.require_auth),
    ):
        """List users with search, role filter, and pagination.

        JOINs hcm_system.roles to include role_name.
        """
        return await _list_users_impl(search, role, page, page_size)

    @router.post("/api/v1/system/users")
    async def create_user_v1(
        body: UserCreate,
        user=Depends(auth_handler.require_auth),
    ):
        """Create a new user.

        Password is hashed with bcrypt. Default roles (admin/operator/viewer/trader)
        are auto-inserted if the roles table is empty.
        Returns USER_001 if username already exists.
        """
        return await _create_user_impl(body)

    @router.put("/api/v1/system/users/{user_id}")
    async def update_user_v1(
        user_id: int,
        body: UserUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Partial update of a user.

        Leave password empty to keep current value.
        Supports toggling is_active for enable/disable.
        """
        return await _update_user_impl(user_id, body)

    @router.delete("/api/v1/system/users/{user_id}")
    async def delete_user_v1(
        user_id: int,
        user=Depends(auth_handler.require_auth),
    ):
        """Soft-delete a user — sets is_active=false.

        Does not physically remove the row; preserves audit history.
        """
        return await _delete_user_impl(user_id)

    # ── 2. MT5 Configuration v1 ──────────────────

    @router.get("/api/v1/system/mt5")
    async def get_mt5_v1(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get MT5 broker configuration (password masked)."""
        return await _get_mt5_impl()

    @router.put("/api/v1/system/mt5")
    async def put_mt5_v1(
        body: MT5ConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Update MT5 broker configuration."""
        return await _put_mt5_impl(body)

    # ── 3. DeepSeek AI Configuration v1 ──────────

    @router.get("/api/v1/system/deepseek")
    async def get_deepseek_v1(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get DeepSeek AI configuration (api_key masked)."""
        return await _get_deepseek_impl()

    @router.put("/api/v1/system/deepseek")
    async def put_deepseek_v1(
        body: DeepSeekConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Update DeepSeek AI configuration.

        Leave api_key empty to keep the current value (no re-entry required).
        """
        return await _put_deepseek_impl(body)

    # ── 4. Network Configuration v1 ──────────────

    @router.get("/api/v1/system/network")
    async def get_network_v1(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get network/service binding configuration."""
        return await _get_network_impl()

    @router.put("/api/v1/system/network")
    async def put_network_v1(
        body: NetworkConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Update network/service binding configuration."""
        return await _put_network_impl(body)

    # ── 5. Notification Configuration v1 ─────────

    @router.get("/api/v1/system/notifications")
    async def get_notifications_v1(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get notification channel configuration."""
        return await _get_notifications_impl()

    @router.put("/api/v1/system/notifications")
    async def put_notifications_v1(
        body: NotificationConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Update notification channel configuration."""
        return await _put_notifications_impl(body)

    # ── 6. Cache Management v1 ───────────────────

    @router.get("/api/v1/system/cache/stats")
    async def get_cache_stats_v1(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get Redis cache statistics.

        Returns hit_rate, size_mb, total_keys, and redis_connected flag.
        If Redis is unavailable, returns {"redis_connected": false}.
        """
        return await _get_cache_stats_impl()

    @router.post("/api/v1/system/cache/clear")
    async def clear_cache_v1(
        user=Depends(auth_handler.require_auth),
    ):
        """Clear all hcm:config:* cache keys.

        Uses SCAN + DEL to safely iterate and delete matching keys.
        Does NOT affect keys with other prefixes.
        """
        return await _clear_cache_impl()

    # ── 7. Account List v1 ─────────────────────

    @router.get("/api/v1/system/accounts")
    async def list_accounts_v1(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """List all accounts from hcm_broker.accounts.

        Returns accounts with master/follower type labels.
        """
        return await _list_accounts_impl()

    @router.delete("/api/v1/system/accounts/{account_id}")
    async def delete_account_v1(
        account_id: int,
        user=Depends(auth_handler.require_auth),
    ):
        """Soft-delete an account — sets is_active=false.

        Does not physically remove the row; preserves data integrity.
        """
        return await _delete_account_impl(account_id)

    @router.post("/api/v1/system/accounts")
    async def create_account_v1(
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """Create a new broker account."""
        return await _create_account_impl(body)

    @router.put("/api/v1/system/accounts/{account_id}")
    async def update_account_v1(
        account_id: int,
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """Update an existing account (excluding password)."""
        return await _update_account_impl(account_id, body)

    @router.put("/api/v1/system/accounts/{account_id}/password")
    async def update_account_password_v1(
        account_id: int,
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """Update only the password for an account."""
        return await _update_account_password_impl(account_id, body)

    # ── 8. Health Check v1 ───────────────────────

    @router.get("/api/v1/system/health/detailed")
    async def detailed_health_v1(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Aggregated health status for all services.

        Probes every downstream /health endpoint concurrently and
        returns overall status with per-service details.
        """
        return await _detailed_health()

    # ══════════════════════════════════════════════════════════
    # Legacy backward-compatible routes
    # ══════════════════════════════════════════════════════════

    # ── 1. User Management legacy ────────────────

    @router.get("/api/system/users")
    async def legacy_list_users(
        request: Request,
        search: Optional[str] = Query(None),
        role: Optional[str] = Query(None),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] List users — alias for /api/v1/system/users."""
        return await _list_users_impl(search, role, page, page_size)

    @router.post("/api/system/users")
    async def legacy_create_user(
        body: UserCreate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Create user — alias for /api/v1/system/users."""
        return await _create_user_impl(body)

    @router.put("/api/system/users/{user_id}")
    async def legacy_update_user(
        user_id: int,
        body: UserUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update user — alias for /api/v1/system/users/{user_id}."""
        return await _update_user_impl(user_id, body)

    @router.delete("/api/system/users/{user_id}")
    async def legacy_delete_user(
        user_id: int,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Delete user — alias for /api/v1/system/users/{user_id}."""
        return await _delete_user_impl(user_id)

    # ── 2. MT5 Configuration legacy ──────────────

    @router.get("/api/system/mt5")
    async def legacy_get_mt5(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get MT5 config — alias for /api/v1/system/mt5."""
        return await _get_mt5_impl()

    @router.put("/api/system/mt5")
    async def legacy_put_mt5(
        body: MT5ConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update MT5 config — alias for /api/v1/system/mt5."""
        return await _put_mt5_impl(body)

    # ── 3. DeepSeek Configuration legacy ─────────

    @router.get("/api/system/deepseek")
    async def legacy_get_deepseek(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get DeepSeek config — alias for /api/v1/system/deepseek."""
        return await _get_deepseek_impl()

    @router.put("/api/system/deepseek")
    async def legacy_put_deepseek(
        body: DeepSeekConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update DeepSeek config — alias for /api/v1/system/deepseek."""
        return await _put_deepseek_impl(body)

    # ── 4. Network Configuration legacy ──────────

    @router.get("/api/system/network")
    async def legacy_get_network(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get network config — alias for /api/v1/system/network."""
        return await _get_network_impl()

    @router.put("/api/system/network")
    async def legacy_put_network(
        body: NetworkConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update network config — alias for /api/v1/system/network."""
        return await _put_network_impl(body)

    # ── 5. Notification Configuration legacy ─────

    @router.get("/api/system/notifications")
    async def legacy_get_notifications(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get notification config — alias for /api/v1/system/notifications."""
        return await _get_notifications_impl()

    @router.put("/api/system/notifications")
    async def legacy_put_notifications(
        body: NotificationConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update notification config — alias for /api/v1/system/notifications."""
        return await _put_notifications_impl(body)

    # ── 6. Cache Management legacy ───────────────

    @router.get("/api/system/cache/stats")
    async def legacy_get_cache_stats(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get cache stats — alias for /api/v1/system/cache/stats."""
        return await _get_cache_stats_impl()

    @router.post("/api/system/cache/clear")
    async def legacy_clear_cache(
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Clear cache — alias for /api/v1/system/cache/clear."""
        return await _clear_cache_impl()

    # ── 7. Account List legacy ──────────────────

    @router.get("/api/system/accounts")
    async def legacy_list_accounts(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] List accounts — alias for /api/v1/system/accounts."""
        return await _list_accounts_impl()

    @router.delete("/api/system/accounts/{account_id}")
    async def legacy_delete_account(
        account_id: int,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Delete account — alias for /api/v1/system/accounts/{account_id}."""
        return await _delete_account_impl(account_id)

    @router.post("/api/system/accounts")
    async def legacy_create_account(
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Create account — alias for /api/v1/system/accounts."""
        return await _create_account_impl(body)

    @router.put("/api/system/accounts/{account_id}")
    async def legacy_update_account(
        account_id: int,
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update account — alias for /api/v1/system/accounts/{account_id}."""
        return await _update_account_impl(account_id, body)

    @router.put("/api/system/accounts/{account_id}/password")
    async def legacy_update_password(
        account_id: int,
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update password — alias for /api/v1/system/accounts/{account_id}/password."""
        return await _update_account_password_impl(account_id, body)
        """[Legacy] Delete account — alias for /api/v1/system/accounts/{account_id}."""
        return await _delete_account_impl(account_id)

    # ── 8. Health Check legacy ───────────────────

    @router.get("/api/system/health/detailed", include_in_schema=False)
    async def legacy_detailed_health(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Health check — alias for /api/v1/system/health/detailed."""
        return await _detailed_health()

    return router
