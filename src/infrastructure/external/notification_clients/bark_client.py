"""
Bark 通知客户端
"""
import asyncio
import requests
from typing import Dict
from .base import NotificationClient


class BarkClient(NotificationClient):
    """Bark 通知客户端"""

    channel_key = "bark"
    display_name = "Bark"

    # 闲鱼 App 官方图标（App Store CDN，全球可达、高清），用作默认推送图标
    GOOFISH_ICON_URL = "https://is1-ssl.mzstatic.com/image/thumb/Purple211/v4/b9/cd/a7/b9cda78b-0c0c-6f84-0494-4735d05682a4/AppIcon-0-0-1x_U007epad-0-1-0-sRGB-85-220.png/512x512bb.jpg"

    def __init__(self, bark_url: str = None, pcurl_to_mobile: bool = True):
        super().__init__(enabled=bool(bark_url), pcurl_to_mobile=pcurl_to_mobile)
        self.bark_url = bark_url

    @staticmethod
    def _map_group(reason: str) -> str:
        """根据通知类型映射 Bark 分组"""
        r = reason or ""
        if any(k in r for k in ("关键词", "命中")):
            return "🎯 关键词推荐"
        if any(k in r for k in ("降价", "低价", "跌")):
            return "💰 降价通知"
        if any(k in r for k in ("咨询", "私聊", "聊一聊")):
            return "💬 咨询通知"
        if any(k in r for k in ("账号", "登录", "失效", "掉线", "验证")):
            return "⚠️ 账号通知"
        if any(k in r for k in ("下架", "重上架", "重新上架")):
            return "📦 上下架通知"
        if any(k in r for k in ("日报", "行情")):
            return "📊 行情日报"
        return "闲鱼监控"

    async def send(self, product_data: Dict, reason: str) -> None:
        """发送 Bark 通知"""
        if not self.is_enabled():
            raise RuntimeError("Bark 未启用")

        message = self._build_message(product_data, reason)
        bark_payload = {
            "title": message.notification_title,
            "body": message.content,
            "url": message.mobile_link or message.desktop_link,
            "level": "timeSensitive",
            "group": self._map_group(message.reason),
        }

        # 通知图标固定为闲鱼 Logo；商品图作为通知配图（大图）
        bark_payload["icon"] = self.GOOFISH_ICON_URL
        if message.image_url:
            bark_payload["image"] = message.image_url

        headers = {"Content-Type": "application/json; charset=utf-8"}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                self.bark_url,
                json=bark_payload,
                headers=headers,
                timeout=10
            )
        )
        response.raise_for_status()