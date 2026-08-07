from __future__ import annotations

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_STYLE_MAP = {
    "primary": ButtonStyle.PRIMARY,
    "success": ButtonStyle.SUCCESS,
    "danger": ButtonStyle.DANGER,
}


def create_button(
    text: str,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    """Centralized button constructor — use this EVERYWHERE instead of
    calling InlineKeyboardButton directly, so every button in the bot
    gets real Telegram button colors consistently.

    style: "primary" (blue) | "success" (green) | "danger" (red) | None
    (Telegram's default gray, for buttons that shouldn't be colored -
    e.g. plain informational rows.)

    Button text must NOT contain color emojis (🔴🟢🔵) - the color comes
    from the real `style` field on the outgoing Bot API payload.
    """
    kwargs: dict = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style is not None:
        mapped = _STYLE_MAP.get(style.lower())
        if mapped is not None:
            kwargs["style"] = mapped
    return InlineKeyboardButton(**kwargs)


def settings_keyboard(
    *,
    has_button: bool,
    has_start_photo: bool,
    has_delivery_sticker: bool,
    protected: bool,
    spoiler: bool,
) -> InlineKeyboardMarkup:
    """Build the admin settings dashboard keyboard.

    Every callback_data value here must have a matching branch in
    settings_callback_handler inside bot.py.
    """
    rows = [
        [create_button(
            "Edit Custom Button" if has_button else "Set Custom Button",
            callback_data="settings:button", style="primary",
        )],
        [create_button(
            "Change Start Photo" if has_start_photo else "Set Start Photo",
            callback_data="settings:start_photo", style="primary",
        )],
        [create_button(
            f"Start Photo Spoiler: {'ON' if spoiler else 'OFF'}",
            callback_data="settings:toggle_spoiler",
            style="success" if spoiler else "primary",
        )],
        [create_button(
            "Change Delivery Sticker" if has_delivery_sticker else "Set Delivery Sticker",
            callback_data="settings:delivery_sticker", style="primary",
        )],
        [create_button(
            f"Shortener Protection: {'ON' if protected else 'OFF'}",
            callback_data="settings:toggle_protection",
            style="success" if protected else "primary",
        )],
    ]

    if has_button:
        rows.append([create_button("Remove Custom Button", callback_data="settings:remove_button", style="danger")])
    if has_start_photo:
        rows.append([create_button("Remove Start Photo", callback_data="settings:remove_start_photo", style="danger")])
    if has_delivery_sticker:
        rows.append([create_button("Remove Delivery Sticker", callback_data="settings:remove_delivery_sticker", style="danger")])

    rows.append([create_button("Close", callback_data="settings:close", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_button(text: str, url: str) -> InlineKeyboardMarkup | None:
    """Return a single-button markup, or None if text/url are blank."""
    text, url = text.strip(), url.strip()
    if not text or not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text[:64], url=url)
    ]])
