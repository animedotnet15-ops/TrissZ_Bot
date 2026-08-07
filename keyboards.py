from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# --------------------------------------------------------------------- #
#  Button "style" compatibility layer
#  ------------------------------------------------------------------
#  Telegram's Bot API (and therefore both Pyrogram and aiogram, which is
#  what this project actually runs on) has NO concept of a colored/styled
#  inline button — there is no `pyrogram.enums.ButtonStyle` and no
#  equivalent in aiogram either; InlineKeyboardButton only ever accepts
#  text + one action (url/callback_data/etc). That API does not exist,
#  so it is not used here.
#
#  Instead we simulate PRIMARY / SUCCESS / DANGER visually with a
#  consistent emoji prefix, centralized in this one helper so every
#  keyboard in the bot looks consistent.
# --------------------------------------------------------------------- #
STYLE_PRIMARY = "primary"
STYLE_SUCCESS = "success"
STYLE_DANGER = "danger"

_STYLE_PREFIX = {
    STYLE_PRIMARY: "🔵",
    STYLE_SUCCESS: "🟢",
    STYLE_DANGER: "🔴",
}


def styled_button(text: str, *, style: str = STYLE_PRIMARY, callback_data: str | None = None,
                   url: str | None = None) -> InlineKeyboardButton:
    prefix = _STYLE_PREFIX.get(style, "")
    label = f"{prefix} {text}".strip()
    if url:
        return InlineKeyboardButton(text=label, url=url)
    return InlineKeyboardButton(text=label, callback_data=callback_data or "noop")


def sc(text: str) -> str:
    """Small-caps-ish display helper — kept simple/ASCII-safe so it renders
    identically across all clients; callers pass already nicely-cased short
    labels and this just normalizes spacing."""
    return " ".join(text.strip().split())


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


# --------------------------------------------------------------------- #
#  Advanced Admin Panel — /admin entrypoint
# --------------------------------------------------------------------- #
def admin_main_menu(bot_enabled: bool) -> InlineKeyboardMarkup:
    rows = [
        [styled_button("sᴛᴀᴛɪsᴛɪᴄs", style=STYLE_PRIMARY, callback_data="adm:stats"),
         styled_button("ғɪʟᴇ ᴍᴀɴᴀɢᴇʀ", style=STYLE_PRIMARY, callback_data="adm:files:0")],
        [styled_button("ʙᴀᴛᴄʜ", style=STYLE_PRIMARY, callback_data="adm:batch"),
         styled_button("ᴜsᴇʀs", style=STYLE_PRIMARY, callback_data="adm:users")],
        [styled_button("ʙʀᴏᴀᴅᴄᴀsᴛ", style=STYLE_PRIMARY, callback_data="adm:broadcast"),
         styled_button("ғᴏʀᴄᴇ sᴜʙ", style=STYLE_PRIMARY, callback_data="adm:fsub")],
        [styled_button("sʜᴏʀᴛᴇɴᴇʀ", style=STYLE_PRIMARY, callback_data="adm:short"),
         styled_button("ʙᴜᴛᴛᴏɴs", style=STYLE_PRIMARY, callback_data="adm:buttons")],
        [styled_button("ᴡᴇʟᴄᴏᴍᴇ", style=STYLE_PRIMARY, callback_data="settings:open_from_admin"),
         styled_button("ᴀᴅᴍɪɴ ʟᴏɢs", style=STYLE_PRIMARY, callback_data="adm:logs:0")],
        [styled_button("ʙᴀᴄᴋᴜᴘ", style=STYLE_SUCCESS, callback_data="adm:backup"),
         styled_button("ʀᴇsᴛᴏʀᴇ", style=STYLE_DANGER, callback_data="adm:restore_info")],
        [styled_button(
            "ʙᴏᴛ ᴏɴ" if not bot_enabled else "ʙᴏᴛ ᴏɴ ✅",
            style=STYLE_SUCCESS, callback_data="adm:bot_on"
        ), styled_button(
            "ʙᴏᴛ ᴏғғ" if bot_enabled else "ʙᴏᴛ ᴏғғ 🔴",
            style=STYLE_DANGER, callback_data="adm:bot_off"
        )],
        [styled_button("✖️ ᴄʟᴏsᴇ", style=STYLE_DANGER, callback_data="adm:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_home_row(back_cb: str) -> list[InlineKeyboardButton]:
    return [
        styled_button("⬅️ ʙᴀᴄᴋ", style=STYLE_PRIMARY, callback_data=back_cb),
        styled_button("🏠 ʜᴏᴍᴇ", style=STYLE_PRIMARY, callback_data="adm:home"),
    ]


def confirm_row(yes_cb: str, no_cb: str) -> list[InlineKeyboardButton]:
    return [
        styled_button("ᴄᴏɴғɪʀᴍ", style=STYLE_SUCCESS, callback_data=yes_cb),
        styled_button("ᴄᴀɴᴄᴇʟ", style=STYLE_DANGER, callback_data=no_cb),
    ]


def shortener_menu(s: dict) -> InlineKeyboardMarkup:
    rows = [
        [styled_button(f"🌐 ᴅᴏᴍᴀɪɴ: {s['domain']}", style=STYLE_PRIMARY, callback_data="adm:short:domain")],
        [styled_button(
            f"🔑 ᴛᴏᴋᴇɴ: {'sᴇᴛ ✅' if s['api_token'] else 'ɴᴏᴛ sᴇᴛ ❌'}",
            style=STYLE_PRIMARY, callback_data="adm:short:token"
        )],
        [styled_button(f"⏱️ ᴍɪɴ: {s['min_seconds']}s", style=STYLE_PRIMARY, callback_data="adm:short:min"),
         styled_button(f"⏳ ᴍᴀx: {s['max_seconds']}s", style=STYLE_PRIMARY, callback_data="adm:short:max")],
        [styled_button(
            "ᴇɴᴀʙʟᴇᴅ ✅" if s["enabled"] else "ᴇɴᴀʙʟᴇ",
            style=STYLE_SUCCESS, callback_data="adm:short:enable"
        ), styled_button(
            "ᴅɪsᴀʙʟᴇ" if s["enabled"] else "ᴅɪsᴀʙʟᴇᴅ 🔴",
            style=STYLE_DANGER, callback_data="adm:short:disable"
        )],
        [styled_button("🧪 ᴛᴇsᴛ ᴀᴘɪ", style=STYLE_PRIMARY, callback_data="adm:short:test")],
        back_home_row("adm:home"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def users_menu() -> InlineKeyboardMarkup:
    rows = [
        [styled_button("🔎 sᴇᴀʀᴄʜ ᴜsᴇʀ", style=STYLE_PRIMARY, callback_data="adm:users:search")],
        [styled_button("🚫 ʙᴀɴ", style=STYLE_DANGER, callback_data="adm:users:ban"),
         styled_button("✅ ᴜɴʙᴀɴ", style=STYLE_SUCCESS, callback_data="adm:users:unban")],
        back_home_row("adm:home"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fsub_menu(channels: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, ch in enumerate(channels):
        label = ch.get("name") or ch.get("chat") or f"#{i}"
        folder = ch.get("folder", "")
        prefix = "✅" if ch.get("enabled", True) else "⛔"
        text = f"{prefix} {folder + ' / ' if folder else ''}{label}"
        rows.append([
            InlineKeyboardButton(text=text[:60], callback_data=f"adm:fsub:toggle:{i}"),
            InlineKeyboardButton(text="🗑", callback_data=f"adm:fsub:remove:{i}"),
        ])
    rows.append([styled_button("➕ ᴀᴅᴅ ᴄʜᴀɴɴᴇʟ", style=STYLE_SUCCESS, callback_data="adm:fsub:add")])
    rows.append(back_home_row("adm:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def buttons_menu(rows_data: list) -> InlineKeyboardMarkup:
    rows = []
    for b in rows_data:
        state = "✅" if b["enabled"] else "⛔"
        rows.append([
            InlineKeyboardButton(text=f"{state} {b['text'][:40]}", callback_data=f"adm:btn:toggle:{b['id']}"),
            InlineKeyboardButton(text="⬆️", callback_data=f"adm:btn:up:{b['id']}"),
            InlineKeyboardButton(text="⬇️", callback_data=f"adm:btn:down:{b['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"adm:btn:del:{b['id']}"),
        ])
    rows.append([styled_button("➕ ᴀᴅᴅ ʙᴜᴛᴛᴏɴ", style=STYLE_SUCCESS, callback_data="adm:btn:add")])
    rows.append(back_home_row("adm:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def logs_menu(has_more: bool, offset: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:logs:{max(0, offset - 15)}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:logs:{offset + 15}"))
    if nav:
        rows.append(nav)
    rows.append(back_home_row("adm:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def files_menu(files: list, offset: int, total: int, page_size: int = 8) -> InlineKeyboardMarkup:
    rows = []
    for f in files:
        rows.append([InlineKeyboardButton(
            text=f"📄 {f['original_name'][:45]}",
            callback_data=f"adm:file:{f['id']}"
        )])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:files:{max(0, offset - page_size)}"))
    if offset + page_size < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:files:{offset + page_size}"))
    if nav:
        rows.append(nav)
    rows.append(back_home_row("adm:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def file_detail_menu(file_id: int) -> InlineKeyboardMarkup:
    rows = [
        [styled_button("🔗 ɢᴇɴʟɪɴᴋ", style=STYLE_SUCCESS, callback_data=f"adm:genlink:{file_id}")],
        [styled_button("🗑 ᴅᴇʟᴇᴛᴇ", style=STYLE_DANGER, callback_data=f"adm:filedel:{file_id}")],
        back_home_row("adm:files:0"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def batch_menu(count: int) -> InlineKeyboardMarkup:
    rows = [
        [styled_button(f"👁️ ᴘʀᴇᴠɪᴇᴡ ({count})", style=STYLE_PRIMARY, callback_data="adm:batch:preview")],
        [styled_button("🔗 ɢᴇɴᴇʀᴀᴛᴇ", style=STYLE_SUCCESS, callback_data="adm:batch:gen"),
         styled_button("🚫 ᴄᴀɴᴄᴇʟ", style=STYLE_DANGER, callback_data="adm:batch:cancel")],
        back_home_row("adm:home"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_button(text: str, url: str) -> InlineKeyboardMarkup | None:
    """Return a single-button markup, or None if text/url are blank."""
    text, url = text.strip(), url.strip()
    if not text or not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text[:64], url=url)
    ]])