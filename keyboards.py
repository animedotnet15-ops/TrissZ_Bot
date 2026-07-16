from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
        # Custom button row
        [InlineKeyboardButton(
            text="✏️ Edit Custom Button" if has_button else "➕ Set Custom Button",
            callback_data="settings:button"
        )],
        # Start photo row
        [InlineKeyboardButton(
            text="🖼 Change Start Photo" if has_start_photo else "🖼 Set Start Photo",
            callback_data="settings:start_photo"          # ← was missing handler in original
        )],
        # Spoiler toggle — only relevant when a photo exists
        [InlineKeyboardButton(
            text=f"🙈 Start Photo Spoiler: {'ON ✅' if spoiler else 'OFF ❌'}",
            callback_data="settings:toggle_spoiler"
        )],
        # Delivery sticker row
        [InlineKeyboardButton(
            text="🎟 Change Delivery Sticker" if has_delivery_sticker else "🎟 Set Delivery Sticker",
            callback_data="settings:delivery_sticker"     # ← was missing handler in original
        )],
        # Protection mode toggle
        [InlineKeyboardButton(
            text=f"🛡️ Shortener Protection: {'ON ✅' if protected else 'OFF ❌'}",
            callback_data="settings:toggle_protection"
        )],
    ]

    # Conditional remove buttons
    if has_button:
        rows.append([InlineKeyboardButton(
            text="🗑 Remove Custom Button",
            callback_data="settings:remove_button"        # ← was missing handler in original
        )])
    if has_start_photo:
        rows.append([InlineKeyboardButton(
            text="🗑 Remove Start Photo",
            callback_data="settings:remove_start_photo"
        )])
    if has_delivery_sticker:
        rows.append([InlineKeyboardButton(
            text="🗑 Remove Delivery Sticker",
            callback_data="settings:remove_delivery_sticker"
        )])

    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="settings:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_button(text: str, url: str) -> InlineKeyboardMarkup | None:
    """Return a single-button markup, or None if text/url are blank."""
    text, url = text.strip(), url.strip()
    if not text or not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text[:64], url=url)
    ]])