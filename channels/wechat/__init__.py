"""WeChat iLink channel."""

from channels.wechat.engine import WechatMessageEngine
from channels.wechat.ilink_client import ILinkClient

__all__ = ["ILinkClient", "WechatMessageEngine"]
