from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class DeliveryReceipt:
    platform_message_id: str


Sender = Callable[[dict[str, Any], dict[str, Any]], Awaitable[DeliveryReceipt]]


class TransportRegistry:
    """Runtime-only platform senders; no SDK object is persisted."""

    def __init__(self) -> None:
        self._senders: dict[str, Sender] = {}

    def register(self, platform: str, sender: Sender) -> None:
        key = platform.strip().lower()
        if not key or not callable(sender):
            raise ValueError("platform and callable sender are required")
        self._senders[key] = sender

    def unregister(self, platform: str) -> None:
        self._senders.pop(platform.strip().lower(), None)

    def available(self, platform: str) -> bool:
        return platform.strip().lower() in self._senders

    async def send(
        self, platform: str, destination: dict[str, Any], payload: dict[str, Any]
    ) -> DeliveryReceipt:
        sender = self._senders.get(platform.strip().lower())
        if sender is None:
            raise RuntimeError(f"transport unavailable: {platform}")
        return await sender(destination, payload)


async def telegram_send(bot: Any, destination: dict[str, Any], payload: dict[str, Any]) -> DeliveryReceipt:
    address = destination.get("address") or {}
    chat_id = address.get("chat_id")
    if not isinstance(chat_id, (str, int)) or isinstance(chat_id, bool):
        raise ValueError("verified Telegram destination lacks chat_id")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("notification text is required")
    message = await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        raise RuntimeError("Telegram did not return a message id")
    return DeliveryReceipt(str(message_id))


def telegram_sender(bot: Any) -> Sender:
    async def send(destination: dict[str, Any], payload: dict[str, Any]) -> DeliveryReceipt:
        return await telegram_send(bot, destination, payload)

    return send
