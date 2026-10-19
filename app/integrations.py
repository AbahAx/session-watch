import re
import time

import httpx

from app.models import Settings


class IntegrationError(Exception):
    """Only safe, credential-free messages may cross this boundary."""


class PanelClient:
    """Fixed query allowlist; no inbound/client mutations or counter resets."""

    ONLINE_PATHS = {"clients": "panel/api/clients/onlines", "inbounds": "panel/api/inbounds/onlines"}

    def __init__(self):
        self.client = None
        self.identity = None
        self.selected_path = None
        self.csrf = None
        self.failed_identity = None
        self.retry_login_at = 0

    async def close(self):
        if self.client:
            await self.client.aclose()
        self.client = None
        self.identity = None
        self.selected_path = None
        self.csrf = None

    async def connect(self, settings: Settings):
        identity = (settings.panel_url, settings.panel_username, settings.panel_password, settings.panel_totp, settings.online_api)
        if self.identity == identity and self.client:
            return
        if self.failed_identity == identity and time.monotonic() < self.retry_login_at:
            raise IntegrationError("面板登录曾失败，冷却 5 分钟后重试；也可更新连接凭据")
        await self.close()
        if not all([settings.panel_url, settings.panel_username, settings.panel_password]):
            raise IntegrationError("请先填写面板地址、账号和密码")
        self.client = httpx.AsyncClient(base_url=settings.panel_url + "/", timeout=10, follow_redirects=False, trust_env=False)
        try:
            # CSRF endpoint is optional in older versions.
            csrf = await self.client.get("csrf-token")
            if csrf.status_code == 200:
                try:
                    data = csrf.json()
                    obj = data.get("obj")
                    self.csrf = data.get("token") or data.get("csrfToken") or (obj if isinstance(obj, str) else obj.get("token") if isinstance(obj, dict) else None)
                except (ValueError, AttributeError, TypeError):
                    self.csrf = None
            headers = {"X-CSRF-Token": self.csrf} if isinstance(self.csrf, str) else {}
            response = await self.client.post("login", data={"username": settings.panel_username, "password": settings.panel_password, "twoFactorCode": settings.panel_totp}, headers=headers)
            result = response.json() if response.status_code == 200 else None
            if not isinstance(result, dict) or result.get("success") is not True:
                self.failed_identity = identity
                self.retry_login_at = time.monotonic() + 300
                raise IntegrationError("面板登录失败，请检查账号、密码、二步验证或面板路径")
            # Some panel releases rotate the CSRF token on successful login.
            fresh = await self.client.get("csrf-token")
            if fresh.status_code == 200:
                try:
                    token_data = fresh.json()
                    obj = token_data.get("obj")
                    self.csrf = token_data.get("token") or token_data.get("csrfToken") or (obj if isinstance(obj, str) else obj.get("token") if isinstance(obj, dict) else None)
                except (ValueError, AttributeError, TypeError):
                    pass
            self.identity = identity
            self.failed_identity = None
        except (httpx.HTTPError, ValueError):
            await self.close()
            raise IntegrationError("面板连接失败，请检查地址、证书和网络") from None
        except IntegrationError:
            await self.close()
            raise

    async def onlines(self, settings: Settings) -> set[str]:
        await self.connect(settings)
        modes = [settings.online_api] if settings.online_api != "auto" else ["clients", "inbounds"]
        paths = [self.selected_path] if self.selected_path else [self.ONLINE_PATHS[m] for m in modes]
        try:
            for path in paths:
                headers = {"X-CSRF-Token": self.csrf} if isinstance(self.csrf, str) else {}
                response = await self.client.post(path, headers=headers)
                if response.status_code in (404, 405):
                    continue
                if response.status_code != 200:
                    raise IntegrationError(f"在线状态查询失败（HTTP {response.status_code}），本轮记为未知")
                data = response.json()
                if not isinstance(data, dict) or data.get("success") is not True or "obj" not in data or (data["obj"] is not None and not isinstance(data["obj"], list)):
                    raise IntegrationError("面板返回格式异常或会话失效，本轮记为未知")
                # Go nil []string serializes as null in some panel releases.
                # This is accepted only in an explicit successful envelope.
                values = data["obj"] or []
                if any(not isinstance(v, str) or len(v) > 256 or not v.strip() for v in values):
                    raise IntegrationError("在线用户列表格式不兼容，本轮记为未知")
                self.selected_path = path
                return set(values)
            raise IntegrationError("未找到兼容的在线查询接口，请检查面板版本与路径")
        except (httpx.HTTPError, ValueError):
            await self.close()
            raise IntegrationError("在线状态查询失败，本轮记为未知") from None
        except IntegrationError:
            await self.close()
            raise


class DeliveryError(Exception):
    def __init__(self, message, retry_after=0, permanent=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


async def send_telegram(settings: Settings, text: str, client=None):
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", settings.telegram_token):
        raise DeliveryError("Bot Token 格式无效", permanent=True)
    if not settings.telegram_chat_id.strip():
        raise DeliveryError("请填写 Chat ID", permanent=True)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
    try:
        response = await client.post(f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage", json={
            "chat_id": settings.telegram_chat_id, "text": text,
            "link_preview_options": {"is_disabled": True},
        })
        try:
            data = response.json()
        except ValueError:
            raise DeliveryError("Telegram 返回格式异常") from None
        if not isinstance(data, dict):
            raise DeliveryError("Telegram 返回格式异常")
        if response.status_code == 429 or data.get("error_code") == 429:
            delay = min(86400, max(1, int(data.get("parameters", {}).get("retry_after", 30))))
            raise DeliveryError("Telegram 发送限流，等待后重试", retry_after=delay)
        if response.status_code != 200 or data.get("ok") is not True:
            # Never echo response descriptions or URLs: both can carry secrets.
            raise DeliveryError(f"Telegram 发送失败（HTTP {response.status_code}）", permanent=400 <= response.status_code < 500)
        return data.get("result", {}).get("message_id")
    except httpx.HTTPError:
        raise DeliveryError("Telegram 网络连接失败，等待重试") from None
    finally:
        if owned:
            await client.aclose()

