from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    source_type: Literal["local", "panel"] = "local"
    local_snapshot_path: str = Field(default="/var/lib/session-watch-local/snapshot.json", max_length=1000)
    monitor_enabled: bool = False
    source_name: str = Field(default="我的 Xray 节点", min_length=1, max_length=80)
    panel_url: str = Field(default="", max_length=1000)
    panel_username: str = Field(default="", max_length=128)
    panel_password: str = Field(default="", max_length=512)
    panel_totp: str = Field(default="", max_length=16)
    online_api: Literal["auto", "clients", "inbounds"] = "auto"
    log_path: str = Field(default="", max_length=1000)
    poll_seconds: int = Field(default=10, ge=5, le=60)
    offline_seconds: int = Field(default=300, ge=300, le=3600)
    timezone: str = "Asia/Shanghai"
    log_timezone: str = "Asia/Shanghai"
    top_targets: int = Field(default=20, ge=1, le=50)
    retention_days: int = Field(default=30, ge=1, le=365)
    telegram_enabled: bool = False
    delivery_paused: bool = False
    ip_location_enabled: bool = False
    telegram_token: str = Field(default="", max_length=256)
    telegram_chat_id: str = Field(default="", max_length=128)

    @field_validator("panel_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        if not value:
            return value
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("请输入不含账号、查询参数的 HTTP(S) 面板根地址（可含面板路径）")
        return value.rstrip("/")

    @field_validator("timezone", "log_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("无效时区")
        return value


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)


class UserUpdate(BaseModel):
    alias: str = Field(max_length=80)
    monitored: bool = True

