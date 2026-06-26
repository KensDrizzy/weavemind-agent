"""Synchronous client for Tencent's WeChat iLink Bot HTTP protocol."""

from __future__ import annotations

import base64
import logging
import secrets
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin

import httpx

from channels.wechat.models import PollResult, WechatAccount

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "0.1.0"
BOT_AGENT = f"WeaveMindAgent/{CHANNEL_VERSION}"


class ILinkError(RuntimeError):
    pass


class ILinkProtocolError(ILinkError):
    pass


class SessionExpiredError(ILinkError):
    pass


class LoginExpiredError(ILinkError):
    pass


def _client_version(version: str) -> int:
    parts = []
    for value in version.split(".")[:3]:
        try:
            parts.append(int(value))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


class ILinkClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: Optional[str] = None,
        bot_id: Optional[str] = None,
        app_id: str = "bot",
        route_tag: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ):
        self.base_url = self._normalize_base_url(base_url)
        self.token = token
        self.bot_id = bot_id
        self.app_id = app_id
        self.route_tag = route_tag
        self._client = client or httpx.Client(follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ILinkClient":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def request_qr_code(
        self,
        *,
        bot_type: str = "3",
        local_token_list: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        endpoint = f"ilink/bot/get_bot_qrcode?{urlencode({'bot_type': bot_type})}"
        response = self._post_json(
            endpoint,
            {"local_token_list": local_token_list or []},
            include_auth=False,
            timeout=15,
        )
        if not response.get("qrcode") or not response.get("qrcode_img_content"):
            raise ILinkProtocolError("QR response is missing qrcode fields")
        return response

    def poll_qr_status(
        self,
        qrcode: str,
        *,
        verify_code: Optional[str] = None,
        timeout: float = 35,
    ) -> dict[str, Any]:
        query = {"qrcode": qrcode}
        if verify_code:
            query["verify_code"] = verify_code
        endpoint = f"ilink/bot/get_qrcode_status?{urlencode(query)}"
        try:
            return self._get_json(endpoint, timeout=timeout)
        except httpx.TimeoutException:
            return {"status": "wait"}

    def login(
        self,
        *,
        workspace: str,
        on_qr_code: Callable[[str], None],
        verify_code_provider: Optional[Callable[[], str]] = None,
        timeout_seconds: int = 480,
        max_refreshes: int = 3,
    ) -> WechatAccount:
        """Complete QR login and return credentials without persisting them."""
        deadline = time.monotonic() + timeout_seconds
        refreshes = 0
        pending_verify_code: Optional[str] = None

        qr = self.request_qr_code()
        on_qr_code(str(qr["qrcode_img_content"]))
        qrcode = str(qr["qrcode"])

        while time.monotonic() < deadline:
            response = self.poll_qr_status(
                qrcode,
                verify_code=pending_verify_code,
            )
            status = str(response.get("status") or "wait")

            if status == "wait":
                continue
            if status == "scaned":
                pending_verify_code = None
                continue
            if status == "need_verifycode":
                if not verify_code_provider:
                    raise ILinkProtocolError("login requires a verification code")
                pending_verify_code = verify_code_provider().strip()
                continue
            if status == "scaned_but_redirect":
                redirect_host = str(response.get("redirect_host") or "")
                if redirect_host:
                    self.base_url = self._normalize_base_url(
                        redirect_host
                        if redirect_host.startswith(("http://", "https://"))
                        else f"https://{redirect_host}"
                    )
                continue
            if status == "binded_redirect":
                raise ILinkProtocolError(
                    "This WeChat account is already bound; use existing credentials"
                )
            if status in {"verify_code_blocked", "expired"}:
                refreshes += 1
                if refreshes >= max_refreshes:
                    raise LoginExpiredError("QR login expired too many times")
                pending_verify_code = None
                qr = self.request_qr_code()
                on_qr_code(str(qr["qrcode_img_content"]))
                qrcode = str(qr["qrcode"])
                continue
            if status == "confirmed":
                token = str(response.get("bot_token") or "")
                bot_id = str(response.get("ilink_bot_id") or "")
                user_id = str(response.get("ilink_user_id") or "")
                if not token or not bot_id or not user_id:
                    raise ILinkProtocolError(
                        "confirmed login response is missing token, bot ID, or user ID"
                    )
                base_url = str(response.get("baseurl") or self.base_url)
                self.token = token
                self.bot_id = bot_id
                self.base_url = self._normalize_base_url(base_url)
                return WechatAccount(
                    bot_token=token,
                    bot_id=bot_id,
                    bound_user_id=user_id,
                    base_url=self.base_url,
                    workspace=workspace,
                )

            logger.warning("Unknown iLink QR status: %s", status)

        raise LoginExpiredError("QR login timed out")

    def get_updates(
        self,
        get_updates_buf: str = "",
        *,
        timeout_seconds: float = 35,
    ) -> PollResult:
        try:
            response = self._post_json(
                "ilink/bot/getupdates",
                {
                    "get_updates_buf": get_updates_buf or "",
                    "base_info": self._base_info(),
                },
                timeout=max(timeout_seconds + 5, 10),
            )
        except httpx.TimeoutException:
            return PollResult(messages=(), get_updates_buf=get_updates_buf)

        return PollResult(
            messages=tuple(response.get("msgs") or ()),
            get_updates_buf=str(
                response.get("get_updates_buf")
                if response.get("get_updates_buf") is not None
                else get_updates_buf
            ),
            timeout_ms=(
                int(response["longpolling_timeout_ms"])
                if response.get("longpolling_timeout_ms") is not None
                else None
            ),
        )

    def send_message(
        self,
        *,
        to_user_id: str,
        context_token: str,
        text: str,
    ) -> str:
        if not context_token:
            raise ILinkProtocolError("context_token is required for replies")
        client_id = f"weavemind-{uuid.uuid4()}"
        self._post_json(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [
                        {"type": 1, "text_item": {"text": str(text)}}
                    ],
                },
                "base_info": self._base_info(),
            },
            timeout=15,
        )
        return client_id

    def get_typing_ticket(
        self,
        *,
        user_id: str,
        context_token: str,
    ) -> str:
        response = self._post_json(
            "ilink/bot/getconfig",
            {
                "ilink_user_id": user_id,
                "context_token": context_token,
                "base_info": self._base_info(),
            },
            timeout=10,
        )
        return str(response.get("typing_ticket") or "")

    def send_typing(
        self,
        *,
        user_id: str,
        typing_ticket: str,
        typing: bool = True,
    ) -> None:
        if not typing_ticket:
            return
        self._post_json(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": 1 if typing else 2,
                "base_info": self._base_info(),
            },
            timeout=10,
        )

    def notify_start(self) -> None:
        self._post_json(
            "ilink/bot/msg/notifystart",
            {"base_info": self._base_info()},
            timeout=10,
        )

    def notify_stop(self) -> None:
        self._post_json(
            "ilink/bot/msg/notifystop",
            {"base_info": self._base_info()},
            timeout=10,
        )

    def _base_info(self) -> dict[str, str]:
        return {
            "channel_version": CHANNEL_VERSION,
            "bot_agent": BOT_AGENT,
        }

    def _common_headers(self) -> dict[str, str]:
        headers = {
            "iLink-App-Id": self.app_id,
            "iLink-App-ClientVersion": str(_client_version(CHANNEL_VERSION)),
        }
        if self.route_tag:
            headers["SKRouteTag"] = self.route_tag
        return headers

    def _auth_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self._random_wechat_uin(),
            **self._common_headers(),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token.strip()}"
        return headers

    def _post_json(
        self,
        endpoint: str,
        body: dict[str, Any],
        *,
        include_auth: bool = True,
        timeout: float,
    ) -> dict[str, Any]:
        headers = self._auth_headers() if include_auth else {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self._random_wechat_uin(),
            **self._common_headers(),
        }
        response = self._client.post(
            self._url(endpoint),
            headers=headers,
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        data = self._decode_json(response)
        self._check_response(data)
        return data

    def _get_json(self, endpoint: str, *, timeout: float) -> dict[str, Any]:
        response = self._client.get(
            self._url(endpoint),
            headers=self._common_headers(),
            timeout=timeout,
        )
        response.raise_for_status()
        data = self._decode_json(response)
        self._check_response(data)
        return data

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise ILinkProtocolError("iLink returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ILinkProtocolError("iLink returned a non-object JSON response")
        return data

    @staticmethod
    def _check_response(data: dict[str, Any]) -> None:
        ret = data.get("ret")
        if ret is None or int(ret) == 0:
            return
        errcode = int(data.get("errcode") or ret)
        message = str(data.get("errmsg") or f"iLink error {errcode}")
        if errcode == -14:
            raise SessionExpiredError(message)
        raise ILinkProtocolError(message)

    def _url(self, endpoint: str) -> str:
        return urljoin(f"{self.base_url.rstrip('/')}/", endpoint.lstrip("/"))

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        value = (url or DEFAULT_BASE_URL).strip()
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        return value.rstrip("/")

    @staticmethod
    def _random_wechat_uin() -> str:
        decimal = str(secrets.randbits(32)).encode("utf-8")
        return base64.b64encode(decimal).decode("ascii")
