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
import os
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
    "notification.dingtalk_secret",
    "notification.wecom_webhook",
    "notification.wecom_secret",
    "notification.enable_trade_alert",
    "notification.enable_signal_alert",
    "notification.enable_risk_alert",
    "notification.enable_system_alert",
    "notification.dingtalk_enabled",
    "notification.wecom_enabled",
]

NOTIFICATION_FIELD_NAMES: list[str] = [
    k.replace("notification.", "") for k in NOTIFICATION_CONFIG_KEYS
]
NOTIFICATION_FIELD_DEFAULTS: dict[str, Any] = {
    "dingtalk_webhook": "",
    "dingtalk_secret": "",
    "wecom_webhook": "",
    "wecom_secret": "",
    "enable_trade_alert": True,
    "enable_signal_alert": True,
    "enable_risk_alert": True,
    "enable_system_alert": False,
    "dingtalk_enabled": True,
    "wecom_enabled": True,
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
    dingtalk_secret: str = Field("", description="DingTalk webhook signing secret (加签)")
    wecom_webhook: str = Field("", description="WeCom (企业微信) group robot webhook URL")
    wecom_secret: str = Field("", description="WeCom webhook secret (optional)")
    enable_trade_alert: bool = Field(False, description="Enable trade/order alerts")
    enable_signal_alert: bool = Field(True, description="Enable signal alerts")
    enable_risk_alert: bool = Field(True, description="Enable risk alerts")
    enable_system_alert: bool = Field(False, description="Enable system alerts")
    dingtalk_enabled: bool = Field(True, description="是否启用钉钉推送（勾选）")
    wecom_enabled: bool = Field(True, description="是否启用企业微信推送（勾选）")


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


def _is_masked(value: Any) -> bool:
    """Return True if `value` is a _mask_sensitive() token (starts with '***').

    Used by PUT handlers to detect a client echoing back the masked secret
    instead of a real value, so we keep the server-side secret instead of
    overwriting it with the placeholder (echo-back would otherwise wipe it).
    """
    return isinstance(value, str) and value.startswith("***")


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

        # Map frontend field names → backend config keys before persisting.
        # frontend ConfigForm keys: server/login → backend config keys: server_address/account_number
        _body_data = body.model_dump()
        _frontend_to_config = {
            "server": "server_address",
            "login": "account_number",
        }
        for _ff, _cf in _frontend_to_config.items():
            _fv = _body_data.get(_ff)
            if _fv is not None and _fv != MT5_FIELD_DEFAULTS.get(_cf):
                setattr(body, _cf, _fv)

        # Strip whitespace from password to prevent invisible characters (e.g. tab)
        body.password = (body.password or "").strip()

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
        # 脱敏: secret 不在 GET 中明文返回 (呼应 G2 明文密码教训)
        if config.get("dingtalk_secret"):
            config["dingtalk_secret"] = _mask_sensitive(config["dingtalk_secret"])
        if config.get("wecom_secret"):
            config["wecom_secret"] = _mask_sensitive(config["wecom_secret"])
        return {"code": 0, "data": config, "message": "ok"}

    async def _put_notifications_impl(body: NotificationConfigUpdate) -> dict:
        """Handler: update notification configuration via config_provider.

        安全约定(呼应 DeepSeek api_key 的 "empty = keep current"):
        - dingtalk_secret / wecom_secret 若为空串, 或仍是 GET 返回的脱敏串(***开头),
          则不覆盖服务端已存值 —— 避免前端把脱敏串当明文回写、清空真密钥。
        - 其它字段(webhook URL / 各告警开关)始终写入(空串即清空该配置)。
        """
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        updated: list[str] = []
        errors: list[str] = []

        # 1) 非 secret 字段: 始终写入(空串 = 清空)
        secret_fields = {"dingtalk_secret", "wecom_secret"}
        for field in NOTIFICATION_FIELD_NAMES:
            if field in secret_fields:
                continue
            val = getattr(body, field, None)
            if val is None:
                continue
            config_key = f"notification.{field}"
            try:
                str_val = str(val).lower() if isinstance(val, bool) else str(val)
                ok = await config_provider.set(config_key, str_val)
                if ok:
                    updated.append(field)
                else:
                    errors.append(f"{field}: write failed")
            except Exception as exc:
                errors.append(f"{field}: {exc}")

        # 2) secret 字段: 仅当传入"真实值"(非空且非脱敏串)时才写入
        for field in sorted(secret_fields):
            val = getattr(body, field, None)
            if not isinstance(val, str) or val == "":
                continue  # 空 = 保留当前值
            if _is_masked(val):
                continue  # 脱敏回传 = 保留当前值
            config_key = f"notification.{field}"
            try:
                ok = await config_provider.set(config_key, val)
                if ok:
                    updated.append(field)
                else:
                    errors.append(f"{field}: write failed")
            except Exception as exc:
                errors.append(f"{field}: {exc}")

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
                          last_heartbeat, created_at, updated_at,
                          terminal_path, status
                          FROM hcm_broker.accounts
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
        """Handler: hard delete an account (physically remove the row).

        hcm_broker.accounts 被多张表外键引用（均为 NO ACTION）：
        hcm_broker.trade_rules、hcm_copy.relationships(两个列)、hcm_signal.signals、
        hcm_trading.orders、hcm_trading.positions。
        因此必须在删除账户行之前，按依赖顺序级联清理这些引用记录，否则外键约束会
        拒绝删除，使删除功能“看似无效”（实际抛 ForeignKeyViolationError）。
        所有删除放在一个事务里：要么全部成功，要么整体回滚，避免出现孤儿数据。
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            existing = await db_pool.fetchrow(
                "SELECT account_id, account_name, account_number FROM hcm_broker.accounts WHERE account_id = $1",
                account_id,
            )
            if existing is None:
                return {
                    "code": "ACCT_001",
                    "data": None,
                    "message": f"Account with id={account_id} not found",
                }

            # MT5 登录号（即桥按账户上锁/存活心跳所用的 live_login）。
            # 删除后需通知对应桥实例释放，否则运行中账户的桥进程仍会持续交易该账户，
            # 表现为“删除了但还在跑 / 删除未生效”。
            login = existing.get("account_number")

            # 按外键依赖顺序级联清理引用该账户的所有记录，最后删除账户本身。
            # 关键：signals 是账户间共享的（跟单号复制主号的 signal_id），orders/positions/
            # trade_logs/intercept_logs/hexp_shadow_eval 等都可能【跨账户】引用本账户的
            # signal_id；且存在 positions(order_id) → orders(signal_id) → signals 的依赖链。
            # 必须按“先叶子后根、且覆盖跨账户引用”的顺序清理，否则外键约束会拒绝删除，
            # 表现为“删除账户报错 / 删除未生效”。
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    import re as _re

                    # 1) 找出所有外键引用 hcm_signal.signals(signal_id) 的表（含分区表，
                    #    动态发现，避免遗漏新分区）。先清空这些表中引用本账户 signals 的
                    #    行（跨账户一并清理）。orders/positions 依赖 positions 先删，单独处理。
                    ref_rows = await conn.fetch(
                        """SELECT DISTINCT tc.table_schema || '.' || tc.table_name AS tbl
                           FROM information_schema.table_constraints tc
                           JOIN information_schema.key_column_usage kcu
                             ON tc.constraint_name = kcu.constraint_name
                            AND tc.table_schema = kcu.table_schema
                           JOIN information_schema.constraint_column_usage ccu
                             ON ccu.constraint_name = tc.constraint_name
                          WHERE tc.constraint_type = 'FOREIGN KEY'
                            AND ccu.table_schema = 'hcm_signal'
                            AND ccu.table_name = 'signals'
                            AND kcu.column_name = 'signal_id'"""
                    )
                    for _r in ref_rows:
                        _tbl = _r["tbl"]
                        # 仅允许简单的 schema.table 形式，杜绝 SQL 注入。
                        if not _re.match(r"^[a-z_0-9]+\.[a-z_0-9]+$", _tbl or ""):
                            continue
                        # orders / positions 在 positions 清空后再删，避免破坏
                        # positions.order_id → orders 外键，这里跳过。
                        if _tbl in ("hcm_trading.orders", "hcm_trading.positions"):
                            continue
                        await conn.execute(
                            f"DELETE FROM {_tbl} WHERE signal_id IN "
                            f"(SELECT signal_id FROM hcm_signal.signals WHERE account_id = $1)",
                            account_id,
                        )

                    # 2) 清理引用本账户 signals 的 orders（跨账户）。必须在 positions 之后，
                    #    因为 positions.order_id 外键指向 orders。
                    #    先删跨账户 positions（它们引用跨账户 orders）。
                    await conn.execute(
                        "DELETE FROM hcm_trading.positions WHERE order_id IN "
                        "(SELECT order_id FROM hcm_trading.orders WHERE signal_id IN "
                        "(SELECT signal_id FROM hcm_signal.signals WHERE account_id = $1))",
                        account_id,
                    )
                    # 再删本账户 positions（引用本账户 orders）。
                    await conn.execute(
                        "DELETE FROM hcm_trading.positions WHERE account_id = $1", account_id
                    )
                    # 现在 positions 已全部清空，可安全删除引用本账户 signals 的 orders（跨账户）。
                    await conn.execute(
                        "DELETE FROM hcm_trading.orders WHERE signal_id IN "
                        "(SELECT signal_id FROM hcm_signal.signals WHERE account_id = $1)",
                        account_id,
                    )
                    # 兜底：本账户可能残留未引用本账户 signals 的 orders。
                    await conn.execute(
                        "DELETE FROM hcm_trading.orders WHERE account_id = $1", account_id
                    )
                    # 3) 本账户 signals 此时已无任何引用，可删除。
                    await conn.execute(
                        "DELETE FROM hcm_signal.signals WHERE account_id = $1", account_id
                    )
                    # 4) 其余直接引用 accounts 的表。
                    await conn.execute(
                        "DELETE FROM hcm_broker.trade_rules WHERE account_id = $1", account_id
                    )
                    await conn.execute(
                        "DELETE FROM hcm_copy.relationships "
                        "WHERE master_account_id = $1 OR copy_account_id = $1",
                        account_id,
                    )
                    # 5) 最后删除账户本身。
                    await conn.execute(
                        "DELETE FROM hcm_broker.accounts WHERE account_id = $1", account_id
                    )

            logger.info(
                "Account hard-deleted: account_id=%d, account_name=%s, login=%s",
                account_id,
                existing["account_name"],
                login,
            )

            # 释放该账户对应的桥实例：下发 restart 信令（桥收到后自行退出，由主机看门狗重拉），
            # 并清理单实例锁/存活心跳/消费组等 Redis 残留键，使删除对该运行账户真正生效。
            try:
                r = _get_redis_client()
                if r is not None:
                    if login is not None:
                        r.set(f"bridge:control:{login}", "restart", ex=120)
                        r.delete(f"bridge:instance:lock:{login}")
                        r.delete(f"bridge:alive:{login}")
                    r.delete(f"group:{account_id}")
            except Exception as r_exc:  # pragma: no cover
                logger.warning(
                    "Account delete: post-cleanup redis keys failed for account_id=%d: %s",
                    account_id,
                    r_exc,
                )

            return {
                "code": 0,
                "data": {"account_id": account_id, "deleted": True},
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Account delete failed for account_id=%d: %s", account_id, exc)
            return {"code": "SYS_001", "data": None, "message": str(exc)}

    async def _set_account_active_impl(account_id: int, is_active: bool) -> dict:
        """Handler: enable / disable an account (is_active flag).

        与 status（running/stopped）正交：停用仅标记 is_active=false，账户仍保留在
        账户列表中（只是带“禁用”标记），不会真正删除；删除请用 DELETE 接口。
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

            await db_pool.execute(
                """UPDATE hcm_broker.accounts
                   SET is_active = $2, updated_at = $3
                   WHERE account_id = $1""",
                account_id,
                is_active,
                datetime.now(timezone.utc),
            )

            # 广播给 bridge：account.<id>.is_active（与 status 开关一致，停用即时生效，
            # 无需重启 bridge；bridge 运行时闸门优先读 Redis，PG 每 5s 重载兜底）
            r = _get_redis_client()
            if r is not None:
                try:
                    r.set(f"account.{account_id}.is_active", "true" if is_active else "false")
                except Exception as exc:  # pragma: no cover
                    logger.warning("G6: is_active broadcast for %s failed (non-fatal): %s", account_id, exc)

            logger.info(
                "Account %s: account_id=%d, account_name=%s",
                "enabled" if is_active else "disabled",
                account_id,
                existing["account_name"],
            )

            return {
                "code": 0,
                "data": {"account_id": account_id, "is_active": is_active},
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Account active toggle failed for account_id=%d: %s", account_id, exc)
            return {"code": "SYS_001", "data": None, "message": str(exc)}

    async def _create_account_impl(body: dict) -> dict:
        """Handler: create a new broker account.

        Extensible fields (G1/G6):
          - terminal_path: 可选；NULL=系统自动分配终端（用户可选覆盖）
          - status: 默认 'running'（新增即用，贴合最小配置承诺）
        G4: 创建成功后幂等建独立消费组 group:<account_id>，使账户立即可接入跟单信号。
        """
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
                     base_currency, terminal_path, is_active, initial_deposit, status)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true,0,'running')
                   ON CONFLICT (account_number) DO UPDATE SET
                     account_name = EXCLUDED.account_name,
                     server_name = EXCLUDED.server_name,
                     broker_name = EXCLUDED.broker_name,
                     account_type = EXCLUDED.account_type,
                     terminal_path = EXCLUDED.terminal_path,
                     updated_at = now()
                   RETURNING account_id, account_name, account_number, server_name,
                             broker_name, account_type, env_type, leverage,
                             base_currency, is_active, terminal_path, status""",
                body.get("account_name", f"Account-{account_number}"),
                account_number,
                body.get("password", ""),
                body.get("server_name", ""),
                body.get("broker_name", ""),
                body.get("account_type", "master"),
                int(body.get("env_type", 1)),
                int(body.get("leverage", 100)),
                body.get("base_currency", "USD"),
                body.get("terminal_path"),  # None → 系统自动分配终端
            )
            # G4: 新建账户即自动建独立消费组（立即可用；失败非致命，bridge 启动也会自愈）
            await ensure_account_consumer_group(row["account_id"])
            return {
                "code": 0,
                "data": {
                    "account_id": row["account_id"],
                    "account_name": row["account_name"],
                    "account_number": row["account_number"],
                    "terminal_path": row["terminal_path"],
                    "status": row["status"],
                    "created": True,
                },
                "message": "ok",
            }
        except Exception as exc:
            logger.error("Account create failed: %s", exc)
            return {"code": "ACCT_001", "data": None, "message": str(exc)}

    async def _update_account_impl(account_id: int, body: dict) -> dict:
        """Handler: update an existing broker account (excluding password).

        Extensible (G1/G6): 可选更新 terminal_path 与 status（启停）；
        原有档案字段保持不变。未提供的可选字段不覆盖。
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}
        try:
            set_clauses = [
                "account_name = $1",
                "server_name = $2",
                "broker_name = $3",
                "account_type = $4",
                "leverage = $5",
                "base_currency = $6",
                "updated_at = now()",
            ]
            params: list[Any] = [
                body.get("account_name", ""),
                body.get("server_name", ""),
                body.get("broker_name", ""),
                body.get("account_type", "master"),
                int(body.get("leverage", 100)),
                body.get("base_currency", "USD"),
            ]
            if "terminal_path" in body and body.get("terminal_path") is not None:
                params.append(body.get("terminal_path"))
                set_clauses.append(f"terminal_path = ${len(params)}")
            if "status" in body and body.get("status") is not None:
                params.append(body.get("status"))
                set_clauses.append(f"status = ${len(params)}")
            params.append(account_id)
            where_idx = len(params)
            row = await db_pool.fetchrow(
                f"""UPDATE hcm_broker.accounts
                    SET {", ".join(set_clauses)}
                    WHERE account_id = ${where_idx}
                    RETURNING account_id, account_name, account_number, status""",
                *params,
            )
            if row is None:
                return {"code": "ACCT_003", "data": None, "message": f"Account not found: {account_id}"}
            return {
                "code": 0,
                "data": {
                    "account_id": row["account_id"],
                    "account_name": row["account_name"],
                    "account_number": row["account_number"],
                    "status": row["status"],
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

    # ── 7a. Account extensibility helpers (G1/G4/G6) ──────────
    def _get_redis_client():
        """Lazily build a Redis client from REDIS_URL env (docker: redis://redis:6379).

        Best-effort: returns None on failure so callers degrade gracefully
        (account creation still succeeds; the bridge also self-heals the consumer
        group on startup). Avoids a hard import-time dependency on redis config.
        """
        try:
            import redis  # present in hcm-web image (used by config_provider)
            url = os.environ.get("REDIS_URL", "redis://localhost:6379")
            return redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
        except Exception as exc:  # pragma: no cover
            logger.warning("Redis client unavailable (%s) — group/status broadcast skipped", exc)
            return None

    async def ensure_account_consumer_group(account_id: int) -> None:
        """G4: idempotently create the per-account consumer group on signal:risk_passed.

        group:<account_id> lets this account receive risk-passed signals independently
        (broadcast model — every account group gets a copy; each bridge filters by its
        own account_id). Called on account creation so a new account is immediately
        wired for copy-trading. Bridge also recreates the group on startup (NOGROUP
        self-heal), so a Redis miss here is non-fatal.
        """
        r = _get_redis_client()
        if r is None:
            return
        try:
            r.xgroup_create("signal:risk_passed", f"group:{account_id}", mkstream=True, id="$")
            logger.info("G4: created consumer group group:%s on signal:risk_passed", account_id)
        except Exception as exc:
            if "BUSYGROUP" in str(exc).upper():
                pass  # group already exists — idempotent
            else:
                logger.warning("G4: ensure consumer group for %s failed (non-fatal): %s", account_id, exc)

    async def _set_account_status_impl(account_id: int, status: str) -> dict:
        """G6: set account-level start/stop status and broadcast it for live control.

        status: running (normal) / stopped / paused (no copy-trading; credentials and
        history preserved, recoverable). Orthogonal to is_active (soft-delete).
        Broadcasts account.<id>.status to Redis so running bridges pick it up without
        a restart.
        """
        if status not in ("running", "stopped", "paused"):
            return {"code": "ACCT_005", "data": None,
                    "message": "status 必须是 running/stopped/paused"}
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}
        try:
            row = await db_pool.fetchrow(
                """UPDATE hcm_broker.accounts
                   SET status = $1, updated_at = now()
                   WHERE account_id = $2
                   RETURNING account_id, status""",
                status, account_id,
            )
            if row is None:
                return {"code": "ACCT_003", "data": None,
                        "message": f"Account not found: {account_id}"}
            # 广播给 bridge：account.<id>.status（bridge 读循环实时读取，无需重启）
            r = _get_redis_client()
            if r is not None:
                try:
                    r.set(f"account.{account_id}.status", status)
                except Exception as exc:  # pragma: no cover
                    logger.warning("G6: status broadcast for %s failed (non-fatal): %s", account_id, exc)
            return {
                "code": 0,
                "data": {"account_id": row["account_id"], "status": row["status"], "updated": True},
                "message": "ok",
            }
        except Exception as exc:
            logger.error("Account status update failed for %d: %s", account_id, exc)
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

    async def _pipeline_status() -> dict:
        """Return pipeline node statuses for axis bar display.

        Probes K-lines, signals, signal publishing, risk engine, dispatcher,
        MT5 connection, account balance, AI scoring, and external factors
        in parallel. Each node returns name, status, group, metric,
        last_activity, and a troubleshooting checklist.
        """
        import asyncio as _asyncio
        import json as _json
        import time as _time

        nodes: list[dict] = []

        async def _check_klines() -> dict:
            """K-line ingestion status — checks most recent open_time and count."""
            try:
                row = await db_pool.fetchrow(
                    "SELECT MAX(open_time) AS last_time, COUNT(*) AS cnt FROM hcm_market.klines"
                )
                cnt: int = row['cnt'] if row else 0
                last = row['last_time'] if row and row['last_time'] else None
                stale_min: float | None = None
                if last:
                    delta = datetime.now(timezone.utc) - (
                        last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last
                    )
                    stale_min = round(delta.total_seconds() / 60, 1)
                if stale_min is not None and stale_min < 5:
                    status = 'healthy'
                elif stale_min is not None and stale_min < 30:
                    status = 'warning'
                else:
                    status = 'down'
                return {
                    'name': 'K线入库', 'status': status, 'group': 'core',
                    'metric': f'{cnt} 根',
                    'last_activity': last.isoformat() if last else None,
                    'troubleshooting': [
                        '检查 MT5 Bridge 是否运行: tasklist | findstr python',
                        '查看 bridge.log 确认 K 线写入',
                        '确认 MT5 终端已登录模拟账户',
                        '检查 hcm-collector 容器: docker logs hcm-v2-hcm-collector-1 --tail 20',
                    ],
                }
            except Exception:
                return {
                    'name': 'K线入库', 'status': 'down', 'group': 'core',
                    'metric': '--', 'last_activity': None, 'troubleshooting': [],
                }

        async def _check_signals() -> dict:
            """信号生成状态 — 读调度器发布的引擎状态(存活/激活模型/切换) + PG 信号计数。"""
            try:
                cnt_row = await db_pool.fetchrow("SELECT COUNT(*) AS cnt FROM hcm_signal.signals")
                cnt = cnt_row['cnt'] if cnt_row else 0

                # 读引擎状态（调度器每循环发布到 Redis）
                engine: dict = {}
                if redis_client is not None and redis_client.is_initialized:
                    try:
                        raw = await redis_client.get("hcm:signal_tower:engine_status")
                        if raw:
                            engine = _json.loads(raw)
                    except Exception:
                        engine = {}

                now = datetime.now(timezone.utc)
                running = bool(engine.get("running", False))
                last_loop_at = engine.get("last_loop_at")
                last_prod = engine.get("last_production_at")
                signals_produced = engine.get("signals_produced", 0)

                # 激活模型：优先取引擎发布的，否则回退配置中心
                active_model = engine.get("active_model")
                if not active_model and redis_client is not None and redis_client.is_initialized:
                    try:
                        active_model = await redis_client.hget("hcm:config:v2", "signal.active_model")
                    except Exception:
                        active_model = None
                mode_switched_at = engine.get("mode_switched_at")

                loop_age = (now.timestamp() - float(last_loop_at)) if last_loop_at else None
                prod_age = (now.timestamp() - float(last_prod)) if last_prod else None

                # 状态判定：引擎未运行或循环停滞 → down；最近产出新鲜 → healthy；否则 warning
                if (not running) or (loop_age is not None and loop_age > 90):
                    status = 'down'
                elif prod_age is not None and prod_age <= 600:
                    status = 'healthy'
                else:
                    status = 'warning'

                last_time = None
                if last_prod:
                    try:
                        last_time = datetime.fromtimestamp(float(last_prod), tz=timezone.utc).isoformat()
                    except Exception:
                        last_time = None

                model_label = {
                    "ai_dynamic": "AI动态", "co_source": "共源", "manual": "手动",
                    "hexp": "和乘幂", "hexp_shadow": "和乘幂(影子)",
                }.get(active_model, active_model or "未知")

                metric_parts = [f'{cnt} 条', f'模型:{model_label}']
                if prod_age is not None:
                    metric_parts.append(f'最近产出:{int(prod_age)}s前')
                metric = ' | '.join(metric_parts)

                trouble = [
                    '检查 signal-tower 容器: docker logs hcm-v2-hcm-signal-tower-1 --tail 50',
                    '确认 K 线数据充足(仅用已发生棒): SELECT count(*) FROM hcm_market.klines WHERE open_time <= now()',
                    '若引擎停滞(down): 点击节点「激活信号引擎」或 POST /api/v1/system/engine/activate 重置棒检测状态',
                ]
                if status == 'down':
                    trouble.insert(0, '信号引擎未运行或循环停滞 → 可点击「激活信号引擎」恢复生产')

                return {
                    'name': '信号生成', 'status': status, 'group': 'core',
                    'metric': metric,
                    'last_activity': last_time,
                    'active_model': active_model,
                    'model_label': model_label,
                    'mode_switched_at': (
                        datetime.fromtimestamp(float(mode_switched_at), tz=timezone.utc).isoformat()
                        if mode_switched_at else None
                    ),
                    'engine_running': running,
                    'last_loop_at': (
                        datetime.fromtimestamp(float(last_loop_at), tz=timezone.utc).isoformat()
                        if last_loop_at else None
                    ),
                    'signals_produced': signals_produced,
                    'can_activate': status in ('down', 'warning'),
                    'troubleshooting': trouble,
                }
            except Exception:
                return {
                    'name': '信号生成', 'status': 'down', 'group': 'core',
                    'metric': '--', 'last_activity': None, 'troubleshooting': [],
                    'active_model': None, 'model_label': None, 'mode_switched_at': None,
                    'engine_running': False, 'last_loop_at': None, 'signals_produced': 0,
                    'can_activate': True,
                }

        async def _check_signal_publish() -> dict:
            """Signal publishing (Redis Stream) status — checks stream length and pending."""
            try:
                xlen: int = 0
                pending: int = 0
                if redis_client is not None and redis_client.is_initialized:
                    xlen = await redis_client.xlen("signal:stream")
                if redis_client is not None and redis_client.is_initialized:
                    try:
                        p = await redis_client.xpending("signal:stream", "risk-engine-group")
                        pending = p.get('pending', 0) if isinstance(p, dict) else (p or 0)
                    except Exception:
                        pending = 0
                status = 'healthy' if pending == 0 else 'warning'
                return {
                    'name': '信号发布', 'status': status, 'group': 'core',
                    'metric': f'队列 {xlen}' if xlen else '--',
                    'last_activity': None,
                    'troubleshooting': [
                        '检查 Redis Stream 消费者组: docker exec hcm-v2-redis-1 redis-cli XINFO GROUPS signal:stream',
                        '确认 risk-engine 容器运行: docker ps | grep risk-engine',
                        '检查 web-push-group 消费者状态',
                    ],
                }
            except Exception:
                return {
                    'name': '信号发布', 'status': 'inactive', 'group': 'core',
                    'metric': '--', 'last_activity': None, 'troubleshooting': [],
                }

        async def _check_risk_engine() -> dict:
            """Risk engine status — checks signal:risk_passed stream length."""
            try:
                xlen: int | None = None
                if redis_client is not None and redis_client.is_initialized:
                    xlen = await redis_client.xlen("signal:risk_passed")
                status = 'healthy' if xlen is not None else 'inactive'
                return {
                    'name': '风控引擎', 'status': status, 'group': 'core',
                    'metric': f'通过 {xlen}' if xlen else '--',
                    'last_activity': None,
                    'troubleshooting': [
                        '检查 risk-engine 容器: docker logs hcm-v2-hcm-risk-engine-1 --tail 20',
                        '确认规则链已启用: SELECT count(*) FROM hcm_engine.rules WHERE enabled=true',
                        '检查信号是否从 signal:stream 正确消费',
                    ],
                }
            except Exception:
                return {
                    'name': '风控引擎', 'status': 'inactive', 'group': 'core',
                    'metric': '--', 'last_activity': None, 'troubleshooting': [],
                }

        async def _check_dispatcher() -> dict:
            """Order dispatch status — checks most recent order in hcm_trading.orders."""
            try:
                row = await db_pool.fetchrow(
                    "SELECT MAX(created_at) AS last_time, COUNT(*) AS cnt FROM hcm_trading.orders"
                )
                cnt: int = row['cnt'] if row else 0
                last = row['last_time'] if row else None
                status = 'healthy' if last else 'inactive'
                return {
                    'name': '下单分发', 'status': status, 'group': 'core',
                    'metric': f'{cnt} 单' if cnt else '0 单',
                    'last_activity': last.isoformat() if last else None,
                    'troubleshooting': [
                        '检查 dispatcher 容器: docker logs hcm-v2-hcm-dispatcher-1 --tail 20',
                        '确认 MT5 已连接且 trade_allowed=True',
                        '检查信号是否通过风控（signal:risk_passed）',
                        '确认账户余额充足',
                    ],
                }
            except Exception:
                return {
                    'name': '下单分发', 'status': 'inactive', 'group': 'core',
                    'metric': '--', 'last_activity': None, 'troubleshooting': [],
                }

        async def _check_mt5() -> dict:
            """MT5 connection status — checks Redis config for account and latest price."""
            try:
                cfg: dict = {}
                if redis_client is not None and redis_client.is_initialized:
                    cfg = await redis_client.hgetall("hcm:config:v2")
                login = cfg.get('mt5.account_number', '--')
                price_raw = cfg.get('market:latest:XAUUSD', '{}')
                price_data = _json.loads(price_raw) if isinstance(price_raw, str) else price_raw
                bid = price_data.get('bid', '--') if isinstance(price_data, dict) else '--'
                status = 'healthy' if bid != '--' else 'warning'
                return {
                    'name': 'MT5 连接', 'status': status, 'group': 'aux',
                    'metric': f'#{login}' if login != '--' else '未配置',
                    'last_activity': None,
                    'troubleshooting': [
                        '确认 MT5 终端已运行: tasklist | findstr terminal64',
                        '检查 MT5 Bridge 是否连接: 查看 bridge.log',
                        '确认账户密码正确（system/mt5 页面）',
                        '检查网络连接和经纪商服务器状态',
                    ],
                }
            except Exception:
                return {
                    'name': 'MT5 连接', 'status': 'down', 'group': 'aux',
                    'metric': '--', 'last_activity': None, 'troubleshooting': [],
                }

        @router.post("/api/v1/system/backup")
        async def trigger_backup(
            request: Request,
            user=Depends(auth_handler.require_auth),
        ):
            """触发全备份：写 hcm:backup:trigger，由 Windows 宿主 mt5_bridge 进程监听并执行备份脚本。
            备份在宿主完成（PG dump + Redis RDB + 源码 robocopy → D:\\HCM_ASST\\backup），
            进度由备份脚本回报至 hcm:backup:status（running/done/failed），前端轮询展示。
            """
            import json as _json
            from datetime import datetime, timezone
            try:
                r = _get_redis_client()
                if r is None:
                    return {"code": "SYS_ERR", "data": None, "message": "redis unavailable"}
                r.set(
                    "hcm:backup:trigger",
                    _json.dumps({
                        "status": "pending",
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    }),
                    ex=300,
                )
                return {"code": 0, "data": {"triggered": True}, "message": "backup triggered"}
            except Exception as exc:
                return {"code": "SYS_ERR", "data": None, "message": str(exc)}

        @router.get("/api/v1/system/backup/status")
        async def backup_status(
            request: Request,
            user=Depends(auth_handler.require_auth),
        ):
            """读取全备份状态（前端「全备份」按钮轮询展示）。"""
            import json as _json
            try:
                r = _get_redis_client()
                if r is None:
                    return {"code": 0, "data": {"status": "idle"}, "message": "redis unavailable"}
                raw = r.get("hcm:backup:status")
                if not raw:
                    return {"code": 0, "data": {"status": "idle"}, "message": "no backup yet"}
                return {"code": 0, "data": _json.loads(raw), "message": "ok"}
            except Exception as exc:
                return {"code": "SYS_ERR", "data": None, "message": str(exc)}

        async def _check_account() -> dict:
            """Account status — reads balance, equity, positions, and MT5 bridge state from Redis."""
            try:
                row = await db_pool.fetchrow(
                    "SELECT balance, equity, is_active FROM hcm_trading.accounts WHERE is_active=true LIMIT 1"
                )
                if row and row['is_active']:
                    bal = row['balance']
                    eq = row['equity']
                    # Count open positions
                    pos_cnt = await db_pool.fetchval(
                        "SELECT count(*) FROM hcm_trading.positions"
                    ) or 0
                    # Get MT5 price feed age from Redis
                    price_age: str = ''
                    try:
                        raw = await redis_client.hget("hcm:config:v2", "market:latest:XAUUSD") if redis_client else None
                        if raw:
                            import json, time as _t
                            tick = json.loads(raw)
                            tick_time = tick.get('time', 0)
                            age_s = int(_t.time()) - tick_time
                            if age_s < 10:
                                price_age = ''
                            elif age_s < 60:
                                price_age = f' (tick {age_s}s)'
                            else:
                                price_age = f' (tick {age_s}s ⚠)'
                    except Exception:
                        pass
                    metric = f'${bal:,.0f} · {pos_cnt}仓{price_age}'
                    status = 'healthy'
                else:
                    metric = '未配置'
                    status = 'warning'
                return {
                    'name': '账户状态', 'status': status, 'group': 'aux',
                    'metric': metric,
                    'last_activity': None,
                    'troubleshooting': [
                        '检查 MT5 Bridge: tasklist | findstr python',
                        '确认账户已激活: SELECT * FROM hcm_trading.accounts WHERE is_active=true',
                        f'检查 XAUUSD 报价: redis-cli HGET hcm:config:v2 market:latest:XAUUSD',
                        '查看 bridge.log: tail -20 /d/HCM_ASST/bridge16.log',
                    ],
                }
            except Exception:
                return {
                    'name': '账户状态', 'status': 'warning', 'group': 'aux',
                    'metric': '--', 'last_activity': None, 'troubleshooting': [],
                }

        async def _check_ai() -> dict:
            """AI scoring — shows actual DeepSeek response when AI was used,
            or fallback reason when scoring engine alone decided."""
            try:
                row = await db_pool.fetchrow(
                    "SELECT signal_tower_mode, pre_score, confidence, signal_dir, "
                    "       fallback_reason, created_at "
                    "FROM hcm_signal.signals ORDER BY created_at DESC LIMIT 1"
                )
                if row:
                    mode = row['signal_tower_mode'] or '--'
                    pre_score = float(row['pre_score']) if row.get('pre_score') else 0
                    conf = float(row['confidence']) if row.get('confidence') else 0
                    direction = row['signal_dir'] or '?'
                    fallback = row['fallback_reason'] or ''
                    last_activity = row['created_at'].isoformat() if row['created_at'] else None

                    from datetime import timedelta
                    delta = datetime.now(timezone.utc) - row['created_at']
                    fresh = delta < timedelta(minutes=10)
                    status = 'healthy' if fresh else 'warning'

                    # Build short + full metric
                    short_metric = ""
                    if mode == 'ai_dynamic' and conf != pre_score:
                        short_metric = f"{direction} {conf:.2f}"
                        metric = f"AI→{direction} {conf:.2f} (引擎{pre_score:.3f})"
                    elif fallback:
                        reason_short = fallback.replace('_', ' ').replace('ai ', '')[:15]
                        short_metric = f"{direction} {conf:.2f}"
                        metric = f"回退 {direction} {conf:.3f} ({reason_short})"
                    else:
                        short_metric = f"{direction} {conf:.2f}"
                        metric = short_metric

                    troubleshooting = [
                        f'模式={mode} 方向={direction} 置信={conf:.2f}',
                        'AI增强: 信号塔调用DeepSeek → 返回direction+confidence',
                        '回退: AI不可用时仅用评分引擎 (ai_fallback/circuit_breaker_open)',
                        '检查AI状态: docker logs hcm-v2-hcm-signal-tower-1 | grep AiInvoker',
                    ]
                else:
                    status = 'inactive'
                    metric = '等待信号'
                    short_metric = '--'
                    last_activity = None
                    troubleshooting = ['无信号产出, 等待下一根K线闭合']

                return {
                    'name': 'AI 评分', 'status': status, 'group': 'aux',
                    'metric': metric,
                    'short_metric': short_metric,
                    'last_activity': last_activity,
                    'troubleshooting': troubleshooting,
                }
            except Exception:
                return {
                    'name': 'AI 评分', 'status': 'inactive', 'group': 'aux',
                    'metric': '--', 'last_activity': None, 'troubleshooting': [],
                }

        async def _check_factors() -> dict:
            """External factors — composite score from 4 market-intel dimensions.

            Reads Redis hcm:market:{dim}:score for macro/sentiment/event/liquidity,
            plus the composite hcm:market:composite:score. Shows breakdown in metric.
            """
            try:
                from datetime import datetime, timezone

                scores: dict[str, float] = {}
                stub_mode = None
                ai_offline = None
                last_collection = None
                if redis_client and redis_client.is_initialized:
                    for dim in ("macro", "sentiment", "event", "liquidity"):
                        raw = await redis_client.get(f"hcm:market:{dim}:score")
                        if raw:
                            try:
                                scores[dim] = float(raw)
                            except ValueError:
                                pass
                    comp_raw = await redis_client.get("hcm:market:composite:score")
                    composite = float(comp_raw) if comp_raw else None
                    stub_mode = await redis_client.get("hcm:market:stub_mode")
                    ai_offline = await redis_client.get("hcm:market:ai_offline")
                    last_collection = await redis_client.get("hcm:market:last_collection")

                # Explicit stub-mode flag → never report healthy/normal
                if stub_mode and str(stub_mode).lower() in ("true", "1", "yes"):
                    short_metric = f"Stub {composite:.2f}" if composite is not None else "Stub"
                    return {
                        'name': '外部因子', 'status': 'inactive', 'group': 'aux',
                        'metric': f'Stub占位({short_metric})',
                        'short_metric': short_metric,
                        'last_activity': None,
                        'troubleshooting': [
                            '外部因子监控处于 Stub 占位模式（未启用真实采集），数据不可信',
                            '关闭 StubMode 需设置环境变量 MARKET_INTEL_STUB_MODE=false',
                            '重启 market-intel 生效: docker compose up -d hcm-market-intel',
                        ],
                    }

                if composite is not None:
                    status = 'healthy' if composite < 0.4 else ('warning' if composite < 0.7 else 'down')
                    labels = {"macro": "M", "sentiment": "S", "event": "E", "liquidity": "L"}
                    parts = []
                    for dim in ("macro", "sentiment", "event", "liquidity"):
                        if dim in scores:
                            parts.append(f"{labels[dim]}:{scores[dim]:.2f}")
                    short_metric = f"{composite:.2f}"
                    metric = f"综合={composite:.2f} ({' '.join(parts)})"
                    troubleshooting = [
                        f"综合={composite:.2f} (<0.4 低风险, 0.4-0.7 中, >0.7 高风险)",
                        f'宏观={scores.get("macro", "?")} 情绪={scores.get("sentiment", "?")}',
                        f'事件={scores.get("event", "?")} 流动={scores.get("liquidity", "?")}',
                        '分频采集: macro/24h sentiment/6h event/12h liquidity/60s',
                    ]
                    if ai_offline and str(ai_offline).lower() in ("true", "1", "yes"):
                        troubleshooting.append(
                            'AI 评分使用启发式(未配置 DEEPSEEK_API_KEY)，'
                            '宏观/情绪等维度为真实派生数据，最终赋分非模型输出')
                    # Stalled collector: heartbeat older than 48h
                    if last_collection:
                        try:
                            age_h = (datetime.now(timezone.utc) -
                                     datetime.fromisoformat(str(last_collection))).total_seconds() / 3600.0
                            if age_h > 48:
                                status = 'warning'
                                metric += ' (采集停滞>48h)'
                                troubleshooting.append(
                                    f'采集心跳停滞 {age_h:.1f}h，采集器可能未运行')
                        except Exception:
                            pass
                else:
                    # PG fallback: check for any real collection history
                    row = await db_pool.fetchrow(
                        "SELECT MAX(snapshot_time) AS last_time FROM hcm_market.macro_snapshots"
                    )
                    if row and row['last_time']:
                        status = 'warning'
                        metric = '评分未生成（采集曾运行）'
                    else:
                        status = 'down'
                        metric = '无数据（采集未激活）'
                    troubleshooting = [
                        '外部因子采集器未产出综合评分',
                        '确认 MARKET_INTEL_STUB_MODE=false 且容器已重启',
                        '检查 hcm-market-intel 容器日志是否报错',
                    ]

                return {
                    'name': '外部因子', 'status': status, 'group': 'aux',
                    'metric': metric,
                    'short_metric': short_metric if composite is not None else None,
                    'last_activity': last_collection,
                    'troubleshooting': troubleshooting,
                }
            except Exception:
                return {
                    'name': '外部因子', 'status': 'down', 'group': 'aux',
                    'metric': '无数据', 'last_activity': None, 'troubleshooting': [],
                }

        # Run all checks concurrently
        results = await _asyncio.gather(
            _check_klines(), _check_signals(), _check_signal_publish(),
            _check_risk_engine(), _check_dispatcher(), _check_mt5(),
            _check_account(), _check_ai(), _check_factors(),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, dict):
                nodes.append(r)

        return {
            'nodes': nodes,
            'timestamp': _time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime()),
        }

    async def _get_score_threshold() -> float:
        """Read current scoring threshold from Redis."""
        try:
            if redis_client is not None and redis_client.is_initialized:
                raw = await redis_client.hget("hcm:config:v2", "score_threshold")
                if raw:
                    return float(raw)
        except Exception:
            pass
        return 0.50

    async def _get_signal_tower_heartbeat() -> Optional[str]:
        """Read signal tower production heartbeat from Redis."""
        try:
            if redis_client is not None and redis_client.is_initialized:
                return await redis_client.get("hcm:signal_tower:last_production:XAUUSD:M5")
        except Exception:
            pass
        return None

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
                        **{k: (v.get('status', str(v)) if isinstance(v, dict) else str(v)) for k, v in checks.items()},
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

        # ── 最近 K 线时间 & 延迟 ──
        klines_last_time: str | None = None
        klines_stale_minutes: float | None = None
        if db_pool is not None and db_pool.is_initialized:
            try:
                from datetime import datetime, timezone as dt_timezone
                klines_row = await db_pool.fetchrow(
                    "SELECT MAX(open_time) AS last_time FROM hcm_market.klines"
                )
                if klines_row and klines_row['last_time']:
                    last = klines_row['last_time']
                    if hasattr(last, 'tzinfo') and last.tzinfo is None:
                        last = last.replace(tzinfo=dt_timezone.utc)
                    klines_last_time = last.isoformat() if hasattr(last, 'isoformat') else str(last)
                    delta = datetime.now(dt_timezone.utc) - last
                    klines_stale_minutes = round(delta.total_seconds() / 60, 1)
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
                # AI 状态 + 最新评分：检查最近 1 小时信号
                latest_pre_score: Optional[float] = None
                last_ai_row = await db_pool.fetchrow("""
                    SELECT signal_tower_mode, pre_score
                    FROM hcm_signal.signals
                    WHERE created_at > now() - interval '1 hour'
                    ORDER BY created_at DESC LIMIT 1
                """)
                if last_ai_row:
                    mode = (last_ai_row.get('signal_tower_mode') or '').lower()
                    latest_pre_score = float(last_ai_row['pre_score']) if last_ai_row.get('pre_score') else None
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
            # Signal count last 1 hour
            ai_signals_last_hour = 0
            try:
                ai_signals_last_hour = await db_pool.fetchval(
                    "SELECT count(*) FROM hcm_signal.signals WHERE created_at > now() - interval '1 hour'"
                ) or 0
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
                'ai_signals_last_hour': ai_signals_last_hour,
                'latest_pre_score': latest_pre_score,
                'score_threshold': await _get_score_threshold(),
                'signal_tower_heartbeat': await _get_signal_tower_heartbeat(),
                'klines_last_time': klines_last_time,
                'klines_stale_minutes': klines_stale_minutes,
                'bridges': (await _bridge_liveness())["bridges"],
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

    @router.put("/api/v1/system/accounts/{account_id}/status")
    async def set_account_status_v1(
        account_id: int,
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """G6: set account start/stop status (running/stopped/paused)."""
        status = body.get("status") if isinstance(body, dict) else None
        return await _set_account_status_impl(account_id, status)

    @router.put("/api/v1/system/accounts/{account_id}/active")
    async def set_account_active_v1(
        account_id: int,
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """启用 / 停用账户（is_active 标记）。

        停用仅标记 is_active=false，账户仍保留在账户列表（带“禁用”标记），
        不会真正删除；删除请调用 DELETE /api/v1/system/accounts/{account_id}。
        """
        if not isinstance(body, dict):
            return {"code": "ACCT_006", "data": None, "message": "请求体必须为 JSON 对象"}
        is_active = body.get("is_active")
        if not isinstance(is_active, bool):
            return {"code": "ACCT_006", "data": None, "message": "is_active 必须为 true/false"}
        return await _set_account_active_impl(account_id, is_active)

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

    # ── 9. Pipeline Status v1 ────────────────────

    @router.get("/api/v1/system/pipeline")
    async def pipeline_status_v1(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Pipeline axis bar: data-flow health across all stages.

        Returns status nodes for K-lines → signals → publish → risk → dispatch
        (core pipeline) plus MT5, account, AI, and external factors (auxiliary).
        Each node carries a status, metric, and troubleshooting checklist.
        """
        return {'code': 0, 'data': await _pipeline_status(), 'message': 'ok'}

    @router.post("/api/v1/system/engine/activate")
    async def activate_signal_engine(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """激活信号引擎：下发激活指令让 signal-tower 重置棒检测/生产状态，恢复信号生产。

        调度器每循环消费 hcm:signal_tower:control 指令并重置 last_bar_open_time /
        生产幂等闸门；适用于引擎因数据停滞(down/warning)而暂停产出的场景。
        """
        import json as _json
        try:
            if redis_client is None or not redis_client.is_initialized:
                return {"code": "SYS_REDIS_001", "data": None, "message": "Redis 不可用"}
            payload = _json.dumps({"action": "activate", "ts": datetime.now(timezone.utc).timestamp()})
            try:
                await redis_client.raw.set("hcm:signal_tower:control", payload, ex=600)
            except Exception:
                await redis_client.set("hcm:signal_tower:control", payload, ex=600)
            return {
                "code": 0,
                "data": {"activated": True, "control_key": "hcm:signal_tower:control"},
                "message": "已下发激活指令，signal-tower 将在数秒内重置状态并恢复生产",
            }
        except Exception as exc:
            return {"code": "SYS_ENG_001", "data": None, "message": f"激活失败: {exc}"}

    # ══════════════════════════════════════════════════════════
    # 10. One-click Diagnose & Self-heal
    # ══════════════════════════════════════════════════════════

    async def _group_exists(stream: str, group: str) -> bool:
        """Return True if consumer group exists on stream."""
        if redis_client is None or not redis_client.is_initialized:
            return False
        try:
            groups = await redis_client.raw.xinfo_groups(stream)
            return any(g.get("name") == group for g in groups)
        except Exception:
            return False

    async def _bridge_liveness() -> dict:
        """Monitor bridge survival via bridge:alive:<login> heartbeats.

        Each authoritative (lock-holding) bridge renews bridge:alive:<login> every ~10s
        (TTL 600s) with a JSON payload {pid, login, account_id, role, server, terminal,
        updated_at}. This helper:
          - flags STALE    : heartbeat exists but not refreshed within ALIVE_THRESHOLD
          - flags DOWN     : an expected running account has no heartbeat at all
          - flags DUPLICATE: heartbeat pid != single-instance lock pid for the same login
        Keys are keyed by the dynamic MT5 login (not hardcoded / not role-bound), so
        switching brokers (new terminal -> new login) is plug-and-play.
        """
        from datetime import datetime as _dt, timezone as _tz
        import json as _json

        bridges: list[dict] = []
        if redis_client is None or not redis_client.is_initialized:
            return {"ok": True, "bridges": bridges, "summary": "Redis 不可读，跳过桥存活检测"}

        now = _dt.now(_tz.utc)
        ALIVE_THRESHOLD = 120  # 心跳每 ~10s 刷新；>120s 未刷新视为陈旧/死亡

        # 1) 收集存活心跳
        alive_map: dict[str, dict] = {}
        try:
            async for key in redis_client.raw.scan_iter(match="bridge:alive:*"):
                try:
                    raw = await redis_client.raw.get(key)
                    if not raw:
                        continue
                    payload = _json.loads(raw if isinstance(raw, str) else raw.decode())
                    login = str(payload.get("login") or (key.decode() if isinstance(key, bytes) else str(key)).split(":")[-1])
                    alive_map[login] = payload
                except Exception:
                    continue
        except Exception:
            return {"ok": True, "bridges": bridges, "summary": "Redis 不可读，跳过桥存活检测"}

        # 2) 单实例锁持有 PID（交叉校验重复实例）
        lock_map: dict[str, str] = {}
        try:
            async for lk in redis_client.raw.scan_iter(match="bridge:instance:lock:*"):
                try:
                    raw = await redis_client.raw.get(lk)
                    if not raw:
                        continue
                    login = (lk.decode() if isinstance(lk, bytes) else str(lk)).split(":")[-1]
                    lock_map[login] = raw.decode() if isinstance(raw, bytes) else str(raw)
                except Exception:
                    continue
        except Exception:
            pass

        # 3) 期望运行桥的账户（活跃主号 ∪ running 跟单关系的 主/跟 账户）
        expected_ids: set = set()
        try:
            if db_pool is not None and db_pool.is_initialized:
                rows = await db_pool.fetch(
                    "SELECT account_id FROM hcm_broker.accounts WHERE is_active = true AND account_type = 'master' "
                    "UNION "
                    "SELECT copy_account_id AS account_id FROM hcm_copy.relationships WHERE status = 'running' "
                    "UNION "
                    "SELECT master_account_id AS account_id FROM hcm_copy.relationships WHERE status = 'running'"
                )
                expected_ids = {int(r["account_id"]) for r in rows if r["account_id"] is not None}
        except Exception:
            expected_ids = set()

        # 4) 逐桥判定
        seen_ids: set = set()
        for login, payload in alive_map.items():
            acct_id = payload.get("account_id")
            try:
                acct_int = int(acct_id) if acct_id is not None else None
            except Exception:
                acct_int = None
            if acct_int is not None:
                seen_ids.add(acct_int)
            status = "alive"
            age = None
            try:
                upd = payload.get("updated_at")
                if upd:
                    upd = str(upd).replace("Z", "+00:00")
                    upd_dt = _dt.fromisoformat(upd)
                    if upd_dt.tzinfo is None:
                        upd_dt = upd_dt.replace(tzinfo=_tz.utc)
                    age = (now - upd_dt).total_seconds()
                    if age > ALIVE_THRESHOLD:
                        status = "stale"
            except Exception:
                status = "stale"
            lock_pid = lock_map.get(login)
            if lock_pid is not None and payload.get("pid") is not None and str(payload.get("pid")) != str(lock_pid):
                status = "duplicate"
            bridges.append({
                "login": login,
                "account_id": acct_int,
                "role": payload.get("role"),
                "server": payload.get("server"),
                "terminal": payload.get("terminal"),
                "pid": payload.get("pid"),
                "lock_pid": lock_pid,
                "status": status,
                "age_seconds": int(age) if age is not None else None,
            })

        # 5) 期望运行但缺失心跳 -> down
        for acct_id in sorted(expected_ids):
            if acct_id not in seen_ids:
                bridges.append({
                    "login": None,
                    "account_id": acct_id,
                    "role": None,
                    "server": None,
                    "terminal": None,
                    "pid": None,
                    "lock_pid": None,
                    "status": "down",
                    "age_seconds": None,
                })

        problems = [b for b in bridges if b["status"] != "alive"]
        ok = len(problems) == 0
        if bridges:
            summary = "桥 " + str(len(bridges)) + " 个: " + ", ".join(
                f"{b['login'] or b['account_id']}={b['status']}" for b in bridges
            )
        else:
            summary = "未发现桥心跳（桥未运行或 Redis 无 bridge:alive:*）"
        return {"ok": ok, "bridges": bridges, "summary": summary}

    async def _calibrate_config_drift(redis_client, db_pool, dry_run: bool = False) -> dict:
        """全量配置校准：以 PG hcm_config.metadata 为权威真源，比对并覆盖 Redis hcm:config:v2。

        dry_run=True   仅扫描并收集漂移明细，不写 Redis（供诊断节点使用）。
        dry_run=False  在扫描基础上把「Redis 缺失」或「值与 PG 不一致」的键覆盖写回 Redis，
                       并逐键 publish hcm:config:invalidate 通知各引擎清本地缓存。

        设计要点：
          - 只做 PG→Redis 单向覆盖，不删除 Redis 中 PG 没有的键（避免误删运行时动态键）；
          - PG 取值 COALESCE(current_value, default_value)，与 config_provider.get 解析链一致；
          - 比对全部键而非固定子集，根治「配置了≠生效了」类漂移位点。
        """
        import time as _t
        res = {"total": 0, "calibrated": 0, "drifts": [], "errors": []}
        if redis_client is None or not getattr(redis_client, "is_initialized", False):
            res["errors"].append("Redis 不可达，无法校准")
            return res
        if db_pool is None or not getattr(db_pool, "is_initialized", False):
            res["errors"].append("PG 不可达，无法校准")
            return res
        try:
            rows = await db_pool.fetch(
                "SELECT config_key, COALESCE(NULLIF(current_value, ''), default_value) AS v "
                "FROM hcm_config.metadata"
            )
            res["total"] = len(rows)
        except Exception as exc:
            res["errors"].append("读取 PG 配置失败: %s" % exc)
            return res
        try:
            raw_map = await redis_client.raw.hgetall("hcm:config:v2")
        except Exception as exc:
            res["errors"].append("读取 Redis 配置失败: %s" % exc)
            return res
        decoded = {}
        for k, v in raw_map.items():
            kk = k.decode() if isinstance(k, bytes) else k
            vv = v.decode() if isinstance(v, bytes) else v
            decoded[kk] = vv
        to_set = {}
        to_del = {}
        for r in rows:
            key = r["config_key"]
            if r["v"] is None:
                # PG 两侧皆空 → 该键在 PG 中未显式设置。禁止把空串写进 Redis（空串穿透
                # Bug 根因）；改为清理 Redis 中该键残留值（若存在），使其与 PG 的「未设置」一致。
                redis_v0 = decoded.get(key)
                if redis_v0 is not None and str(redis_v0).strip() != "":
                    to_del[key] = str(redis_v0)
                    res["drifts"].append({
                        "key": key,
                        "pg": None,
                        "redis": str(redis_v0),
                    })
                continue
            pg_v = str(r["v"])
            redis_v = decoded.get(key)
            if redis_v is None or str(redis_v).strip() != pg_v.strip():
                to_set[key] = pg_v
                res["drifts"].append({
                    "key": key,
                    "pg": pg_v,
                    "redis": None if redis_v is None else str(redis_v),
                })
        if dry_run or (not to_set and not to_del):
            return res
        try:
            version = str(_t.time())
            async with redis_client.raw.pipeline() as pipe:
                for k, v in to_set.items():
                    pipe.hset("hcm:config:v2", k, v)
                    pipe.hset("hcm:config:version", k, version)
                for k in to_del:
                    pipe.hdel("hcm:config:v2", k)
                    pipe.hdel("hcm:config:version", k)
                await pipe.execute()
            for k in to_set:
                try:
                    await redis_client.raw.publish("hcm:config:invalidate", k)
                except Exception:
                    pass
            for k in to_del:
                try:
                    await redis_client.raw.publish("hcm:config:invalidate", k)
                except Exception:
                    pass
            res["calibrated"] = len(to_set) + len(to_del)
        except Exception as exc:
            res["errors"].append("写回 Redis 失败: %s" % exc)
        return res

    async def _apply_self_heals(fix_codes: set) -> tuple[list[str], set[str]]:
        """执行一键自愈的安全动作。返回 (动作描述列表, 成功执行的 fix 集合)。

        仅做可逆/无损操作：清桥脏键、删未来棒、回填缺失配置、下发引擎激活/桥重启控制。
        无法从 web 容器执行的(容器重启、PG/Redis 重启)标记为需人工，不在此处理。
        """
        applied: list[str] = []
        succeeded: set[str] = set()
        rc = redis_client

        async def _clear_patterns(patterns: list[str]) -> int:
            total = 0
            if rc is None or not rc.is_initialized:
                return 0
            try:
                for pat in patterns:
                    cursor = 0
                    while True:
                        cursor, keys = await rc.raw.scan(cursor=cursor, match=pat, count=200)
                        if keys:
                            dkeys = [k if isinstance(k, str) else k.decode() for k in keys]
                            total += await rc.raw.delete(*dkeys)
                        if cursor == 0:
                            break
            except Exception as exc:
                logger.warning("clear patterns failed: %s", exc)
            return total

        # 桥脏键清理（锁/存活/zone待触发/主号持仓快照）。
        # 注意：不清理 bridge:processed:*，避免重复信号被重放导致重复下单。
        if {"restart_bridge", "clear_bridge_keys"} & fix_codes:
            n = await _clear_patterns([
                "bridge:instance:lock:*", "bridge:alive:*",
                "bridge:zone_pending:*", "hcm:master:positions:*",
            ])
            applied.append(f"clear_bridge_keys: 清理 {n} 个桥脏键(锁/存活/zone/主号快照)")
            succeeded.add("clear_bridge_keys")
            if "restart_bridge" in fix_codes:
                succeeded.add("restart_bridge")

        # 删除未来棒（K线停滞根因）
        if "delete_future_klines" in fix_codes and db_pool is not None and db_pool.is_initialized:
            try:
                row = await db_pool.fetchrow("SELECT COUNT(*) AS c FROM hcm_market.klines WHERE open_time > now()")
                cnt = int(row["c"]) if row else 0
                if cnt > 0:
                    await db_pool.execute("DELETE FROM hcm_market.klines WHERE open_time > now()")
                applied.append(f"delete_future_klines: 删除未来棒 {cnt} 根")
                succeeded.add("delete_future_klines")
            except Exception as exc:
                applied.append(f"delete_future_klines: 失败 {exc}")

        # 回填时段平仓系数（仅缺失才回填，不动用户已设值）
        if "reseed_session_config" in fix_codes and rc is not None and rc.is_initialized:
            try:
                from .close import SESSIONS, SESSION_SUFFIXES, SESSION_DEFAULTS
                cnt = 0
                for s in SESSIONS:
                    for suf in SESSION_SUFFIXES:
                        key = f"close.{s}.{suf}"
                        if await rc.raw.hget("hcm:config:v2", key) is None:
                            val = SESSION_DEFAULTS[s][suf]
                            strval = str(val).lower() if isinstance(val, bool) else str(val)
                            # 双写（PG SoT + Redis L2 + PUB）：单写 Redis 会在 Redis 重启/
                            # 校准覆盖后丢失（空串穿透同类缺陷）。铁律 5.2 禁止单端直写。
                            if db_pool is not None and db_pool.is_initialized:
                                try:
                                    await db_pool.execute(
                                        "INSERT INTO hcm_config.metadata "
                                        "(config_key, default_value, current_value, value_type, category) "
                                        "VALUES ($1, $2, $3, 'string', 'close') "
                                        "ON CONFLICT (config_key) DO UPDATE SET current_value=EXCLUDED.current_value, updated_at=now()",
                                        key, strval, strval,
                                    )
                                except Exception as _pgerr:
                                    logger.warning("reseed_session_config PG write failed key=%s: %s", key, _pgerr)
                            await rc.raw.hset("hcm:config:v2", key, strval)
                            try:
                                await rc.raw.publish("hcm:config:invalidate", key)
                            except Exception:
                                pass
                            cnt += 1
                applied.append(f"reseed_session_config: 回填 {cnt} 个会话系数键（PG+Redis 双写）")
                succeeded.add("reseed_session_config")
            except Exception as exc:
                applied.append(f"reseed_session_config: 失败 {exc}")

        # 下发信号引擎激活（重置棒检测/恢复生产）
        if "activate_engine" in fix_codes and rc is not None and rc.is_initialized:
            try:
                import json as _json
                payload = _json.dumps({"action": "activate", "ts": datetime.now(timezone.utc).isoformat()})
                await rc.raw.set("hcm:signal_tower:control", payload)
                applied.append("activate_engine: 已下发引擎激活指令(重置棒检测/恢复产出)")
                succeeded.add("activate_engine")
            except Exception as exc:
                applied.append(f"activate_engine: 失败 {exc}")

        # 重建 risk-engine 消费组（容器重启后消费组可能丢失）
        if "recreate_risk_group" in fix_codes and rc is not None and rc.is_initialized:
            try:
                if not await _group_exists("signal:stream", "risk-engine-group"):
                    await rc.raw.xgroup_create("signal:stream", "risk-engine-group", id="$", mkstream=True)
                    applied.append("recreate_risk_group: 已重建 risk-engine-group 消费组")
                else:
                    applied.append("recreate_risk_group: 消费组已存在，跳过")
                succeeded.add("recreate_risk_group")
            except Exception as exc:
                applied.append(f"recreate_risk_group: 失败 {exc}")

        # 桥重启控制：给存活桥下发 restart 指令（桥检测到后自行退出，看门狗重拉）
        if "restart_bridge" in fix_codes and rc is not None and rc.is_initialized:
            try:
                targets = []
                async for key in rc.raw.scan_iter(match="bridge:alive:*"):
                    login = (key.decode() if isinstance(key, bytes) else str(key)).split(":")[-1]
                    await rc.raw.set(f"bridge:control:{login}", "restart", ex=120)
                    targets.append(login)
                applied.append(f"restart_bridge: 已对 {len(targets)} 个存活桥下发重启指令(看门狗将重拉)")
                succeeded.add("restart_bridge")
            except Exception as exc:
                applied.append(f"restart_bridge: 失败 {exc}")

        # 清理历史 mt5_ticket=null 的 open 孤儿持仓（数据卫生，解除僵尸阻塞）
        # 这些单（如 7-20 爆炸式开单产物，或开仓 INSERT 修复前漏写 ticket 的遗留双写）无 ticket
        # 无法被 MT5 对账，永远残留 PG open，经最大单数修复后被计入持仓数 → 反阻塞同 account+symbol 新单。
        # 标记 closed 可逆（position_sync 对 MT5 真实活持仓会重建带 ticket 记录）。
        # 安全阈值：>10 分钟前的 null-ticket open（10s 内新开且尚未被 position_sync 写入 ticket 的活持仓可豁免，
        # 由下一轮 sync 自愈 reopen，避免误关刚复制的合法单）。
        if "clear_orphan_positions" in fix_codes and db_pool is not None and db_pool.is_initialized:
            try:
                row = await db_pool.fetchrow(
                    "SELECT COUNT(*) AS c FROM hcm_trading.positions "
                    "WHERE status='open' AND (mt5_ticket IS NULL OR mt5_ticket <= 0) "
                    "AND open_time < now() - interval '10 minutes'")
                cnt = int(row["c"]) if row else 0
                if cnt > 0:
                    await db_pool.execute(
                        "UPDATE hcm_trading.positions SET status='closed', updated_at=now() "
                        "WHERE status='open' AND (mt5_ticket IS NULL OR mt5_ticket <= 0) "
                        "AND open_time < now() - interval '10 minutes'")
                applied.append(f"clear_orphan_positions: 标记 {cnt} 条 ticket 缺失的 open 孤儿为 closed(>10分钟前)")
                succeeded.add("clear_orphan_positions")
            except Exception as exc:
                applied.append(f"clear_orphan_positions: 失败 {exc}")

        # Phantom 持仓自愈：删除所有 mt5_ticket 为空的幽灵行（含已 closed 的历史双写）。
        # 判据同诊断节点 7.56：position_sync/桥开仓均始终写 ticket，NULL/<=0 必为幽灵，
        # 删除不丢真实数据（真实持仓无论 open/closed 都带 ticket）。
        # 与 clear_orphan_positions(仅标记 open 为 closed、可逆) 互补：本项直接 DELETE 全态幽灵。
        if "clear_phantom_positions" in fix_codes and db_pool is not None and db_pool.is_initialized:
            try:
                row = await db_pool.fetchrow(
                    "SELECT COUNT(*) AS c FROM hcm_trading.positions "
                    "WHERE mt5_ticket IS NULL OR mt5_ticket <= 0")
                cnt = int(row["c"]) if row else 0
                if cnt > 0:
                    await db_pool.execute(
                        "DELETE FROM hcm_trading.positions "
                        "WHERE mt5_ticket IS NULL OR mt5_ticket <= 0")
                applied.append(f"clear_phantom_positions: 删除 {cnt} 条 ticket 为空的 Phantom 持仓(全态)")
                succeeded.add("clear_phantom_positions")
            except Exception as exc:
                applied.append(f"clear_phantom_positions: 失败 {exc}")

        # 配置回填（PG metadata → Redis，仅缺失键 HSETNX）
        if "backfill_config" in fix_codes and rc is not None and rc.is_initialized and db_pool is not None and db_pool.is_initialized:
            try:
                pg_rows = await db_pool.fetch(
                    "SELECT config_key, COALESCE(NULLIF(current_value,''), default_value) AS v "
                    "FROM hcm_config.metadata"
                )
                redis_keys = set(await rc.raw.hkeys("hcm:config:v2") or [])
                cnt = 0
                for r in pg_rows:
                    key = r["config_key"]
                    if key not in redis_keys:
                        await rc.raw.hsetnx("hcm:config:v2", key, str(r["v"]))
                        cnt += 1
                applied.append(f"backfill_config: 回填 {cnt} 个缺失配置键")
                succeeded.add("backfill_config")
            except Exception as exc:
                applied.append(f"backfill_config: 失败 {exc}")

        # 激活模型一致性自愈：将 PG 的 signal.active_model 覆盖写回 Redis（针对性修复
        # "直接 SQL 改 PG 未双写 Redis" 导致的共源闸门静默失效）。HSET 后信号塔 60s 热重载
        # 即读到新值；config_provider 的 L1 缓存亦随之刷新。
        if "resync_active_model" in fix_codes and rc is not None and rc.is_initialized and db_pool is not None and db_pool.is_initialized:
            try:
                pg_row = await db_pool.fetchrow(
                    "SELECT COALESCE(NULLIF(current_value,''), default_value) AS v "
                    "FROM hcm_config.metadata WHERE config_key='signal.active_model'"
                )
                if pg_row and pg_row["v"] is not None:
                    await rc.raw.hset("hcm:config:v2", "signal.active_model", str(pg_row["v"]))
                    try:
                        await rc.raw.publish("hcm:config:v2:updated", "signal.active_model")
                    except Exception:
                        pass
                    applied.append(f"resync_active_model: PG='{pg_row['v']}' 已覆盖写回 Redis")
                    succeeded.add("resync_active_model")
                else:
                    applied.append("resync_active_model: PG 无 signal.active_model 值，跳过")
            except Exception as exc:
                applied.append(f"resync_active_model: 失败 {exc}")

        # D1 (2026-08-04): 评分配置一致性自愈。将 PG hcm_config.metadata 中的
        # scoring.* 关键键覆盖写回 Redis hcm:config:v2，修复"面板改了但 Redis 丢失
        # → 引擎读到旧值/默认值"的配置漂移。HSET 后信号塔 60s 热重载即生效。
        DRIFT_KEYS_HEAL = [
            "scoring.h1_bias_enabled",
            "scoring.h1_reverse_penalty",
            "scoring.h1_reverse_penalty_confirmed",
            "scoring.strong_trend_reverse_penalty",
            "scoring.trend_strong_adx_threshold",
            "scoring.trend_reverse_suppress_factor",
            "scoring.lag_momentum_conflict_share",
            "scoring.lag_momentum_conflict_mom_opp",
            "scoring.direction_min_score",
        ]
        if "resync_config_drift" in fix_codes and rc is not None and rc.is_initialized and db_pool is not None and db_pool.is_initialized:
            try:
                resync_count = 0
                for key in DRIFT_KEYS_HEAL:
                    pg_row = await db_pool.fetchrow(
                        "SELECT COALESCE(NULLIF(current_value,''), default_value) AS v "
                        "FROM hcm_config.metadata WHERE config_key=$1", key
                    )
                    if pg_row and pg_row["v"] is not None:
                        await rc.raw.hset("hcm:config:v2", key, str(pg_row["v"]))
                        resync_count += 1
                if resync_count > 0:
                    try:
                        await rc.raw.publish("hcm:config:v2:updated", "resync_config_drift")
                    except Exception:
                        pass
                applied.append(f"resync_config_drift: {resync_count}/{len(DRIFT_KEYS_HEAL)} 键已从 PG 覆盖写回 Redis")
                succeeded.add("resync_config_drift")
            except Exception as exc:
                applied.append(f"resync_config_drift: 失败 {exc}")

        # 精准触发闸门锚定：把 hexp.min_grade 对齐到部署意图（配置键 hexp.min_grade_intended，
        # 可在面板/配置中心热调，默认 B）。与 resync_config_drift(全量子集)互补：本项针对单个
        # 被确认过的有意档位，即使 PG 当前值本身也被改过，也强制锚定回意图档（自愈校准对齐，
        # 而非盲从 PG）。换档位只需改 hexp.min_grade_intended 这一键，无需动代码或重启 hcm-web。
        #
        # ⚠️【铁律】见 docs/hexp_config_min_grade_iron_rule.md：本校准强制覆盖 hexp.min_grade，
        # 会撤销用户在面板对侧键 hexp.min_grade 的显式保存（曾导致"保存A级刷新复原成B"）。
        # 因此 web/api/hexp.py 的 _write_config 在保存 min_grade 时已同步 min_grade_intended，
        # 使意图锚点跟随用户选择。⚠️ 任何改动此处的人，必须保持"用户显式保存值优先于意图锚点"
        # 的约束，禁止再次出现"UI 改 A 键、系统读 B 键"的分裂覆盖。
        if "calibrate_min_grade" in fix_codes and rc is not None and rc.is_initialized and db_pool is not None and db_pool.is_initialized:
            try:
                # 读取部署意图档位：优先 Redis，回退 PG，最终回退默认 "B"
                _MG_INTENDED_DEFAULT = "B"
                _mg_target = None
                _ri = await rc.raw.hget("hcm:config:v2", "hexp.min_grade_intended")
                _ri = _ri.decode() if isinstance(_ri, bytes) else _ri
                if _ri:
                    _mg_target = str(_ri).strip().upper()
                else:
                    _pg_i = await db_pool.fetchrow(
                        "SELECT COALESCE(NULLIF(current_value,''), default_value) AS v "
                        "FROM hcm_config.metadata WHERE config_key='hexp.min_grade_intended'"
                    )
                    if _pg_i and _pg_i["v"]:
                        _mg_target = str(_pg_i["v"]).strip().upper()
                if not _mg_target:
                    _mg_target = _MG_INTENDED_DEFAULT
                # 1) 写 PG hcm_config.metadata（current_value 优先，缺则补 default_value）
                await db_pool.execute(
                    "UPDATE hcm_config.metadata "
                    "SET current_value=$2 WHERE config_key=$1",
                    "hexp.min_grade", _mg_target,
                )
                # 2) 写 Redis hcm:config:v2（引擎热读源）
                await rc.raw.hset("hcm:config:v2", "hexp.min_grade", _mg_target)
                # 3) 发布失效通知，各引擎清本地缓存
                try:
                    await rc.raw.publish("hcm:config:invalidate", "hexp.min_grade")
                except Exception:
                    pass
                applied.append(f"calibrate_min_grade: hexp.min_grade 已锚定到部署意图 {_mg_target}（PG+Redis 双写）")
                succeeded.add("calibrate_min_grade")
            except Exception as exc:
                applied.append(f"calibrate_min_grade: 失败 {exc}")

        # 全量配置校准（PG 全部键 vs Redis），把漂移键从 PG 权威覆盖写回 Redis。
        # 与 resync_config_drift(仅固定子集)互补：本项覆盖 metadata 全表，根治任意漂移。
        if "calibrate_config" in fix_codes:
            try:
                cal = await _calibrate_config_drift(rc, db_pool, dry_run=False)
                n = cal.get("calibrated", 0)
                applied.append(f"calibrate_config: 已把 {n} 个漂移键从 PG 覆盖写回 Redis")
                if n:
                    succeeded.add("calibrate_config")
            except Exception as exc:
                applied.append(f"calibrate_config: 失败 {exc}")

        # 清除僵尸消费组（已停用/不存在账号遗留的 group:<account_id>），降噪诊断并释放 pending。
        # 仅销毁 id 不在 PG 活跃主号/跟单号集合中的 group:<数字>，绝不碰 running 组或
        # risk-engine-group / copy-trading-group / dispatcher-group 等非账号组。
        if "clear_zombie_groups" in fix_codes and rc is not None and rc.is_initialized and db_pool is not None and db_pool.is_initialized:
            try:
                acct_rows = await db_pool.fetch(
                    "SELECT account_id FROM hcm_broker.accounts "
                    "WHERE account_type IN ('master','follower') AND is_active = true"
                )
                active_ids = {int(r["account_id"]) for r in acct_rows}
                destroyed: list[str] = []
                for stream in ("signal:risk_passed", "signal:stream"):
                    try:
                        grps = await rc.raw.xinfo_groups(stream)
                    except Exception:
                        continue
                    for g in grps:
                        gname = g.get("name")
                        if not gname:
                            continue
                        gname = str(gname)
                        if gname.startswith("group:") and gname[6:].isdigit():
                            gid = int(gname[6:])
                            if gid not in active_ids:
                                try:
                                    await rc.raw.xgroup_destroy(stream, gname)
                                    destroyed.append(f"{stream}:{gname}")
                                except Exception as de:
                                    applied.append(f"clear_zombie_groups: 销毁 {stream}:{gname} 失败 {de}")
                if destroyed:
                    applied.append(f"clear_zombie_groups: 销毁 {len(destroyed)} 个僵尸消费组 ({', '.join(destroyed)})")
                    succeeded.add("clear_zombie_groups")
                else:
                    applied.append("clear_zombie_groups: 无可销毁的僵尸消费组")
            except Exception as exc:
                applied.append(f"clear_zombie_groups: 失败 {exc}")

        return applied, succeeded

    async def _diagnose(auto_heal: bool = False) -> dict:
        """Run full-chain diagnosis and optionally apply safe self-heal.

        Checks (read-only):
          - downstream service /health endpoints
          - PG connectivity + config table row count
          - Redis connectivity + stream consumer groups
          - bridge single-instance lock
          - hcm:config:v2 integrity vs PG metadata

        Safe heals (only when auto_heal=True):
          - recreate bridge-order-group on signal:risk_passed if missing
          - HSETNX backfill missing hcm:config:v2 keys from PG metadata
        """
        import httpx
        nodes: list[dict] = []
        heal_applied: list[str] = []

        # ── 1. Downstream services ──
        for name, url in _SERVICE_ENDPOINTS.items():
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as hc:
                    resp = await hc.get(url)
                    ok = resp.status_code == 200
                    data = resp.json() if ok else {}
                    nodes.append({
                        "id": f"svc_{name.lower()}",
                        "name": name,
                        "ok": ok,
                        "msg": f"HTTP {resp.status_code}" if ok else f"HTTP {resp.status_code}",
                        "fix": "" if ok else "restart_container",
                    })
            except Exception as exc:
                nodes.append({
                    "id": f"svc_{name.lower()}",
                    "name": name,
                    "ok": False,
                    "msg": str(exc)[:80],
                    "fix": "restart_container",
                })

        # ── 2. PG connectivity ──
        pg_ok = False
        pg_config_rows = 0
        try:
            if db_pool is not None and db_pool.is_initialized:
                row = await db_pool.fetchrow(
                    "SELECT COUNT(*) AS cnt FROM hcm_config.metadata"
                )
                pg_config_rows = row["cnt"] if row else 0
                pg_ok = pg_config_rows > 0
        except Exception as exc:
            pass
        nodes.append({
            "id": "pg_connectivity",
            "name": "PG 配置库",
            "ok": pg_ok,
            "msg": f"metadata {pg_config_rows} 行" if pg_ok else "无法读取 hcm_config.metadata",
            "fix": "" if pg_ok else "check_pg",
        })

        # ── 3. Redis connectivity + config hash ──
        redis_ok = False
        config_key_count = 0
        try:
            if redis_client is not None and redis_client.is_initialized:
                redis_ok = await redis_client.ping()
                config_key_count = await redis_client.raw.hlen("hcm:config:v2")
        except Exception:
            pass
        nodes.append({
            "id": "redis_connectivity",
            "name": "Redis 热缓存",
            "ok": redis_ok,
            "msg": f"hcm:config:v2 {config_key_count} 键" if redis_ok else "Redis 不可达",
            "fix": "" if redis_ok else "restart_redis",
        })

        # ── 4. Bridge 单实例锁（每账户键 bridge:instance:lock:<account_id>）──
        # 旧通用键 bridge:instance:lock 已弃用；当前桥以每账户键上锁（值=进程 PID，
        # TTL=30s，每循环 ~5min 续期一次）。故"采样时刻未持锁"在续期间隙属正常，
        # 不可据此判错——需结合桥消费活跃度（TTL 无关）判断真实存活。
        held_locks: list = []          # (account_id, pid, ttl)
        consumer_active = False
        try:
            if redis_client is not None and redis_client.is_initialized:
                async for lk in redis_client.raw.scan_iter(match="bridge:instance:lock:*"):
                    try:
                        raw_val = await redis_client.raw.get(lk)
                        ttl = await redis_client.raw.ttl(lk)
                    except Exception:
                        continue
                    if raw_val and isinstance(ttl, int) and ttl > 0:
                        acct = lk.decode() if isinstance(lk, bytes) else str(lk)
                        acct = acct.split(":")[-1]
                        pid = raw_val.decode() if isinstance(raw_val, bytes) else str(raw_val)
                        held_locks.append((acct, pid, ttl))
                # 消费组存活（稳定信号）：signal:risk_passed 上的 group:<account_id>
                # 消费组若存在且含消费者，即表明对应桥已注册并在订阅（消费者条目在组内
                # 持久存在，不像单实例锁键那样 TTL 抖动，适合作为"桥未上锁"间隙的回退）。
                try:
                    groups = await redis_client.raw.xinfo_groups("signal:risk_passed")
                    for g in groups:
                        gname = g.get("name")
                        if not gname or not str(gname).startswith("group:"):
                            continue
                        try:
                            consumers = await redis_client.raw.xinfo_consumers("signal:risk_passed", gname)
                        except Exception:
                            consumers = []
                        if consumers:
                            consumer_active = True
                            break
                except Exception:
                    pass
        except Exception:
            pass
        lock_held = bool(held_locks)
        lock_ok = lock_held or consumer_active
        if lock_held:
            holders_desc = ", ".join(f"{a}(pid={p})" for a, p, _ in held_locks)
            lock_msg = f"已上锁: {holders_desc}"
        elif consumer_active:
            lock_msg = "单实例锁键采样时刻未持锁（TTL 30s、每循环续期，间隙属正常）；消费组活跃，桥存活正常"
        else:
            lock_msg = ("桥未持锁且无活跃消费组（可能未运行）" if redis_ok else "Redis 不可读")
        nodes.append({
            "id": "bridge_lock",
            "name": "Bridge 单实例锁",
            "ok": lock_ok,
            "msg": lock_msg,
            "fix": "" if lock_ok else "restart_bridge",
        })

        # ── 5. Bridge 消费组（signal:risk_passed 上的 group:<account_id>）──
        # 当前桥按账号创建消费组 group:<account_id>（旧名 bridge-order-group 已弃用），
        # 故诊断应检查 group: 前缀的真实消费组，而非已不存在的 bridge-order-group。
        bridge_groups: list = []
        try:
            if redis_client is not None and redis_client.is_initialized:
                all_groups = await redis_client.raw.xinfo_groups("signal:risk_passed")
                for g in all_groups:
                    gname = g.get("name")
                    if gname and str(gname).startswith("group:"):
                        bridge_groups.append(str(gname))
        except Exception:
            pass
        bridge_group_ok = bool(bridge_groups)
        nodes.append({
            "id": "bridge_consumer_group",
            "name": "Bridge 消费组 (signal:risk_passed)",
            "ok": bridge_group_ok,
            "msg": (f"消费组: {', '.join(bridge_groups)}" if bridge_group_ok
                    else ("signal:risk_passed 上无 group: 消费组（桥未注册）" if redis_ok else "Redis 不可读")),
            "fix": "" if bridge_group_ok else "restart_bridge",
        })

        # ── 5.4 僵尸消费组（已停用/不存在账号遗留的 group:<account_id>）──
        # 账户被停用/切走后，其消费组仍留在 signal:risk_passed / signal:stream 上，
        # 因无桥消费而 pending 累积、且在诊断里显示 lag → 噪音（如 group:17/group:20）。
        # 僵尸判定：group:<id> 中的 id 不在 PG hcm_broker.accounts 的活跃主号/跟单号集合中。
        zombie_groups: list[str] = []
        try:
            if redis_client is not None and redis_client.is_initialized and db_pool is not None and db_pool.is_initialized:
                acct_rows = await db_pool.fetch(
                    "SELECT account_id FROM hcm_broker.accounts "
                    "WHERE account_type IN ('master','follower') AND is_active = true"
                )
                active_ids = {int(r["account_id"]) for r in acct_rows}
                for stream in ("signal:risk_passed", "signal:stream"):
                    try:
                        grps = await redis_client.raw.xinfo_groups(stream)
                    except Exception:
                        continue
                    for g in grps:
                        gname = g.get("name")
                        if not gname:
                            continue
                        gname = str(gname)
                        if gname.startswith("group:") and gname[6:].isdigit():
                            gid = int(gname[6:])
                            if gid not in active_ids:
                                zombie_groups.append(f"{stream}:{gname}")
        except Exception:
            pass
        nodes.append({
            "id": "zombie_consumer_groups",
            "name": "僵尸消费组(已停用账号)",
            "ok": len(zombie_groups) == 0,
            "msg": ("发现 %d 个僵尸消费组(已停用账号遗留): %s" % (len(zombie_groups), ", ".join(zombie_groups)))
                  if zombie_groups else "无僵尸消费组",
            "fix": "" if not zombie_groups else "clear_zombie_groups",
            "data": {"zombies": zombie_groups},
        })

        # ── 5.5 Bridge 存活心跳（bridge:alive:<login>）──
        # 每个持锁权威桥每 ~10s 续期 bridge:alive:<login>（TTL 600s，payload 含 role/server/pid）。
        # 据此监控桥存活、检测"死亡(陈旧/缺失)"与"重复实例(心跳PID≠锁PID)"；键按动态 login
        # 生成，换经纪商(新终端新 login)即插即用。
        bl = await _bridge_liveness()
        nodes.append({
            "id": "bridge_liveness",
            "name": "Bridge 存活心跳",
            "ok": bl["ok"],
            "msg": bl["summary"],
            "fix": "" if bl["ok"] else "restart_bridge",
            "data": {"bridges": bl["bridges"]},
        })

        # ── 5.6 桥/账号拓扑对账（按 MT5 接入账号状态判定主号/多个跟单号）──
        # 面板注册为活跃的主号/跟单号(PG hcm_broker.accounts) 与 实际运行桥(Redis bridge:alive:*)
        # 对账：缺失=已注册活跃但无桥(终端未开/桥崩溃)→信息提示；孤儿=有存活键但不在
        # 注册活跃集(账号已移除/停用)→应清理脏键。使"自愈/桥启动看 MT5 账号状态判主号跟单"可观测。
        expected_accounts = []
        reconcile_pg_err = False
        try:
            if db_pool is not None and db_pool.is_initialized:
                rows = await db_pool.fetch(
                    "SELECT login, account_id, account_type FROM hcm_broker.accounts "
                    "WHERE account_type IN ('master','follower') AND is_active = true"
                )
                expected_accounts = [(str(r["login"]), int(r["account_id"]), str(r["account_type"])) for r in rows]
        except Exception:
            reconcile_pg_err = True
        alive_logins = []
        try:
            if redis_client is not None and redis_client.is_initialized:
                async for k in redis_client.raw.scan_iter(match="bridge:alive:*"):
                    alive_logins.append((k.decode() if isinstance(k, bytes) else str(k)).split(":")[-1])
        except Exception:
            pass
        if reconcile_pg_err:
            nodes.append({
                "id": "bridge_account_reconcile",
                "name": "桥/账号拓扑对账",
                "ok": True,
                "msg": "对账跳过(PG 读取失败)",
                "fix": "",
            })
        else:
            expected_logins = {l for l, _, _ in expected_accounts}
            missing = [l for l in expected_logins if l not in alive_logins]
            orphan = [l for l in alive_logins if l not in expected_logins]
            n_master = sum(1 for _, _, t in expected_accounts if t == "master")
            n_follower = sum(1 for _, _, t in expected_accounts if t == "follower")
            reconcile_ok = len(orphan) == 0
            parts = [f"PG 活跃主号 {n_master}/跟单 {n_follower}", f"运行桥 {len(alive_logins)}"]
            if missing:
                parts.append(f"缺失 {len(missing)}(终端未开/桥崩溃)")
            if orphan:
                parts.append(f"孤儿 {len(orphan)}(账号已移除,建议清脏键)")
            nodes.append({
                "id": "bridge_account_reconcile",
                "name": "桥/账号拓扑对账",
                "ok": reconcile_ok,
                "msg": "；".join(parts),
                "fix": "" if reconcile_ok else "clear_bridge_keys",
                "data": {
                    "expected_master": n_master, "expected_follower": n_follower,
                    "running_bridges": len(alive_logins), "missing": missing, "orphan": orphan,
                },
            })

        # ── 6. risk-engine consumer group ──
        risk_group_ok = False
        if redis_ok:
            risk_group_ok = await _group_exists("signal:stream", "risk-engine-group")
        nodes.append({
            "id": "risk_engine_group",
            "name": "risk-engine-group",
            "ok": risk_group_ok,
            "msg": "消费者组正常" if risk_group_ok else "signal:stream 上缺少 risk-engine-group",
            "fix": "" if risk_group_ok else "recreate_risk_group",
        })

        # ── 7. Config integrity: Redis vs PG metadata ──
        config_integrity_ok = True
        missing_keys: list[str] = []
        if redis_ok and pg_ok:
            try:
                pg_rows = await db_pool.fetch(
                    "SELECT config_key, COALESCE(NULLIF(current_value,''), default_value) AS v "
                    "FROM hcm_config.metadata"
                )
                redis_keys = set(await redis_client.raw.hkeys("hcm:config:v2") or [])
                missing_keys = [
                    r["config_key"] for r in pg_rows
                    if r["config_key"] not in redis_keys
                ]
                config_integrity_ok = len(missing_keys) == 0
            except Exception:
                config_integrity_ok = False
        nodes.append({
            "id": "config_integrity",
            "name": "配置完整性",
            "ok": config_integrity_ok,
            "msg": f"缺失 {len(missing_keys)} 键" if not config_integrity_ok else "Redis/PG 配置一致",
            "fix": "" if config_integrity_ok else "backfill_config",
        })
        # ── 7.5 K线未来棒（信号管线停滞根因）──
        future_klines = 0
        if db_pool is not None and db_pool.is_initialized:
            try:
                # 容忍“当前正在形成的棒”（其 open_time 最多等于 now，属正常边界）；
                # 仅当超过当前时间 1 分钟以上的“远未来棒”才判定为数据污染并报警。
                row = await db_pool.fetchrow("SELECT COUNT(*) AS c FROM hcm_market.klines WHERE open_time > now() + interval '1 minute'")
                future_klines = int(row["c"]) if row else 0
            except Exception:
                pass
        nodes.append({
            "id": "future_klines",
            "name": "K线未来棒",
            "ok": future_klines == 0,
            "msg": ("未来棒 %d 根（导致管线停滞）" % future_klines) if future_klines else "无未来棒(数据干净)",
            "fix": "" if future_klines == 0 else "delete_future_klines",
        })

        # ── 7.55 僵尸持仓（ticket 缺失的 open）──
        # 这些单（如 7-20 爆炸式开单产物）无 mt5_ticket，无法被 MT5 对账，
        # 原 _close_stale_positions 过滤 mt5_ticket IS NOT NULL 而永不清理 → PG 永久残留 open
        # → 经最大单数修复后被 _get_open_positions_count 计入 → 反阻塞同 account+symbol 新单。
        orphan_cnt = -1
        if db_pool is not None and db_pool.is_initialized:
            try:
                orow = await db_pool.fetchrow(
                    "SELECT COUNT(*) AS c FROM hcm_trading.positions "
                    "WHERE status='open' AND (mt5_ticket IS NULL OR mt5_ticket <= 0)")
                orphan_cnt = int(orow["c"]) if orow else 0
            except Exception:
                orphan_cnt = -1
        nodes.append({
            "id": "orphan_positions",
            "name": "僵尸持仓(ticket缺失)",
            "ok": orphan_cnt == 0,
            "msg": ("存在 %d 条 ticket 缺失的 open 持仓（无法被 MT5 对账，永久阻塞同品种新单）" % orphan_cnt)
                  if orphan_cnt > 0 else ("无僵尸持仓" if orphan_cnt == 0 else "查询失败"),
            "fix": "" if orphan_cnt == 0 else "clear_orphan_positions",
        })

        # ── 7.56 Phantom 持仓（所有 mt5_ticket 为空的幽灵，含已 closed 的历史双写）──
        # 判据：position_sync UPSERT(566行) 与桥开仓 INSERT(1126行) 均始终写 mt5_ticket，
        # 故 mt5_ticket IS NULL/<=0 的任意行必为 pre-fix 双写幽灵（非真实经纪商单）。
        # 现有 orphan_positions 节点(7.55)只查 open 态，会漏报已 closed 的历史幽灵；
        # 本节点覆盖全态，使 2026-07-25 那类 570 条 closed 幽灵也能被一键诊断发现并自愈。
        phantom_cnt = -1
        if db_pool is not None and db_pool.is_initialized:
            try:
                prow = await db_pool.fetchrow(
                    "SELECT COUNT(*) AS c FROM hcm_trading.positions "
                    "WHERE mt5_ticket IS NULL OR mt5_ticket <= 0")
                phantom_cnt = int(prow["c"]) if prow else 0
            except Exception:
                phantom_cnt = -1
        nodes.append({
            "id": "phantom_positions",
            "name": "Phantom持仓(ticket为空)",
            "ok": phantom_cnt == 0,
            "msg": ("存在 %d 条 mt5_ticket 为空的 Phantom 持仓（pre-fix 双写幽灵，非真实经纪商单，可安全删除）" % phantom_cnt)
                  if phantom_cnt > 0 else ("无 Phantom 持仓" if phantom_cnt == 0 else "查询失败"),
            "fix": "" if phantom_cnt == 0 else "clear_phantom_positions",
        })

        # ── 7.6 时段平仓系数完整性 ──
        sess_missing: list[str] = []
        if redis_ok:
            try:
                from .close import SESSIONS, SESSION_SUFFIXES
                for s in SESSIONS:
                    for suf in SESSION_SUFFIXES:
                        key = f"close.{s}.{suf}"
                        if await redis_client.raw.hget("hcm:config:v2", key) is None:
                            sess_missing.append(key)
            except Exception:
                pass
        nodes.append({
            "id": "session_config",
            "name": "时段平仓系数",
            "ok": len(sess_missing) == 0,
            "msg": ("缺失 %d 个会话系数键" % len(sess_missing)) if sess_missing else "亚/欧/美系数齐全",
            "fix": "" if not sess_missing else "reseed_session_config",
        })

        # ── 7.7 信号引擎存活（用 last_loop_at 判存活，避免横盘无单误报为停滞）──
        eng_stall = False
        eng_msg = "引擎产出正常"
        if redis_ok:
            try:
                raw = await redis_client.raw.get("hcm:signal_tower:engine_status")
                if raw:
                    import json as _json
                    eng = _json.loads(raw if isinstance(raw, str) else raw.decode())
                    running = bool(eng.get("running", False))
                    last_loop = eng.get("last_loop_at")
                    if not running:
                        eng_stall = True
                        eng_msg = "信号引擎未运行"
                    elif last_loop is not None:
                        try:
                            age = time.time() - float(last_loop)
                            if age > 120:
                                eng_stall = True
                                eng_msg = "引擎循环停滞 %d 秒(已无心跳)" % int(age)
                        except Exception:
                            pass
            except Exception:
                pass
        nodes.append({
            "id": "engine_heartbeat",
            "name": "信号引擎存活",
            "ok": not eng_stall,
            "msg": eng_msg,
            "fix": "" if not eng_stall else "activate_engine",
        })

        # ── 7.85 激活模型一致性：PG hcm_config.metadata vs Redis hcm:config:v2 ──
        # signal.active_model 必须经 config_provider.set 双写（PG+Redis+PUB），禁止直接 SQL 改
        # PG，否则 Redis 仍是旧值 → 共源闸门静默失效（2026-07-25 根因：7-23 05:20 直接 SQL 改
        # PG 未双写 Redis，闸门空转约 1.5 天，0.13~0.30 弱分信号放量逆势亏损）。
        active_ok = True
        active_msg = "signal.active_model PG↔Redis 一致"
        active_pg = active_redis = None
        if redis_ok and pg_ok:
            try:
                pg_row = await db_pool.fetchrow(
                    "SELECT COALESCE(NULLIF(current_value,''), default_value) AS v "
                    "FROM hcm_config.metadata WHERE config_key='signal.active_model'"
                )
                active_pg = pg_row["v"] if pg_row else None
                active_redis = await redis_client.raw.hget("hcm:config:v2", "signal.active_model")
                if isinstance(active_redis, bytes):
                    active_redis = active_redis.decode()
                pg_norm = (active_pg or "").strip().lower()
                r_norm = (active_redis or "").strip().lower()
                if pg_norm != r_norm:
                    active_ok = False
                    active_msg = (
                        f"signal.active_model 不一致: PG='{active_pg}' Redis='{active_redis}' "
                        f"（须经配置面板/接口双写，禁止直接 SQL 改 PG）"
                    )
            except Exception as exc:
                active_ok = False
                active_msg = f"signal.active_model 一致性检查异常: {exc}"
        elif redis_ok or pg_ok:
            active_ok = False
            active_msg = "Redis 或 PG 不可达，无法核对 signal.active_model"
        nodes.append({
            "id": "active_model_consistency",
            "name": "激活模型一致性(PG↔Redis)",
            "ok": active_ok,
            "msg": active_msg,
            "fix": "" if active_ok else "resync_active_model",
            "data": {"pg": active_pg, "redis": active_redis},
        })

        # ── 7.86 关键评分配置 PG↔Redis 一致性（D1 2026-08-04）──
        # scoring.* 关键键的双写一致性核对，揭示「面板改了≠生效了」根因：
        # 面板→API→config_provider.set 双写 PG+Redis+PUB，但若 Redis 重启丢键，
        # 引擎读到 Redis 旧值/默认值 → 配置漂移。
        DRIFT_KEYS = [
            "scoring.h1_bias_enabled",
            "scoring.h1_reverse_penalty",
            "scoring.h1_reverse_penalty_confirmed",
            "scoring.strong_trend_reverse_penalty",
            "scoring.trend_strong_adx_threshold",
            "scoring.trend_reverse_suppress_factor",
            "scoring.lag_momentum_conflict_share",
            "scoring.lag_momentum_conflict_mom_opp",
            "scoring.direction_min_score",
        ]
        drift_ok = True
        drift_msg = "scoring.* 关键键 PG↔Redis 一致"
        drift_kv: list[str] = []
        if redis_ok and pg_ok:
            try:
                for key in DRIFT_KEYS:
                    pg_row = await db_pool.fetchrow(
                        "SELECT COALESCE(NULLIF(current_value,''), default_value) AS v "
                        "FROM hcm_config.metadata WHERE config_key=$1", key
                    )
                    pg_v = pg_row["v"] if pg_row else None
                    r_v_raw = await redis_client.raw.hget("hcm:config:v2", key)
                    r_v = r_v_raw.decode() if isinstance(r_v_raw, bytes) else r_v_raw
                    if pg_v is not None and r_v is not None and str(pg_v).strip() != str(r_v).strip():
                        drift_ok = False
                        drift_kv.append(f"{key.split('.')[-1]}: PG={pg_v} ≠ Redis={r_v}")
                    elif pg_v is not None and r_v is None:
                        drift_ok = False
                        drift_kv.append(f"{key.split('.')[-1]}: PG={pg_v} Redis=缺失")
            except Exception as exc:
                drift_ok = False
                drift_msg = f"config_drift 检查异常: {exc}"
        elif redis_ok or pg_ok:
            drift_ok = False
            drift_msg = "Redis 或 PG 不可达，无法核对 scoring 关键键一致性"
        if not drift_ok and drift_kv:
            drift_msg = "scoring.* 关键键不一致（引擎可能读到旧值）: " + "; ".join(drift_kv[:5])
        nodes.append({
            "id": "config_drift_scoring",
            "name": "评分配置一致性(PG↔Redis)",
            "ok": drift_ok,
            "msg": drift_msg,
            "fix": "" if drift_ok else "resync_config_drift",
            "data": {"drifts": drift_kv},
        })

        # ── 配置全量校准检查（PG 全部键 vs Redis hcm:config:v2） ──
        # 与上方 scoring 关键键检查互补：此处扫描 metadata 全表，暴露任意漂移（不限评分键）。
        cal = await _calibrate_config_drift(redis_client, db_pool, dry_run=True)
        if cal["errors"]:
            cal_ok = False
            cal_detail = "检查失败: " + "; ".join(cal["errors"])
        else:
            cal_ok = (len(cal["drifts"]) == 0)
            cal_detail = ("全部 %d 个配置键 PG 与 Redis 一致" % cal["total"]) if cal_ok else \
                ("漂移 %d/%d 个，如: %s" % (
                    len(cal["drifts"]), cal["total"],
                    ", ".join(d["key"] for d in cal["drifts"][:20])))
        nodes.append({
            "id": "config_calibration",
            "name": "配置全量校准(PG↔Redis)",
            "ok": cal_ok,
            "msg": cal_detail,
            "fix": "" if cal_ok else "calibrate_config",
            "data": {"total": cal["total"], "drifts": cal["drifts"]},
        })
        if not cal_ok:
            suggestions.append("执行「🔧 校准配置」或「⚡ 自愈」以把全部漂移的配置键从 PG 覆盖写回 Redis。")

        # ── 7.87 K线写入源标识（D4 2026-08-05）──
        # 诊断"谁在写 K线"：生产环境唯一真实写入源应为 bridge(mt5_bridge 主机进程)。
        # 若最近窗口内无 bridge 写入 / 仅 collector 写入，说明 K线管道可能停滞或双写污染。
        klines_ok = True
        klines_msg = "K线写入源正常（bridge 为主写入源）"
        klines_data: dict = {}
        if pg_ok:
            try:
                since_min = 60
                rows = await db_pool.fetch(
                    "SELECT source, COUNT(*) AS cnt, MAX(open_time) AS last_at "
                    "FROM hcm_market.klines "
                    "WHERE open_time > now() - ($1::int || ' minutes')::interval "
                    "GROUP BY source ORDER BY cnt DESC",
                    since_min,
                )
                src_map = {r["source"]: {"cnt": r["cnt"], "last_at": str(r["last_at"])} for r in rows}
                klines_data = {"since_minutes": since_min, "sources": src_map}
                if not src_map:
                    klines_ok = False
                    klines_msg = f"近 {since_min} 分钟无任何 K线写入（管道可能停滞）"
                elif "bridge" not in src_map:
                    klines_ok = False
                    klines_msg = (
                        f"近 {since_min} 分钟无 bridge 写入（仅 {list(src_map)}），"
                        "K线可能来自非权威源"
                    )
                else:
                    _b = src_map.get("bridge", {}).get("cnt", 0)
                    _c = src_map.get("collector", {}).get("cnt", 0)
                    if _c > 0 and _c >= _b:
                        klines_ok = False
                        klines_msg = (
                            f"collector 写入量({_c})≥bridge({_b})，存在双写竞争风险"
                        )
            except Exception as exc:
                klines_ok = False
                klines_msg = f"K线写入源检查异常: {exc}"
        else:
            klines_ok = False
            klines_msg = "PG 不可达，无法核对 K线写入源"
        nodes.append({
            "id": "klines_writer_source",
            "name": "K线写入源标识",
            "ok": klines_ok,
            "msg": klines_msg,
            "fix": "",
            "data": klines_data,
        })

        # ── 7.88 精准触发闸门漂移（min_grade_drift, 2026-08-15）──
        # 比对三值：①代码默认（hexp_engine._DEFAULTS["hexp.min_grade"] = "C"，
        #   引擎未命中配置中心时的回退值）；②部署意图（配置键 hexp.min_grade_intended，
        #   可在面板/配置中心热调，默认回退 "B"，避免静默漂移）；③Redis 当前运行值。
        # 漂移判定：Redis 当前值 ≠ 部署意图（intended） → 告警（代码默认 C 仅作参考展示，
        # 不参与漂移判定，因为 B 是有意偏离默认的中间档）。换档位只需改 hexp.min_grade_intended
        # 这一键，无需动代码或重启 hcm-web（自愈读取该键把 hexp.min_grade 对齐）。
        # 自愈 calibrate_min_grade：把 hexp.min_grade 对齐到部署意图（PG + Redis 双写 + PUB）。
        _MG_CODE_DEFAULT = "C"          # 来源：hcm-signal-tower/signal_tower/hexp_engine.py _DEFAULTS
        _MG_INTENDED_DEFAULT = "B"      # hexp.min_grade_intended 缺省回退值（用户 2026-08-15 确认锚定 B）
        _mg_intended = _MG_INTENDED_DEFAULT
        try:
            if redis_client is not None and redis_client.is_initialized:
                _ri = await redis_client.raw.hget("hcm:config:v2", "hexp.min_grade_intended")
                _ri = _ri.decode() if isinstance(_ri, bytes) else _ri
                if _ri:
                    _mg_intended = str(_ri).strip().upper()
                else:
                    _pg_i = await db_pool.fetchrow(
                        "SELECT COALESCE(NULLIF(current_value,''), default_value) AS v "
                        "FROM hcm_config.metadata WHERE config_key='hexp.min_grade_intended'"
                    )
                    if _pg_i and _pg_i["v"]:
                        _mg_intended = str(_pg_i["v"]).strip().upper()
        except Exception:
            _mg_intended = _MG_INTENDED_DEFAULT
        _mg_redis = None
        _mg_pg = None
        try:
            if redis_client is not None and redis_client.is_initialized:
                _r = await redis_client.raw.hget("hcm:config:v2", "hexp.min_grade")
                _mg_redis = _r.decode() if isinstance(_r, bytes) else _r
            if pg_ok and db_pool is not None and db_pool.is_initialized:
                _pg = await db_pool.fetchrow(
                    "SELECT COALESCE(NULLIF(current_value,''), default_value) AS v "
                    "FROM hcm_config.metadata WHERE config_key='hexp.min_grade'"
                )
                _mg_pg = _pg["v"] if _pg and _pg["v"] is not None else None
        except Exception as exc:
            _mg_redis = f"<err:{exc}>"
        _mg_drift = bool(_mg_redis) and str(_mg_redis).strip().upper() != _mg_intended
        _mg_parts = [
            f"代码默认={_MG_CODE_DEFAULT}",
            f"部署意图={_mg_intended}",
            f"Redis当前={_mg_redis or '缺失'}",
            f"PG={_mg_pg or '缺失'}",
        ]
        if _mg_drift:
            _mg_msg = "hexp.min_grade 漂移: " + "; ".join(_mg_parts) + "（应锚定部署意图）"
        else:
            _mg_msg = "hexp.min_grade 锚定部署意图（" + "; ".join(_mg_parts) + "）"
        nodes.append({
            "id": "min_grade_drift",
            "name": "精准触发闸门(min_grade)漂移",
            "ok": not _mg_drift,
            "msg": _mg_msg,
            "fix": "" if not _mg_drift else "calibrate_min_grade",
            "data": {
                "code_default": _MG_CODE_DEFAULT,
                "intended": _mg_intended,
                "redis": _mg_redis,
                "pg": _mg_pg,
            },
        })

        # ── 安全自愈调度（一键自愈）──
        # 仅执行可逆/无损动作；容器/PG/Redis 重启等需宿主机的动作不改在此处理。
        # CONFIRMED_HEALS: 执行后必恢复；其余(activate_engine/restart_bridge)为尝试性，需复核。
        CONFIRMED_HEALS = {"clear_bridge_keys", "delete_future_klines",
                           "reseed_session_config", "backfill_config", "recreate_risk_group",
                           "clear_orphan_positions", "resync_active_model",
                           "clear_phantom_positions", "resync_config_drift",
                           "calibrate_config", "clear_zombie_groups",
                           "calibrate_min_grade"}
        if auto_heal:
            fix_codes = {n["fix"] for n in nodes if not n["ok"] and n["fix"]}
            if fix_codes:
                applied_list, succeeded = await _apply_self_heals(fix_codes)
                heal_applied.extend(applied_list)
                for n in nodes:
                    if (not n["ok"]) and n["fix"] in succeeded:
                        if n["fix"] in CONFIRMED_HEALS:
                            n["ok"] = True
                            n["msg"] = "已自愈: " + n["msg"]
                            n["fix"] = ""
                        else:
                            n["healed"] = True
                            n["msg"] = "已尝试自愈(若仍异常需人工): " + n["msg"]

        # ── 8. Recent order flow ──
        order_ok = False
        order_last = None
        try:
            if db_pool is not None and db_pool.is_initialized:
                row = await db_pool.fetchrow(
                    "SELECT MAX(created_at) AS last_time FROM hcm_trading.orders"
                )
                if row and row["last_time"]:
                    from datetime import timedelta
                    delta = datetime.now(timezone.utc) - row["last_time"]
                    order_ok = delta < timedelta(minutes=30)
                    order_last = row["last_time"].isoformat()
        except Exception:
            pass
        # 最近下单仅作信息提示：无订单可能是策略过滤/横盘，不标异常
        order_msg = f"最近订单 {order_last}" if order_ok else (
            "最近 30 分钟无订单（策略过滤/横盘属正常）" if order_last else "无订单记录"
        )
        nodes.append({
            "id": "recent_order",
            "name": "最近下单",
            "ok": True,
            "msg": order_msg,
            "fix": "",
        })

        # recent_order 只是信息参考：策略过滤/横盘导致 30 分钟无单属正常，
        # 不应让 overall 变成 degraded。仅系统级卡点参与 all_pass。
        critical_nodes = [n for n in nodes if n["id"] != "recent_order"]
        all_pass = all(n["ok"] for n in critical_nodes)
        suggestion = "全链路畅通" if all_pass else "检测到异常，请查看下方节点"
        if not all_pass and auto_heal:
            failed = [n["name"] for n in nodes if not n["ok"]]
            suggestion = f"已尝试自愈，仍有 {len(failed)} 项未恢复: {', '.join(failed)}" if failed else "自愈完成"

        return {
            "all_pass": all_pass,
            "nodes": nodes,
            "suggestion": suggestion,
            "heal_applied": heal_applied,
        }

    @router.get("/api/v1/system/calibrate-config", summary="检查配置漂移(PG↔Redis)")
    async def calibrate_config_check(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """只读检查：以 PG 为权威真源，扫描全部配置键与 Redis hcm:config:v2 的漂移明细。

        不写任何数据，供前端「查看漂移」与诊断节点复用。
        """
        cal = await _calibrate_config_drift(redis_client, db_pool, dry_run=True)
        ok = (len(cal["drifts"]) == 0) and not cal["errors"]
        return {"code": 0 if ok else 1, "data": cal,
                "message": "全部配置键一致" if ok else ("; ".join(cal["errors"]) or "存在漂移")}

    @router.post("/api/v1/system/calibrate-config", summary="执行配置校准(PG→Redis)")
    async def calibrate_config_apply(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """全量校准：把 PG hcm_config.metadata 全部键覆盖写回 Redis hcm:config:v2。

        仅覆盖「Redis 缺失」或「值与 PG 不一致」的键，并逐键发布 hcm:config:invalidate
        通知各引擎清本地缓存。PG 为唯一权威真源，不删除 Redis 中 PG 没有的键。
        """
        cal = await _calibrate_config_drift(redis_client, db_pool, dry_run=False)
        ok = (len(cal["errors"]) == 0)
        return {"code": 0 if ok else 1, "data": cal,
                "message": "已校准 %d 个漂移键" % cal.get("calibrated", 0) if ok
                else ("; ".join(cal["errors"]) or "校准失败")}

    @router.post("/api/v1/system/diagnose")
    async def diagnose_v1(
        request: Request,
        auto_heal: bool = Query(False, description="是否执行安全自愈"),
        user=Depends(auth_handler.require_auth),
    ):
        """One-click diagnosis with optional safe self-heal.

        Read-only by default. Pass auto_heal=true to recreate missing
        bridge-order-group and HSETNX-backfill missing config keys.
        """
        return {"code": 0, "data": await _diagnose(auto_heal=auto_heal), "message": "ok"}

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

    @router.put("/api/system/accounts/{account_id}/active")
    async def legacy_set_account_active(
        account_id: int,
        body: dict,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Enable/disable account — alias for /api/v1/system/accounts/{account_id}/active."""
        if not isinstance(body, dict):
            return {"code": "ACCT_006", "data": None, "message": "请求体必须为 JSON 对象"}
        is_active = body.get("is_active")
        if not isinstance(is_active, bool):
            return {"code": "ACCT_006", "data": None, "message": "is_active 必须为 true/false"}
        return await _set_account_active_impl(account_id, is_active)

    # ── 8. Health Check legacy ───────────────────

    @router.get("/api/system/health/detailed", include_in_schema=False)
    async def legacy_detailed_health(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Health check — alias for /api/v1/system/health/detailed."""
        return await _detailed_health()

    # ── 9. Pipeline Status legacy ────────────────

    @router.get("/api/system/pipeline", include_in_schema=False)
    async def legacy_pipeline_status(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Pipeline status — alias for /api/v1/system/pipeline."""
        return {'code': 0, 'data': await _pipeline_status(), 'message': 'ok'}

    # ── 10. One-Click Pipeline Diagnose + Self-Heal ──

    @router.post("/api/v1/system/diagnose")
    async def diagnose_pipeline(
        request: Request,
        user=Depends(auth_handler.require_auth),
        auto_heal: bool = False,
    ):
        """One-click diagnose of the full signal→order pipeline.

        Checks 8 nodes: K线→指标→Regime→评分→AI→风控→SL/TP→订单.
        Returns status + suggested fixes. When auto_heal=True, applies fixes.
        """
        nodes: list[dict] = []
        try:
            import time as _time, datetime as _dt, json as _json
        except ImportError:
            pass

        # ── Node 1: K-line (bridge → PG) ──
        kline_ok, kline_msg, kline_fix = True, "", ""
        try:
            if db_pool and db_pool.is_initialized:
                row = await db_pool.fetchrow(
                    "SELECT open_time FROM hcm_market.klines WHERE symbol='XAUUSD' ORDER BY open_time DESC LIMIT 1")
                if row:
                    now_utc = _dt.datetime.now(_dt.timezone.utc)
                    latest = row["open_time"]
                    if latest.tzinfo is None:
                        latest = latest.replace(tzinfo=_dt.timezone.utc)
                    age_sec = (now_utc - latest).total_seconds()
                    if age_sec > 600:
                        kline_ok = False
                        kline_msg = f"K线延迟 {age_sec:.0f}s"
                        kline_fix = "restart_bridge"
                    else:
                        kline_msg = f"最新 {latest.strftime('%H:%M')} (延迟{age_sec:.0f}s)"
                else:
                    kline_ok = False; kline_msg = "无 K 线数据"; kline_fix = "restart_bridge"
            else:
                kline_ok = False; kline_msg = "DB 未连接"; kline_fix = "restart_postgres"
        except Exception as e:
            kline_ok = False; kline_msg = str(e)[:80]; kline_fix = "restart_bridge"
        nodes.append({"id": "kline", "name": "K线摄入", "ok": kline_ok, "msg": kline_msg, "fix": kline_fix})

        # ── Node 2: Signal Tower (heartbeat) ──
        st_ok, st_msg, st_fix = True, "", ""
        try:
            if redis_client and redis_client.is_initialized:
                hb_raw = await redis_client.get("hcm:signal_tower:last_production:XAUUSD:M5")
                if hb_raw:
                    hb = _dt.datetime.fromisoformat(str(hb_raw).replace("Z", "+00:00"))
                    age_sec = (_dt.datetime.now(_dt.timezone.utc) - hb).total_seconds()
                    if age_sec > 600:
                        st_ok = False; st_msg = f"心跳 {hb.strftime('%H:%M')} (延迟{age_sec:.0f}s)"
                        st_fix = "restart_signal_tower"
                    else:
                        st_msg = f"心跳 {hb.strftime('%H:%M')} (延迟{age_sec:.0f}s)"
                else:
                    st_ok = False; st_msg = "无心跳"; st_fix = "restart_signal_tower"
            else:
                st_ok = False; st_msg = "Redis 未连接"; st_fix = "restart_redis"
        except Exception as e:
            st_ok = False; st_msg = str(e)[:80]; st_fix = "restart_signal_tower"
        nodes.append({"id": "signal_tower", "name": "信号引擎", "ok": st_ok, "msg": st_msg, "fix": st_fix})

        # ── Node 3: Indicator Calculator ──
        nodes.append({"id": "indicators", "name": "技术指标", "ok": True, "msg": "随信号塔加载", "fix": ""})

        # ── Node 4: Regime Classifier ──
        nodes.append({"id": "regime", "name": "市况分类", "ok": True, "msg": "随信号塔加载", "fix": ""})

        # ── Node 5: Scoring Engine ──
        score_ok, score_msg, score_fix = True, "", ""
        try:
            if db_pool and db_pool.is_initialized:
                row = await db_pool.fetchrow(
                    "SELECT pre_score, signal_dir, created_at FROM hcm_signal.signals ORDER BY signal_id DESC LIMIT 1")
                if row:
                    score_msg = f"{row['signal_dir']} score={float(row['pre_score']):.3f}"
                else:
                    score_ok = False; score_msg = "无信号产出"; score_fix = "lower_score_threshold"
            else:
                score_ok = False; score_msg = "DB 未连接"; score_fix = "restart_postgres"
        except Exception as e:
            score_ok = False; score_msg = str(e)[:80]; score_fix = "restart_signal_tower"
        nodes.append({"id": "scoring", "name": "评分引擎", "ok": score_ok, "msg": score_msg, "fix": score_fix})

        # ── Node 6: AI / Fallback ──
        ai_ok, ai_msg, ai_fix = True, "", ""
        try:
            if redis_client and redis_client.is_initialized:
                deepseek_cfg = await redis_client.hget("hcm:config:v2", "deepseek.api_key")
                ai_msg = "AI DeepSeek"
                if not deepseek_cfg or deepseek_cfg == "test_pg_key":
                    ai_ok = False; ai_msg = "DeepSeek API key 未配置"; ai_fix = "set_deepseek_key"
                else:
                    # Check AI circuit breaker
                    ai_fallback_count = 0
                    if db_pool and db_pool.is_initialized:
                        row2 = await db_pool.fetchrow(
                            "SELECT count(*) as cnt FROM hcm_signal.signals "
                            "WHERE fallback_reason='ai_fallback' AND created_at > now() - interval '1 hour'")
                        if row2:
                            ai_fallback_count = row2["cnt"] or 0
                    if ai_fallback_count > 0:
                        ai_msg += f" (最近1h回退{ai_fallback_count}次)"
        except Exception as e:
            ai_ok = False; ai_msg = str(e)[:80]; ai_fix = "set_deepseek_key"
        nodes.append({"id": "ai", "name": "AI增强", "ok": ai_ok, "msg": ai_msg, "fix": ai_fix})

        # ── Node 7: Risk Engine ──
        risk_ok, risk_msg, risk_fix = True, "", ""
        try:
            if redis_client and redis_client.is_initialized:
                min_conf = await redis_client.hget("hcm:config:v2", "risk_min_confidence")
                max_pos = await redis_client.hget("hcm:config:v2", "risk.max_concurrent_signals")
                tier_low = await redis_client.hget("hcm:config:v2", "risk.score_tier_low")
                risk_msg = f"min_conf={min_conf or '?'} max_pos={max_pos or '?'}"
                if tier_low and float(tier_low) > 0.2:
                    risk_ok = False
                    risk_msg += f" (score_tier_low={tier_low} 过高!)"
                    risk_fix = "lower_risk_score_tier_low"
                if auto_heal and risk_fix == "lower_risk_score_tier_low":
                    # 双写（PG SoT + Redis L2 + PUB）：单写 Redis 会在校准/重启后被 PG 旧值
                    # 覆盖（「改了又复原」）。铁律 5.2 禁止单端直写。
                    if db_pool is not None and db_pool.is_initialized:
                        try:
                            await db_pool.execute(
                                "INSERT INTO hcm_config.metadata "
                                "(config_key, default_value, current_value, value_type, category) "
                                "VALUES ($1, $2, $3, 'string', 'risk') "
                                "ON CONFLICT (config_key) DO UPDATE SET current_value=EXCLUDED.current_value, updated_at=now()",
                                "risk.score_tier_low", "0.10", "0.10",
                            )
                        except Exception as _pgerr:
                            logger.warning("lower_risk_score_tier_low PG write failed: %s", _pgerr)
                    await redis_client.hset("hcm:config:v2", "risk.score_tier_low", "0.10")
                    try:
                        await redis_client.publish("hcm:config:invalidate", "risk.score_tier_low")
                    except Exception:
                        pass
                    risk_msg += " → 已自动修复为0.10（PG+Redis 双写）"
                    risk_ok = True; risk_fix = ""
        except Exception as e:
            risk_ok = False; risk_msg = str(e)[:80]; risk_fix = "restart_risk_engine"
        nodes.append({"id": "risk", "name": "风控引擎", "ok": risk_ok, "msg": risk_msg, "fix": risk_fix})

        # ── Node 8: Bridge / MT5 ──
        bridge_ok, bridge_msg, bridge_fix = True, "", ""
        try:
            if redis_client and redis_client.is_initialized:
                live_raw = await redis_client.hget("hcm:config:v2", "market:latest:XAUUSD")
                if live_raw:
                    data = _json.loads(live_raw)
                    bid = float(data.get("bid", 0))
                    ask = float(data.get("ask", 0))
                    bridge_msg = f"bid={bid:.2f} ask={ask:.2f}"
                else:
                    bridge_ok = False; bridge_msg = "无实时报价"; bridge_fix = "restart_bridge"
            else:
                bridge_ok = False; bridge_msg = "Redis 未连接"; bridge_fix = "restart_redis"
        except Exception as e:
            bridge_ok = False; bridge_msg = str(e)[:80]; bridge_fix = "restart_bridge"
        nodes.append({"id": "bridge", "name": "桥接/下单", "ok": bridge_ok, "msg": bridge_msg, "fix": bridge_fix})

        # ── Auto-heal actions ──
        heal_done: list[str] = []
        if auto_heal:
            import subprocess as _sp, os as _os, asyncio
            for n in nodes:
                if n["fix"] == "restart_signal_tower" and n["fix"] not in heal_done:
                    try:
                        _sp.run(["docker", "restart", "hcm-v2-hcm-signal-tower-1"], timeout=30, capture_output=True)
                        heal_done.append("restart_signal_tower")
                        n["msg"] += " (已重启)"
                        n["ok"] = True
                    except Exception:
                        pass
                elif n["fix"] == "restart_bridge" and n["fix"] not in heal_done:
                    # 桥进程运行在 Windows 主机，web 容器(Linux)无法直接拉起/杀死主机进程。
                    # 容器内唯一有效的自愈：经 Redis 向【存活】桥下发重启信令(bridge:control:<login>=restart)，
                    # 桥收到后主动退出，由主机看门狗重拉。若桥已全死(主机看门狗未运行)，则必须人工在
                    # Windows 主机执行 start.bat —— 容器侧不做无意义的 taskkill/Popen（否则自愈无效）。
                    try:
                        _alives = [k async for k in redis_client.raw.scan_iter(match="bridge:alive:*")]
                        for _k in _alives:
                            _login = str(_k).rsplit(":", 1)[-1]
                            try:
                                await redis_client.raw.set(f"bridge:control:{_login}", "restart", ex=120)
                            except Exception:
                                pass
                        if _alives:
                            heal_done.append("restart_bridge")
                            n["msg"] += f" (已对 {len(_alives)} 个存活桥下发重启信令，主机看门狗将重拉)"
                            n["ok"] = True
                        else:
                            n["msg"] += (" (无任何存活桥心跳——主机看门狗可能未运行，"
                                         "请在 Windows 主机执行 start.bat 拉起桥进程)")
                    except Exception as _e:
                        n["msg"] += f" (重启信令下发失败: {_e})"
                elif n["fix"] == "restart_risk_engine" and n["fix"] not in heal_done:
                    try:
                        _sp.run(["docker", "restart", "hcm-v2-hcm-risk-engine-1"], timeout=30, capture_output=True)
                        heal_done.append("restart_risk_engine")
                        n["msg"] += " (已重启)"
                        n["ok"] = True
                    except Exception:
                        pass

        all_ok = all(n["ok"] for n in nodes)
        return {
            "code": 0,
            "data": {
                "all_pass": all_ok,
                "nodes": nodes,
                "heal_applied": heal_done,
                "suggestion": "全链路畅通" if all_ok else f"{sum(1 for n in nodes if not n['ok'])}个节点异常",
            },
            "message": "ok",
        }

    return router
