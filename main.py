"""
NewAPI Key 分发组件。

提供 QQ/AstrBot 用户绑定、申请、审批和自动创建 NewAPI token 的轻量流程。
"""

from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools


def _mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:7]}{'*' * max(6, len(key) - 14)}{key[-7:]}"


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开启", "是"}
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _extract_command_args(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if not text:
        return ""
    if text.startswith("/"):
        text = text[1:]
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@dataclass(slots=True)
class PluginSettings:
    newapi_base_url: str
    admin_access_token: str
    admin_user_id: int
    bot_admin_ids: set[str]
    enable_chat_config: bool
    auto_create_enabled: bool
    private_only_for_secret: bool
    store_plain_keys: bool
    max_keys_per_user: int
    default_group: str
    default_quota: int
    default_amount: float
    quota_per_amount_unit: int
    default_expire_days: int
    default_model_limits: str
    allow_ips: str
    delete_remote_on_user_delete: bool
    request_timeout_seconds: int


@dataclass(slots=True)
class KeyCreateOptions:
    name: str = ""
    group: str = ""
    amount: float | None = None
    expire_days: int | None = None
    model_limits: str = ""
    allow_ips: str = ""


class NewAPIError(RuntimeError):
    """NewAPI 调用失败。"""


class NewAPIClient:
    def __init__(
        self,
        base_url: str,
        admin_access_token: str,
        admin_user_id: int,
        timeout_seconds: int,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.admin_access_token = (admin_access_token or "").strip()
        self.admin_user_id = admin_user_id
        self.timeout = aiohttp.ClientTimeout(total=max(5, timeout_seconds))

    def configured(self) -> bool:
        return bool(self.base_url and self.admin_access_token and self.admin_user_id)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _admin_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.admin_access_token}",
            "Content-Type": "application/json",
        }
        if self.admin_user_id:
            headers["New-Api-User"] = str(self.admin_user_id)
        return headers

    @staticmethod
    def _unwrap_response(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        if payload.get("success") is False:
            message = payload.get("message") or payload.get("error") or "NewAPI 请求失败"
            raise NewAPIError(str(message))
        return payload.get("data", payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        admin: bool = True,
        api_key: str | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers: dict[str, str]
        if admin:
            headers = self._admin_headers()
        else:
            headers = {"Authorization": f"Bearer {api_key or ''}"}

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.request(
                method,
                self._url(path),
                headers=headers,
                json=json_body,
                params=params,
            ) as resp:
                text = await resp.text()
                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    payload = {"message": text}
                if resp.status >= 400:
                    message = (
                        payload.get("message")
                        if isinstance(payload, dict)
                        else None
                    ) or text or f"HTTP {resp.status}"
                    raise NewAPIError(str(message))
                return self._unwrap_response(payload)

    async def create_token(
        self,
        *,
        name: str,
        quota: int,
        expire_days: int,
        group: str,
        model_limits: str,
        allow_ips: str,
    ) -> tuple[int | None, str]:
        if not self.configured():
            raise NewAPIError("尚未配置 NewAPI 管理访问令牌或管理用户 ID")

        expired_time = -1
        if expire_days > 0:
            expired_time = int(time.time()) + expire_days * 86400

        payload = {
            "name": name,
            "expired_time": expired_time,
            "remain_quota": quota,
            "unlimited_quota": False,
            "model_limits_enabled": bool(model_limits.strip()),
            "model_limits": model_limits.strip(),
            "group": group.strip() or "default",
            "allow_ips": allow_ips.strip(),
            "cross_group_retry": False,
        }
        data = await self._request("POST", "/api/token/", json_body=payload)
        token_id = self._extract_token_id(data)
        if token_id is None:
            token = await self.find_token_by_name(name)
            token_id = self._extract_token_id(token)
        if token_id is None:
            raise NewAPIError("Token 已创建，但未能从 NewAPI 响应中找到 token id")

        key = await self.get_token_key(token_id)
        return token_id, key

    async def find_token_by_name(self, name: str) -> dict[str, Any] | None:
        candidates: list[Any] = []
        for path, params in (
            ("/api/token/search", {"keyword": name}),
            ("/api/token/", {"p": 0, "size": 50}),
        ):
            try:
                data = await self._request("GET", path, params=params)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[NewAPIKey] 查询 token 列表失败: {path} - {exc}")
                continue
            candidates.extend(self._extract_token_items(data))

        matches = [
            item
            for item in candidates
            if isinstance(item, dict) and str(item.get("name", "")) == name
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
        return matches[0]

    async def get_token_key(self, token_id: int) -> str:
        data = await self._request("POST", f"/api/token/{token_id}/key")
        key = self._extract_key(data)
        if not key:
            raise NewAPIError("NewAPI 未返回完整 key")
        return key

    async def delete_token(self, token_id: int) -> None:
        await self._request("DELETE", f"/api/token/{token_id}")

    async def update_token(
        self,
        *,
        token_id: int,
        name: str,
        quota: int,
        expire_days: int,
        group: str,
        model_limits: str,
        allow_ips: str,
    ) -> None:
        expired_time = -1
        if expire_days > 0:
            expired_time = int(time.time()) + expire_days * 86400

        payload = {
            "id": token_id,
            "name": name,
            "expired_time": expired_time,
            "remain_quota": quota,
            "unlimited_quota": False,
            "model_limits_enabled": bool(model_limits.strip()),
            "model_limits": model_limits.strip(),
            "group": group.strip() or "default",
            "allow_ips": allow_ips.strip(),
            "cross_group_retry": False,
        }
        await self._request("PUT", "/api/token/", json_body=payload)

    async def token_usage(self, key: str) -> Any:
        return await self._request(
            "GET",
            "/api/usage/token",
            admin=False,
            api_key=key,
        )

    @staticmethod
    def _extract_token_id(data: Any) -> int | None:
        if isinstance(data, dict):
            for key in ("id", "token_id"):
                value = data.get(key)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        pass
            nested = data.get("token")
            if isinstance(nested, dict):
                return NewAPIClient._extract_token_id(nested)
        return None

    @staticmethod
    def _extract_key(data: Any) -> str:
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            for key in ("key", "token", "value"):
                value = data.get(key)
                if value:
                    return str(value).strip()
            nested = data.get("data")
            if nested is not data:
                return NewAPIClient._extract_key(nested)
        return ""

    @staticmethod
    def _extract_token_items(data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in ("items", "tokens", "list", "rows", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = NewAPIClient._extract_token_items(value)
                if nested:
                    return nested
        return []


class KeyStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "users": {},
                "keys": {},
                "applications": {},
                "runtime_config": {},
                "next_application_id": 1,
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[NewAPIKey] 读取数据文件失败，使用空数据: {exc}")
            data = {}
        data.setdefault("users", {})
        data.setdefault("keys", {})
        data.setdefault("applications", {})
        data.setdefault("runtime_config", {})
        data.setdefault("next_application_id", 1)
        return data

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def user(self, qq_id: str) -> dict[str, Any]:
        users = self.data.setdefault("users", {})
        user = users.setdefault(
            qq_id,
            {
                "status": "active",
                "created_at": int(time.time()),
            },
        )
        return user

    def is_banned(self, qq_id: str) -> bool:
        return self.user(qq_id).get("status") == "banned"

    def set_user_status(self, qq_id: str, status: str) -> None:
        self.user(qq_id)["status"] = status
        self.save()

    def active_keys(self, qq_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.data.get("keys", {}).values()
            if item.get("qq_id") == qq_id and item.get("status") == "active"
        ]

    def keys_for_user(self, qq_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
        return [
            item
            for item in self.data.get("keys", {}).values()
            if item.get("qq_id") == qq_id
            and (not active_only or item.get("status") == "active")
        ]

    def all_keys(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        items = list(self.data.get("keys", {}).values())
        if active_only:
            items = [item for item in items if item.get("status") == "active"]
        return sorted(items, key=lambda item: int(item.get("created_at") or 0), reverse=True)

    def get_key(self, key_id: str) -> dict[str, Any] | None:
        item = self.data.get("keys", {}).get(key_id)
        return item if isinstance(item, dict) else None

    def add_key(self, record: dict[str, Any]) -> dict[str, Any]:
        key_id = record.get("id") or uuid.uuid4().hex[:8]
        record["id"] = key_id
        record.setdefault("status", "active")
        record.setdefault("created_at", int(time.time()))
        self.data.setdefault("keys", {})[key_id] = record
        self.save()
        return record

    def update_key(self, key_id: str, **updates: Any) -> None:
        item = self.get_key(key_id)
        if not item:
            return
        item.update(updates)
        item["updated_at"] = int(time.time())
        self.save()

    def add_application(self, qq_id: str, reason: str) -> dict[str, Any]:
        app_id = str(self.data.get("next_application_id", 1))
        self.data["next_application_id"] = int(app_id) + 1
        app = {
            "id": app_id,
            "qq_id": qq_id,
            "reason": reason.strip() or "未填写",
            "status": "pending",
            "created_at": int(time.time()),
        }
        self.data.setdefault("applications", {})[app_id] = app
        self.save()
        return app

    def pending_applications(self) -> list[dict[str, Any]]:
        items = [
            item
            for item in self.data.get("applications", {}).values()
            if item.get("status") == "pending"
        ]
        return sorted(items, key=lambda item: int(item.get("id") or 0))

    def get_application(self, app_id: str) -> dict[str, Any] | None:
        app = self.data.get("applications", {}).get(str(app_id))
        return app if isinstance(app, dict) else None

    def update_application(self, app_id: str, **updates: Any) -> None:
        app = self.get_application(app_id)
        if not app:
            return
        app.update(updates)
        app["updated_at"] = int(time.time())
        self.save()

    def runtime_config(self) -> dict[str, Any]:
        config = self.data.setdefault("runtime_config", {})
        return config if isinstance(config, dict) else {}

    def set_runtime_config(self, key: str, value: Any) -> None:
        config = self.data.setdefault("runtime_config", {})
        if not isinstance(config, dict):
            config = {}
            self.data["runtime_config"] = config
        if value in (None, ""):
            config.pop(key, None)
        else:
            config[key] = value
        self.save()

    def clear_runtime_config(self) -> None:
        self.data["runtime_config"] = {}
        self.save()


class NewAPIKeyDistributorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.store = KeyStore(self.data_dir / "newapi_key_distributor.json")
        self._migrate_runtime_config_to_plugin_config()
        self.settings = self._load_settings(config)
        self.client = self._build_client()

    async def initialize(self):
        logger.info(
            "[NewAPIKey] 插件加载完成: "
            f"base_url={self.settings.newapi_base_url}, data={self.store.path}"
        )

    @staticmethod
    def _cfg(config: AstrBotConfig, key: str, default: Any) -> Any:
        if hasattr(config, "get"):
            try:
                return config.get(key, default)
            except TypeError:
                value = config.get(key)
                return default if value is None else value
        if isinstance(config, dict):
            return config.get(key, default)
        return getattr(config, key, default)

    def _load_settings(self, config: AstrBotConfig) -> PluginSettings:
        return PluginSettings(
            newapi_base_url=str(
                self._cfg(config, "newapi_base_url", "https://newapi.qianye.host/")
            ).strip()
            or "https://newapi.qianye.host/",
            admin_access_token=str(self._cfg(config, "admin_access_token", "")).strip(),
            admin_user_id=_as_int(self._cfg(config, "admin_user_id", 1), 1),
            bot_admin_ids=set(_as_list(self._cfg(config, "bot_admin_ids", []))),
            auto_create_enabled=_as_bool(
                self._cfg(config, "auto_create_enabled", False), False
            ),
            private_only_for_secret=_as_bool(
                self._cfg(config, "private_only_for_secret", True), True
            ),
            store_plain_keys=_as_bool(
                self._cfg(config, "store_plain_keys", False), False
            ),
            max_keys_per_user=max(
                1, _as_int(self._cfg(config, "max_keys_per_user", 1), 1)
            ),
            default_group=str(
                self._cfg(config, "default_group", "浅夜の梦专属号池")
            ).strip()
            or "浅夜の梦专属号池",
            default_quota=_as_int(self._cfg(config, "default_quota", 500000), 500000),
            default_amount=_as_float(
                self._cfg(
                    config,
                    "default_amount",
                    _as_int(self._cfg(config, "default_quota", 500000), 500000)
                    / max(1, _as_int(self._cfg(config, "quota_per_amount_unit", 500000), 500000)),
                ),
                1.0,
            ),
            quota_per_amount_unit=max(
                1,
                _as_int(self._cfg(config, "quota_per_amount_unit", 500000), 500000),
            ),
            default_expire_days=_as_int(
                self._cfg(config, "default_expire_days", 0), 0
            ),
            default_model_limits=str(
                self._cfg(config, "default_model_limits", "")
            ).strip(),
            allow_ips=str(self._cfg(config, "allow_ips", "")).strip(),
            delete_remote_on_user_delete=_as_bool(
                self._cfg(config, "delete_remote_on_user_delete", False), False
            ),
            request_timeout_seconds=max(
                5,
                _as_int(self._cfg(config, "request_timeout_seconds", 20), 20),
            ),
            enable_chat_config=_as_bool(
                self._cfg(config, "enable_chat_config", True), True
            ),
        )

    def _save_plugin_config(self) -> None:
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()

    def _set_plugin_config(self, key: str, value: Any) -> None:
        try:
            self.config[key] = value
        except TypeError:
            setattr(self.config, key, value)
        self._save_plugin_config()

    def _remove_plugin_config(self, key: str) -> None:
        removed = False
        pop = getattr(self.config, "pop", None)
        if callable(pop):
            try:
                pop(key, None)
                removed = True
            except TypeError:
                try:
                    pop(key)
                    removed = True
                except Exception:
                    removed = False
        if not removed and hasattr(self.config, key):
            try:
                delattr(self.config, key)
                removed = True
            except Exception:
                removed = False
        if removed:
            self._save_plugin_config()

    def _migrate_runtime_config_to_plugin_config(self) -> None:
        runtime = self.store.runtime_config()
        if not runtime:
            return

        migrated = False
        for key in (
            "newapi_base_url",
            "admin_access_token",
            "admin_user_id",
            "bot_admin_ids",
        ):
            if key not in runtime:
                continue
            try:
                self.config[key] = runtime[key]
                migrated = True
            except TypeError:
                setattr(self.config, key, runtime[key])
                migrated = True

        if migrated:
            self._save_plugin_config()
            logger.info("[NewAPIKey] 已将旧运行时配置迁移到 AstrBot 插件配置")
        self.store.clear_runtime_config()

    def _build_client(self) -> NewAPIClient:
        return NewAPIClient(
            self.settings.newapi_base_url,
            self.settings.admin_access_token,
            self.settings.admin_user_id,
            self.settings.request_timeout_seconds,
        )

    def _refresh_runtime_settings(self) -> None:
        self.settings = self._load_settings(self.config)
        self.client = self._build_client()

    def _sender_id(self, event: AstrMessageEvent) -> str:
        get_sender_id = getattr(event, "get_sender_id", None)
        if callable(get_sender_id):
            try:
                value = get_sender_id()
                if value:
                    return str(value)
            except Exception:
                pass

        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for obj in (sender, message_obj, event):
            if not obj:
                continue
            for attr in ("user_id", "sender_id", "qq"):
                value = getattr(obj, attr, None)
                if value:
                    return str(value)
        return str(getattr(event, "unified_msg_origin", "unknown"))

    def _is_admin(self, qq_id: str) -> bool:
        return qq_id in self.settings.bot_admin_ids

    def _is_private_event(self, event: AstrMessageEvent) -> bool:
        for obj in (getattr(event, "message_obj", None), event):
            for attr in ("message_type", "type", "event_type"):
                value = str(getattr(obj, attr, "") or "").lower()
                if "group" in value:
                    return False
                if "private" in value or "friend" in value:
                    return True
        origin = str(getattr(event, "unified_msg_origin", "")).lower()
        if "group" in origin:
            return False
        return True

    def _requires_private(self, event: AstrMessageEvent) -> str | None:
        if self.settings.private_only_for_secret and not self._is_private_event(event):
            return "为避免泄露完整 Key，请私聊机器人执行这个命令。"
        return None

    def _requires_secret_private(self, event: AstrMessageEvent) -> str | None:
        if not self._is_private_event(event):
            return "完整 Key 只会通过私聊发送。请私聊机器人重新执行该命令，群聊中不会生成或展示完整 Key。"
        return None

    def _can_manage_config(self, event: AstrMessageEvent, qq_id: str) -> bool:
        if not self.settings.enable_chat_config:
            return False
        if self._is_admin(qq_id):
            return True
        # Bootstrap mode: if no admin is configured yet, allow the first private
        # operator to configure admin IDs and NewAPI credentials.
        return not self.settings.bot_admin_ids and self._is_private_event(event)

    def _runtime_config_summary(self) -> str:
        token = self.settings.admin_access_token
        admins = ", ".join(sorted(self.settings.bot_admin_ids)) or "未配置"
        return (
            "当前 NewAPI 运行配置：\n"
            f"地址：{self.settings.newapi_base_url or '未配置'}\n"
            f"管理用户 ID：{self.settings.admin_user_id or '未配置'}\n"
            f"Access Token：{_mask_key(token) if token else '未配置'}\n"
            f"组件管理员：{admins}\n"
            f"聊天配置：{'开启' if self.settings.enable_chat_config else '关闭'}"
        )

    def _too_many_keys_message(self, qq_id: str) -> str | None:
        count = len(self.store.active_keys(qq_id))
        if count >= self.settings.max_keys_per_user:
            return f"你当前已有 {count} 个 active Key，已达到每人上限 {self.settings.max_keys_per_user}。"
        return None

    async def _validate_user_key(self, key: str) -> str | None:
        try:
            await self.client.token_usage(key)
            return None
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    async def _create_key_for_user(
        self,
        qq_id: str,
        *,
        reason: str,
        created_by: str,
        options: KeyCreateOptions | None = None,
        bypass_user_limit: bool = False,
    ) -> tuple[dict[str, Any], str]:
        if self.store.is_banned(qq_id):
            raise NewAPIError("该用户已被封禁，无法创建 Key。")
        if not bypass_user_limit:
            too_many = self._too_many_keys_message(qq_id)
            if too_many:
                raise NewAPIError(too_many)

        options = options or KeyCreateOptions()
        token_name = options.name.strip() or f"qq_{qq_id}_{int(time.time())}"
        group = self.settings.default_group
        amount = (
            options.amount
            if options.amount is not None
            else self.settings.default_amount
        )
        if amount <= 0:
            raise NewAPIError("金额必须大于 0。请传入金额，或在配置里设置 default_amount。")
        quota = int(round(amount * self.settings.quota_per_amount_unit))
        expire_days = 0
        model_limits = self.settings.default_model_limits
        allow_ips = self.settings.allow_ips

        token_id, key = await self.client.create_token(
            name=token_name,
            quota=quota,
            expire_days=expire_days,
            group=group,
            model_limits=model_limits,
            allow_ips=allow_ips,
        )
        record = self.store.add_key(
            {
                "qq_id": qq_id,
                "source": "created",
                "token_id": token_id,
                "token_name": token_name,
                "key_masked": _mask_key(key),
                "key_plain": key if self.settings.store_plain_keys else "",
                "group": group,
                "amount": amount,
                "quota": quota,
                "quota_per_amount_unit": self.settings.quota_per_amount_unit,
                "expire_days": expire_days,
                "model_limits": model_limits,
                "allow_ips": allow_ips,
                "reason": reason,
                "created_by": created_by,
            }
        )
        return record, key

    def _format_key_record(self, item: dict[str, Any]) -> str:
        token_id = item.get("token_id") or "-"
        token_name = item.get("token_name") or "-"
        source = item.get("source") or "-"
        amount = item.get("amount")
        quota = item.get("quota") or "-"
        group = item.get("group") or "-"
        amount_text = f"金额:{amount} | " if amount not in (None, "") else ""
        return (
            f"{item.get('id')} | {item.get('key_masked')} | "
            f"名称:{token_name} | 来源:{source} | token:{token_id} | "
            f"分组:{group} | {amount_text}原生额度:{quota}"
        )

    def _parse_create_options(self, text: str) -> KeyCreateOptions:
        options = KeyCreateOptions()
        if not text.strip():
            return options

        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()

        key_aliases = {
            "name": "name",
            "名称": "name",
            "名字": "name",
            "quota": "amount",
            "amount": "amount",
            "money": "amount",
            "额度": "amount",
            "限额": "amount",
            "金额": "amount",
        }
        positional: list[str] = []

        for token in tokens:
            if "=" not in token:
                positional.append(token)
                continue
            raw_key, value = token.split("=", 1)
            field = key_aliases.get(raw_key.strip().lower()) or key_aliases.get(
                raw_key.strip()
            )
            if not field:
                raise ValueError(f"不支持参数 {raw_key}，请只传名称和金额")
            value = value.strip()
            if field == "amount":
                parsed_amount = _as_float(value, -1)
                if parsed_amount <= 0:
                    raise ValueError(f"{raw_key} 必须是大于 0 的金额")
                options.amount = parsed_amount
            else:
                setattr(options, field, value)

        # Positional shorthand for admins:
        # name amount
        if positional and not options.name:
            options.name = positional[0]
        rest_positional = positional[1:]
        if rest_positional:
            if options.amount is not None:
                raise ValueError("只支持传入名称和金额，不再接收分组或过期天数")
            amount = _as_float(rest_positional[0], -1)
            if amount <= 0:
                raise ValueError("第二个参数必须是大于 0 的金额，不再接收分组")
            options.amount = amount

        if len(rest_positional) > 1:
            raise ValueError("只支持传入名称和金额，不再接收分组或过期天数")

        return options

    def _format_create_options(self, record: dict[str, Any]) -> str:
        expire_days = _as_int(record.get("expire_days"), 0)
        expire_text = "永不过期" if expire_days == 0 else f"{expire_days} 天"
        return (
            f"名称：{record.get('token_name')}\n"
            f"分组：{record.get('group')}\n"
            f"金额：{record.get('amount')}\n"
            f"原生额度：{record.get('quota')}\n"
            f"有效期：{expire_text}\n"
            f"模型限制：{record.get('model_limits') or '不限制'}"
        )

    def _format_usage(self, usage: Any) -> str:
        if isinstance(usage, dict):
            parts = []
            for key in (
                "used_quota",
                "remain_quota",
                "quota",
                "total_quota",
                "request_count",
                "used_amount",
            ):
                if key in usage:
                    parts.append(f"{key}: {usage[key]}")
            if parts:
                return "\n".join(parts)
            return json.dumps(usage, ensure_ascii=False, indent=2)
        return str(usage)

    @filter.command("key", alias=["apikey", "newapi"])
    async def key_command(self, event: AstrMessageEvent):
        args = _extract_command_args(event.message_str)
        qq_id = self._sender_id(event)
        if not args or args in {"help", "帮助"}:
            yield event.plain_result(self._help_text())
            return

        cmd, _, rest = args.partition(" ")
        cmd = cmd.strip().lower()
        rest = rest.strip()

        if self.store.is_banned(qq_id) and cmd not in {"查看", "状态", "帮助", "help", "配置", "设置", "config"}:
            yield event.plain_result("你已被禁止使用 Key 分发功能。")
            return

        if cmd in {"配置", "设置", "config"}:
            yield event.plain_result(self._handle_config(event, qq_id, rest))
        elif cmd in {"绑定", "填写", "bind"}:
            async for result in self._handle_bind(event, qq_id, rest):
                yield result
        elif cmd in {"申请", "apply"}:
            yield event.plain_result(self._handle_apply(qq_id, rest))
        elif cmd in {"创建", "create"}:
            async for result in self._handle_create(event, qq_id, rest):
                yield result
        elif cmd in {"查看", "我的", "list", "状态"}:
            yield event.plain_result(self._handle_list(qq_id, rest))
        elif cmd in {"用量", "usage"}:
            async for result in self._handle_usage(event, qq_id, rest):
                yield result
        elif cmd in {"删除", "delete"}:
            async for result in self._handle_delete(event, qq_id, rest):
                yield result
        elif cmd in {"修改", "edit", "update"}:
            async for result in self._handle_update_key(event, qq_id, rest):
                yield result
        elif cmd in {"修改名称", "改名", "名称", "rename"}:
            async for result in self._handle_update_key_field(event, qq_id, rest, "name"):
                yield result
        elif cmd in {"修改金额", "金额", "额度", "amount"}:
            async for result in self._handle_update_key_field(event, qq_id, rest, "amount"):
                yield result
        elif cmd in {"加额", "加额度", "充值", "topup", "addquota"}:
            async for result in self._handle_add_quota(event, qq_id, rest):
                yield result
        elif cmd in {"审核", "review"}:
            yield event.plain_result(self._handle_review_list(qq_id))
        elif cmd in {"通过", "approve"}:
            async for result in self._handle_approve(event, qq_id, rest):
                yield result
        elif cmd in {"生成", "发放", "issue"}:
            async for result in self._handle_admin_issue(event, qq_id, rest):
                yield result
        elif cmd in {"拒绝", "reject"}:
            yield event.plain_result(self._handle_reject(qq_id, rest))
        elif cmd in {"封禁", "ban"}:
            yield event.plain_result(self._handle_user_status(qq_id, rest, "banned"))
        elif cmd in {"解封", "unban"}:
            yield event.plain_result(self._handle_user_status(qq_id, rest, "active"))
        elif cmd in {"检查", "check"}:
            async for result in self._handle_check(event, qq_id):
                yield result
        else:
            yield event.plain_result("未知子命令。发送 /key 帮助 查看用法。")

    def _help_text(self) -> str:
        return (
            "NewAPI Key 分发命令：\n"
            "/key 绑定 sk-xxx - 绑定已有 Key\n"
            "/key 申请 用途说明 - 提交申请\n"
            "/key 创建 [用途说明] - 自助创建或提交申请\n"
            "/key 查看 - 查看我的 Key\n"
            "/key 用量 [Key记录ID] - 查询用量（需保存完整 Key）\n"
            "/key 删除 <Key记录ID> - 删除/停用本地记录\n\n"
            "管理员：\n"
            "/key 配置 查看 - 查看 NewAPI 管理配置\n"
            "/key 配置 token <Access Token> - 写入管理访问令牌\n"
            "/key 配置 user <用户ID> - 写入 NewAPI 管理用户 ID\n"
            "/key 配置 url <地址> - 写入 NewAPI 地址\n"
            "/key 配置 管理员 添加 <QQ> - 添加组件管理员\n"
            "/key 审核 - 查看待审核申请\n"
            "/key 通过 <申请ID> [姓名] [金额] - 创建并发放 Key\n"
            "/key 生成 <QQ> <姓名> [金额] - 主动发放 Key\n"
            "/key 修改 <记录ID|QQ> [姓名] [金额] - 修改 Key\n"
            "/key 加额 <记录ID|QQ> <金额> - 给已有 Key 增加额度\n"
            "/key 拒绝 <申请ID> 原因\n"
            "/key 封禁 <QQ> / /key 解封 <QQ>\n"
            "/key 检查 - 检查 NewAPI 管理配置"
        )

    def _handle_config(self, event: AstrMessageEvent, qq_id: str, rest: str) -> str:
        if not self._can_manage_config(event, qq_id):
            return "没有权限，或聊天配置功能未开启。"
        if not self._is_private_event(event):
            return "配置 Access Token 等敏感信息时，请私聊机器人执行。"

        if not rest or rest in {"查看", "list", "show"}:
            return self._runtime_config_summary()
        if rest in {"刷新", "重载", "reload", "refresh"}:
            self._refresh_runtime_settings()
            return "已重新读取 AstrBot 插件配置。"

        key, _, value = rest.partition(" ")
        key = key.strip().lower()
        value = value.strip()

        if key in {"帮助", "help"}:
            return (
                "配置命令：\n"
                "/key 配置 查看\n"
                "/key 配置 token <Access Token>\n"
                "/key 配置 user <NewAPI用户ID>\n"
                "/key 配置 url <NewAPI地址>\n"
                "/key 配置 管理员 添加 <QQ>\n"
                "/key 配置 管理员 删除 <QQ>\n"
                "/key 配置 刷新\n"
                "/key 配置 清除 token|user|url|管理员"
            )

        if key in {"token", "access", "access_token"}:
            if not value:
                return "用法：/key 配置 token <Access Token>"
            self._set_plugin_config("admin_access_token", value)
            self._refresh_runtime_settings()
            return f"已保存 Access Token：{_mask_key(value)}"

        if key in {"user", "user_id", "userid", "用户"}:
            user_id = _as_int(value, 0)
            if user_id <= 0:
                return "用法：/key 配置 user <NewAPI用户ID>"
            self._set_plugin_config("admin_user_id", user_id)
            self._refresh_runtime_settings()
            return f"已保存 NewAPI 管理用户 ID：{user_id}"

        if key in {"url", "base_url", "地址"}:
            if not value:
                return "用法：/key 配置 url <NewAPI地址>"
            self._set_plugin_config("newapi_base_url", value)
            self._refresh_runtime_settings()
            return f"已保存 NewAPI 地址：{value}"

        if key in {"管理员", "admin", "admins"}:
            action, _, target = value.partition(" ")
            action = action.strip().lower()
            target = target.strip()
            admins = set(self.settings.bot_admin_ids)

            if not action or action in {"查看", "list", "show"}:
                return "组件管理员：" + (", ".join(sorted(admins)) or "未配置")
            if action in {"添加", "add"}:
                if not target:
                    return "用法：/key 配置 管理员 添加 <QQ>"
                admins.add(target)
                self._set_plugin_config("bot_admin_ids", sorted(admins))
                self._refresh_runtime_settings()
                return f"已添加组件管理员：{target}"
            if action in {"删除", "remove", "del"}:
                if not target:
                    return "用法：/key 配置 管理员 删除 <QQ>"
                admins.discard(target)
                self._set_plugin_config("bot_admin_ids", sorted(admins))
                self._refresh_runtime_settings()
                return f"已删除组件管理员：{target}"
            return "用法：/key 配置 管理员 添加|删除|查看 <QQ>"

        if key in {"清除", "clear", "reset"}:
            mapping = {
                "token": "admin_access_token",
                "access": "admin_access_token",
                "access_token": "admin_access_token",
                "user": "admin_user_id",
                "user_id": "admin_user_id",
                "url": "newapi_base_url",
                "base_url": "newapi_base_url",
                "管理员": "bot_admin_ids",
                "admin": "bot_admin_ids",
            }
            config_key = mapping.get(value.lower())
            if not config_key:
                return "用法：/key 配置 清除 token|user|url|管理员"
            self._remove_plugin_config(config_key)
            self._refresh_runtime_settings()
            return f"已清除插件配置：{value}"

        return "未知配置项。发送 /key 配置 帮助 查看用法。"

    async def _handle_bind(self, event: AstrMessageEvent, qq_id: str, key: str):
        private_msg = self._requires_secret_private(event)
        if private_msg:
            yield event.plain_result(private_msg)
            return
        if not key:
            yield event.plain_result("用法：/key 绑定 sk-xxx")
            return
        too_many = self._too_many_keys_message(qq_id)
        if too_many:
            yield event.plain_result(too_many)
            return

        error = await self._validate_user_key(key)
        if error:
            yield event.plain_result(f"Key 校验失败：{error}")
            return

        record = self.store.add_key(
            {
                "qq_id": qq_id,
                "source": "bound",
                "token_id": None,
                "token_name": "",
                "key_masked": _mask_key(key),
                "key_plain": key if self.settings.store_plain_keys else "",
                "group": "",
                "quota": "",
                "reason": "用户绑定已有 Key",
                "created_by": qq_id,
            }
        )
        suffix = "，已保存完整 Key，可查询用量。" if self.settings.store_plain_keys else "，未保存完整 Key。"
        yield event.plain_result(f"绑定成功：{record['id']} | {record['key_masked']}{suffix}")

    def _handle_apply(self, qq_id: str, reason: str) -> str:
        if self.store.is_banned(qq_id):
            return "你已被禁止申请 Key。"
        app = self.store.add_application(qq_id, reason)
        return f"申请已提交，编号：{app['id']}。请等待管理员审核。"

    async def _handle_create(self, event: AstrMessageEvent, qq_id: str, reason: str):
        private_msg = self._requires_secret_private(event)
        if private_msg:
            yield event.plain_result(private_msg)
            return

        if not self._is_admin(qq_id) and not self.settings.auto_create_enabled:
            app = self.store.add_application(qq_id, reason or "用户请求自助创建 Key")
            yield event.plain_result(
                f"当前未开放自助创建，已自动转为申请，编号：{app['id']}。"
            )
            return

        try:
            record, key = await self._create_key_for_user(
                qq_id,
                reason=reason or "用户自助创建",
                created_by=qq_id,
                bypass_user_limit=self._is_admin(qq_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[NewAPIKey] 创建 Key 失败: {exc}", exc_info=True)
            yield event.plain_result(f"创建失败：{exc}")
            return

        yield event.plain_result(
            "创建成功，请立即保存完整 Key：\n"
            f"记录ID：{record['id']}\n"
            f"Key：{key}\n"
            f"脱敏：{record['key_masked']}"
        )

    def _handle_list(self, qq_id: str, target: str = "") -> str:
        target = (target or "").strip()
        if self._is_admin(qq_id):
            if target in {"自己", "me"}:
                items = self.store.active_keys(qq_id)
                title = "你的 Key："
            elif target:
                items = self.store.keys_for_user(target, active_only=True)
                title = f"{target} 的 active Key："
            else:
                items = self.store.all_keys(active_only=True)
                title = "全部 active Key："
        else:
            items = self.store.active_keys(qq_id)
            title = "你的 Key："

        if not items:
            return "没有 active Key。"
        lines = [title]
        lines.extend(self._format_key_record(item) for item in items[:50])
        if len(items) > 50:
            lines.append(f"... 还有 {len(items) - 50} 条未显示")
        return "\n".join(lines)

    async def _handle_usage(self, event: AstrMessageEvent, qq_id: str, key_id: str):
        items = self.store.active_keys(qq_id)
        if key_id and self._is_admin(qq_id):
            item = self.store.get_key(key_id)
            if item:
                items = [item]
            else:
                user_items = self.store.keys_for_user(key_id, active_only=True)
                if len(user_items) == 1:
                    items = user_items
                elif len(user_items) > 1:
                    yield event.plain_result("该 QQ 有多条 active Key，请使用记录 ID 查询。")
                    return
                else:
                    items = []
        elif key_id:
            items = [item for item in items if item.get("id") == key_id]
        if not items:
            yield event.plain_result("没有找到可查询的 Key 记录。")
            return
        item = items[0]
        key = item.get("key_plain") or ""
        if not key:
            yield event.plain_result(
                "当前配置未保存完整 Key，无法直接查询用量。可开启 store_plain_keys 后重新绑定，或去 NewAPI 面板查看。"
            )
            return
        try:
            usage = await self.client.token_usage(key)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"查询失败：{exc}")
            return
        yield event.plain_result(
            f"{item.get('id')} | {item.get('key_masked')}\n{self._format_usage(usage)}"
        )

    async def _handle_delete(self, event: AstrMessageEvent, qq_id: str, key_id: str):
        if not key_id:
            yield event.plain_result("用法：/key 删除 <Key记录ID>；管理员也可 /key 删除 <QQ>")
            return
        items: list[dict[str, Any]]
        item = self.store.get_key(key_id)
        if item:
            if item.get("qq_id") != qq_id and not self._is_admin(qq_id):
                yield event.plain_result("没有权限删除这个 Key 记录。")
                return
            items = [item]
        elif self._is_admin(qq_id):
            items = self.store.keys_for_user(key_id, active_only=True)
            if not items:
                yield event.plain_result("没有找到这个记录 ID，或该 QQ 没有 active Key。")
                return
        else:
            yield event.plain_result("没有找到属于你的这个 Key 记录。")
            return

        remote_deleted = 0
        remote_failed = 0
        for item in items:
            if (
                self.settings.delete_remote_on_user_delete
                and item.get("token_id")
                and self.client.configured()
            ):
                try:
                    await self.client.delete_token(int(item["token_id"]))
                    remote_deleted += 1
                except Exception as exc:  # noqa: BLE001
                    remote_failed += 1
                    logger.warning(f"[NewAPIKey] 删除远程 token 失败: {exc}")
            self.store.update_key(
                str(item["id"]),
                status="deleted",
                deleted_at=int(time.time()),
                deleted_by=qq_id,
            )
        extra = ""
        if remote_deleted or remote_failed:
            extra = f"，远程删除成功 {remote_deleted} 个，失败 {remote_failed} 个"
        yield event.plain_result(f"已删除本地记录 {len(items)} 条{extra}。")

    def _resolve_edit_target(
        self,
        operator_qq: str,
        target: str,
        action: str = "修改",
    ) -> tuple[dict[str, Any] | None, str | None]:
        item = self.store.get_key(target)
        if item:
            if item.get("qq_id") != operator_qq and not self._is_admin(operator_qq):
                return None, f"没有权限{action}这个 Key 记录。"
            return item, None

        if self._is_admin(operator_qq):
            items = self.store.keys_for_user(target, active_only=True)
            if len(items) == 1:
                return items[0], None
            if len(items) > 1:
                return None, f"该 QQ 有多条 active Key，请使用记录 ID {action}。"

        return None, f"没有找到可{action}的 Key 记录。"

    def _amount_from_record(self, item: dict[str, Any]) -> float:
        amount = _as_float(item.get("amount"), -1)
        if amount > 0:
            return amount
        return _as_float(item.get("quota"), 0) / max(
            1,
            _as_int(
                item.get("quota_per_amount_unit"),
                self.settings.quota_per_amount_unit,
            ),
        )

    def _split_update_field(self, text: str) -> tuple[str | None, str]:
        if not text.strip():
            return None, ""
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        if not tokens:
            return None, ""

        aliases = {
            "name": "name",
            "名称": "name",
            "名字": "name",
            "姓名": "name",
            "amount": "amount",
            "money": "amount",
            "金额": "amount",
            "额度": "amount",
            "quota": "amount",
        }
        field = aliases.get(tokens[0].strip().lower()) or aliases.get(tokens[0].strip())
        if not field:
            return None, text
        value = " ".join(tokens[1:]).strip()
        return field, value

    async def _sync_key_update(
        self,
        item: dict[str, Any],
        *,
        operator_qq: str,
        token_name: str | None = None,
        group: str | None = None,
        amount: float | None = None,
        expire_days: int | None = None,
        model_limits: str | None = None,
        allow_ips: str | None = None,
        action: str = "修改",
        extra_updates: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, bool]:
        token_name = str(token_name or item.get("token_name") or f"qq_{item.get('qq_id')}")
        group = self.settings.default_group
        amount = amount if amount is not None else self._amount_from_record(item)
        if amount <= 0:
            return None, "金额必须大于 0。", False
        quota = int(round(amount * self.settings.quota_per_amount_unit))
        expire_days = 0
        model_limits_value = (
            item.get("model_limits") if model_limits is None else model_limits
        )
        model_limits = str(model_limits_value or self.settings.default_model_limits)
        allow_ips_value = item.get("allow_ips") if allow_ips is None else allow_ips
        allow_ips = str(allow_ips_value or self.settings.allow_ips)

        remote_updated = False
        if item.get("token_id") and self.client.configured():
            try:
                await self.client.update_token(
                    token_id=int(item["token_id"]),
                    name=token_name,
                    quota=quota,
                    expire_days=expire_days,
                    group=group,
                    model_limits=model_limits,
                    allow_ips=allow_ips,
                )
                remote_updated = True
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[NewAPIKey] {action}远程 token 失败: {exc}", exc_info=True)
                return None, f"远程 token {action}失败：{exc}", False

        updates = {
            "token_name": token_name,
            "group": group,
            "amount": amount,
            "quota": quota,
            "quota_per_amount_unit": self.settings.quota_per_amount_unit,
            "expire_days": expire_days,
            "model_limits": model_limits,
            "allow_ips": allow_ips,
            "updated_by": operator_qq,
        }
        if extra_updates:
            updates.update(extra_updates)
        self.store.update_key(str(item["id"]), **updates)
        return self.store.get_key(str(item["id"])) or item, None, remote_updated

    async def _handle_update_key(self, event: AstrMessageEvent, qq_id: str, rest: str):
        target, _, option_text = rest.partition(" ")
        target = target.strip()
        if not target or not option_text.strip():
            yield event.plain_result(
                "用法：/key 修改 <记录ID|QQ> [姓名] [金额]\n"
                "示例：/key 修改 ab12cd34 张三 1000000"
            )
            return

        item, error = self._resolve_edit_target(qq_id, target)
        if error:
            yield event.plain_result(error)
            return
        assert item is not None

        field, value = self._split_update_field(option_text)
        if field:
            async for result in self._handle_update_key_field(
                event,
                qq_id,
                f"{target} {value}".strip(),
                field,
            ):
                yield result
            return

        try:
            options = self._parse_create_options(option_text)
        except ValueError as exc:
            yield event.plain_result(f"参数错误：{exc}")
            return

        token_name = options.name.strip() or str(item.get("token_name") or "")
        amount = (
            options.amount
            if options.amount is not None
            else self._amount_from_record(item)
        )
        updated, error, remote_updated = await self._sync_key_update(
            item,
            operator_qq=qq_id,
            token_name=token_name,
            amount=amount,
            action="修改",
        )
        if error:
            yield event.plain_result(error)
            return
        assert updated is not None
        suffix = "，远程 token 已同步修改" if remote_updated else ""
        yield event.plain_result(
            f"已修改 Key 记录：{item['id']}{suffix}\n"
            f"{self._format_create_options(updated)}"
        )

    async def _handle_update_key_field(
        self,
        event: AstrMessageEvent,
        qq_id: str,
        rest: str,
        field: str,
    ):
        target, _, value = rest.partition(" ")
        target = target.strip()
        value = value.strip()
        field_names = {
            "name": "名称",
            "amount": "金额",
        }
        field_name = field_names.get(field, field)
        if not target or not value:
            suffix = f" <{field_name}>"
            yield event.plain_result(
                f"用法：/key 修改{field_name} <记录ID|QQ>{suffix}\n"
                f"示例：/key 修改{field_name} ab12cd34 {self._example_value(field)}"
            )
            return

        item, error = self._resolve_edit_target(qq_id, target, action=f"修改{field_name}")
        if error:
            yield event.plain_result(error)
            return
        assert item is not None

        updates: dict[str, Any] = {}
        if field == "name":
            updates["token_name"] = value
        elif field == "amount":
            amount = _as_float(value, -1)
            if amount <= 0:
                yield event.plain_result("金额必须是大于 0 的数字。")
                return
            updates["amount"] = amount
        else:
            yield event.plain_result("不支持的修改项。")
            return

        updated, sync_error, remote_updated = await self._sync_key_update(
            item,
            operator_qq=qq_id,
            token_name=updates.get("token_name"),
            amount=updates.get("amount"),
            action=f"修改{field_name}",
        )
        if sync_error:
            yield event.plain_result(sync_error)
            return
        assert updated is not None
        suffix = "，远程 token 已同步修改" if remote_updated else ""
        yield event.plain_result(
            f"已修改 Key {field_name}：{item['id']}{suffix}\n"
            f"{self._format_create_options(updated)}"
        )

    @staticmethod
    def _example_value(field: str) -> str:
        examples = {
            "name": "张三",
            "amount": "1000000",
        }
        return examples.get(field, "值")

    async def _handle_add_quota(self, event: AstrMessageEvent, admin_qq: str, rest: str):
        if not self._is_admin(admin_qq):
            yield event.plain_result("没有权限。")
            return

        target, _, amount_text = rest.partition(" ")
        target = target.strip()
        amount_text = amount_text.strip()
        if not target or not amount_text:
            yield event.plain_result(
                "用法：/key 加额 <记录ID|QQ> <金额>\n"
                "示例：/key 加额 ab12cd34 100000"
            )
            return

        item, error = self._resolve_edit_target(admin_qq, target, action="加额")
        if error:
            yield event.plain_result(error)
            return
        assert item is not None

        add_amount = _as_float(amount_text, -1)
        if add_amount <= 0:
            yield event.plain_result("增加金额必须是大于 0 的数字。")
            return

        if not item.get("token_id"):
            yield event.plain_result("该记录没有 NewAPI token_id，无法同步远程额度。")
            return
        if not self.client.configured():
            yield event.plain_result("NewAPI 管理接口未配置，请先配置 url、token 和 user。")
            return

        old_amount = _as_float(item.get("amount"), -1)
        if old_amount < 0:
            old_amount = _as_float(item.get("quota"), 0) / max(
                1,
                _as_int(
                    item.get("quota_per_amount_unit"),
                    self.settings.quota_per_amount_unit,
                ),
            )
        new_amount = old_amount + add_amount
        quota = int(round(new_amount * self.settings.quota_per_amount_unit))
        token_name = str(item.get("token_name") or f"qq_{item.get('qq_id')}")
        group = self.settings.default_group
        expire_days = 0
        model_limits = str(
            item.get("model_limits") or self.settings.default_model_limits
        )
        allow_ips = str(item.get("allow_ips") or self.settings.allow_ips)

        try:
            await self.client.update_token(
                token_id=int(item["token_id"]),
                name=token_name,
                quota=quota,
                expire_days=expire_days,
                group=group,
                model_limits=model_limits,
                allow_ips=allow_ips,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[NewAPIKey] 增加远程 token 额度失败: {exc}", exc_info=True)
            yield event.plain_result(f"远程 token 加额失败：{exc}")
            return

        self.store.update_key(
            str(item["id"]),
            token_name=token_name,
            group=group,
            amount=new_amount,
            quota=quota,
            quota_per_amount_unit=self.settings.quota_per_amount_unit,
            expire_days=expire_days,
            model_limits=model_limits,
            allow_ips=allow_ips,
            last_topup_amount=add_amount,
            last_topup_at=int(time.time()),
            updated_by=admin_qq,
        )
        yield event.plain_result(
            f"已给 Key 记录加额：{item['id']}，远程 token 已同步\n"
            f"原金额：{old_amount}\n"
            f"增加金额：{add_amount}\n"
            f"当前金额：{new_amount}\n"
            f"当前原生额度：{quota}"
        )

    def _handle_review_list(self, qq_id: str) -> str:
        if not self._is_admin(qq_id):
            return "没有权限。"
        apps = self.store.pending_applications()
        if not apps:
            return "暂无待审核申请。"
        lines = ["待审核申请："]
        for app in apps[:20]:
            lines.append(
                f"{app['id']} | QQ:{app['qq_id']} | 理由:{app.get('reason', '')}"
            )
        return "\n".join(lines)

    async def _handle_approve(self, event: AstrMessageEvent, admin_qq: str, app_id: str):
        if not self._is_admin(admin_qq):
            yield event.plain_result("没有权限。")
            return
        private_msg = self._requires_secret_private(event)
        if private_msg:
            yield event.plain_result(private_msg)
            return
        if not app_id:
            yield event.plain_result(
                "用法：/key 通过 <申请ID> [姓名] [金额]"
            )
            return
        app_id, _, option_text = app_id.partition(" ")
        app = self.store.get_application(app_id)
        if not app:
            yield event.plain_result("申请不存在。")
            return
        if app.get("status") != "pending":
            yield event.plain_result(f"申请当前状态不是 pending：{app.get('status')}")
            return
        try:
            options = self._parse_create_options(option_text)
        except ValueError as exc:
            yield event.plain_result(f"参数错误：{exc}")
            return
        try:
            record, key = await self._create_key_for_user(
                str(app["qq_id"]),
                reason=str(app.get("reason", "")),
                created_by=admin_qq,
                options=options,
                bypass_user_limit=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[NewAPIKey] 审批创建 Key 失败: {exc}", exc_info=True)
            yield event.plain_result(f"审批失败：{exc}")
            return
        self.store.update_application(
            app_id,
            status="approved",
            reviewer=admin_qq,
            key_id=record["id"],
        )
        yield event.plain_result(
            "已通过并创建 Key。请管理员私聊转交给申请人：\n"
            f"申请ID：{app_id}\n"
            f"QQ：{app['qq_id']}\n"
            f"记录ID：{record['id']}\n"
            f"{self._format_create_options(record)}\n"
            f"Key：{key}\n"
            f"脱敏：{record['key_masked']}"
        )

    async def _handle_admin_issue(self, event: AstrMessageEvent, admin_qq: str, rest: str):
        if not self._is_admin(admin_qq):
            yield event.plain_result("没有权限。")
            return
        private_msg = self._requires_secret_private(event)
        if private_msg:
            yield event.plain_result(private_msg)
            return
        target_qq, _, option_text = rest.partition(" ")
        target_qq = target_qq.strip()
        if not target_qq:
            yield event.plain_result(
                "用法：/key 生成 <QQ> <姓名> [金额]"
            )
            return
        try:
            options = self._parse_create_options(option_text)
        except ValueError as exc:
            yield event.plain_result(f"参数错误：{exc}")
            return
        if not options.name.strip():
            yield event.plain_result(
                "用法：/key 生成 <QQ> <姓名> [金额]\n"
                "示例：/key 生成 123456789 张三 1000000"
            )
            return
        try:
            record, key = await self._create_key_for_user(
                target_qq,
                reason="管理员主动发放",
                created_by=admin_qq,
                options=options,
                bypass_user_limit=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[NewAPIKey] 管理员主动发放 Key 失败: {exc}", exc_info=True)
            yield event.plain_result(f"创建失败：{exc}")
            return

        yield event.plain_result(
            "已主动生成 Key。请管理员私聊转交给用户：\n"
            f"QQ：{target_qq}\n"
            f"记录ID：{record['id']}\n"
            f"{self._format_create_options(record)}\n"
            f"Key：{key}\n"
            f"脱敏：{record['key_masked']}"
        )

    def _handle_reject(self, admin_qq: str, rest: str) -> str:
        if not self._is_admin(admin_qq):
            return "没有权限。"
        app_id, _, reason = rest.partition(" ")
        if not app_id:
            return "用法：/key 拒绝 <申请ID> 原因"
        app = self.store.get_application(app_id)
        if not app:
            return "申请不存在。"
        if app.get("status") != "pending":
            return f"申请当前状态不是 pending：{app.get('status')}"
        self.store.update_application(
            app_id,
            status="rejected",
            reviewer=admin_qq,
            reject_reason=reason.strip() or "未填写",
        )
        return f"已拒绝申请 {app_id}。"

    def _handle_user_status(self, admin_qq: str, rest: str, status: str) -> str:
        if not self._is_admin(admin_qq):
            return "没有权限。"
        qq_id = rest.strip().split(maxsplit=1)[0] if rest.strip() else ""
        if not qq_id:
            return "请提供 QQ 号。"
        self.store.set_user_status(qq_id, status)
        return f"已将 {qq_id} 状态设置为 {status}。"

    async def _handle_check(self, event: AstrMessageEvent, qq_id: str):
        if not self._is_admin(qq_id):
            yield event.plain_result("没有权限。")
            return
        if not self.client.configured():
            yield event.plain_result("NewAPI 管理配置不完整：请填写 admin_access_token 和 admin_user_id。")
            return
        try:
            await self.client._request("GET", "/api/token/", params={"p": 0, "size": 1})
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"NewAPI 管理接口检查失败：{exc}")
            return
        yield event.plain_result("NewAPI 管理接口检查通过。")
