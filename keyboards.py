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

    rows.append([
        InlineKeyboardButton(text="🔒 Force-Sub", callback_data="fsub:panel"),
        InlineKeyboardButton(text="🔘 Buttons", callback_data="btn:panel"),
    ])
    rows.append([
        InlineKeyboardButton(text="🎨 Welcome", callback_data="welcome:panel"),
        InlineKeyboardButton(text="📦 Custom Batches", callback_data="cbatch:panel"),
    ])
    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="settings:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_confirm_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Send Broadcast", callback_data=f"bcast:go:{admin_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"bcast:abort:{admin_id}"),
    ]])


def broadcast_progress_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛑 Stop Broadcast", callback_data=f"bcast:stop:{admin_id}"),
    ]])


def custom_button(text: str, url: str) -> InlineKeyboardMarkup | None:
    """Return a single-button markup, or None if text/url are blank."""
    text, url = text.strip(), url.strip()
    if not text or not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text[:64], url=url)
    ]])


# ══════════════════════════════════════════════════════════════════════
# Advanced Force-Subscribe Manager
# ══════════════════════════════════════════════════════════════════════
def fsub_panel_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, ch in enumerate(channels):
        label = ch.get("name") or ch.get("chat", "channel")
        folder = ch.get("folder", "")
        status = "✅" if ch.get("enabled", True) else "❌"
        prefix = f"[{folder}] " if folder else ""
        rows.append([InlineKeyboardButton(
            text=f"{status} {prefix}{label}"[:64], callback_data=f"fsub:toggle:{i}"
        )])
        controls = []
        if i > 0:
            controls.append(InlineKeyboardButton(text="⬆️", callback_data=f"fsub:up:{i}"))
        if i < len(channels) - 1:
            controls.append(InlineKeyboardButton(text="⬇️", callback_data=f"fsub:down:{i}"))
        controls.append(InlineKeyboardButton(text="🏷 Label", callback_data=f"fsub:edit:{i}"))
        controls.append(InlineKeyboardButton(text="🗑 Remove", callback_data=f"fsub:remove:{i}"))
        rows.append(controls)
    rows.append([InlineKeyboardButton(text="➕ Add Channel/Group", callback_data="fsub:add")])
    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="fsub:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fsub_remove_confirm_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, remove it", callback_data=f"fsub:remove_confirm:{index}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="fsub:panel"),
    ]])


# ══════════════════════════════════════════════════════════════════════
# Button Manager
# ══════════════════════════════════════════════════════════════════════
def button_manager_keyboard(buttons: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, btn in enumerate(buttons):
        status = "✅" if btn.get("enabled", 1) else "❌"
        kind = "🔗" if btn.get("url") else "⚙️"
        rows.append([InlineKeyboardButton(
            text=f"{status} {kind} {btn.get('text', '')}"[:64],
            callback_data=f"btn:toggle:{btn['id']}"
        )])
        controls = []
        if i > 0:
            controls.append(InlineKeyboardButton(text="⬆️", callback_data=f"btn:up:{btn['id']}"))
        if i < len(buttons) - 1:
            controls.append(InlineKeyboardButton(text="⬇️", callback_data=f"btn:down:{btn['id']}"))
        controls.append(InlineKeyboardButton(text="✏️ Edit", callback_data=f"btn:edit:{btn['id']}"))
        controls.append(InlineKeyboardButton(text="🗑 Delete", callback_data=f"btn:delete:{btn['id']}"))
        rows.append(controls)
    rows.append([
        InlineKeyboardButton(text="➕ Add Button", callback_data="btn:add"),
        InlineKeyboardButton(text="👁 Preview", callback_data="btn:preview"),
    ])
    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="btn:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button_delete_confirm_keyboard(btn_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, delete it", callback_data=f"btn:delete_confirm:{btn_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="btn:panel"),
    ]])


def render_configured_buttons(buttons: list[dict]) -> InlineKeyboardMarkup | None:
    """Build the REAL keyboard attached to bot messages from enabled
    button_configs rows. Telegram inline keyboards only support url and
    callback_data buttons — no colors/styles exist in the Bot API, so
    none are simulated here."""
    enabled = [b for b in buttons if b.get("enabled", 1)]
    if not enabled:
        return None
    rows = []
    for b in enabled:
        if b.get("url"):
            rows.append([InlineKeyboardButton(text=b["text"][:64], url=b["url"])])
        elif b.get("callback"):
            rows.append([InlineKeyboardButton(
                text=b["text"][:64], callback_data=f"cfgbtn:{b['id']}"
            )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ══════════════════════════════════════════════════════════════════════
# Welcome Customization
# ══════════════════════════════════════════════════════════════════════
def welcome_panel_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Edit Welcome Text", callback_data="welcome:edit_text")],
        [InlineKeyboardButton(
            text="🖼 Change Photo" if cfg.get("photo_id") else "🖼 Set Photo",
            callback_data="welcome:set_photo"
        )],
        [InlineKeyboardButton(
            text="🎟 Change Sticker" if cfg.get("sticker_id") else "🎟 Set Sticker",
            callback_data="welcome:set_sticker"
        )],
        [InlineKeyboardButton(
            text=f"🎬 Text Animation: {'ON ✅' if cfg.get('anim_enabled') else 'OFF ❌'}",
            callback_data="welcome:toggle_anim"
        )],
        [InlineKeyboardButton(
            text=f"🎞 Sticker Animation: {'ON ✅' if cfg.get('sticker_anim_enabled') else 'OFF ❌'}",
            callback_data="welcome:toggle_sticker_anim"
        )],
        [InlineKeyboardButton(
            text=f"⏱ Animation Speed: {cfg.get('anim_speed', 'normal').title()}",
            callback_data="welcome:cycle_speed"
        )],
        [InlineKeyboardButton(
            text=f"🙈 Photo Spoiler: {'ON ✅' if cfg.get('spoiler') else 'OFF ❌'}",
            callback_data="welcome:toggle_spoiler"
        )],
        [InlineKeyboardButton(
            text=f"🔌 Welcome Message: {'ENABLED ✅' if cfg.get('enabled') else 'DISABLED ❌'}",
            callback_data="welcome:toggle_enabled"
        )],
    ]
    if cfg.get("photo_id"):
        rows.append([InlineKeyboardButton(text="🗑 Remove Photo", callback_data="welcome:remove_photo")])
    if cfg.get("sticker_id"):
        rows.append([InlineKeyboardButton(text="🗑 Remove Sticker", callback_data="welcome:remove_sticker")])
    rows.append([
        InlineKeyboardButton(text="👁 Preview", callback_data="welcome:preview"),
        InlineKeyboardButton(text="♻️ Reset", callback_data="welcome:reset"),
    ])
    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="welcome:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def welcome_reset_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, reset everything", callback_data="welcome:reset_confirm"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="welcome:panel"),
    ]])


# ══════════════════════════════════════════════════════════════════════
# Custom Batch (First Message -> Last Message)
# ══════════════════════════════════════════════════════════════════════
def custom_batch_list_keyboard(batches: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for b in batches:
        status_icon = {"collecting": "🟡", "completed": "🟢", "cancelled": "⚪️"}.get(b.get("status"), "❔")
        file_count = len(b.get("file_ids", [])) if "file_ids" in b else b.get("file_count", 0)
        rows.append([InlineKeyboardButton(
            text=f"{status_icon} Batch #{b['id']} — {file_count} file(s)",
            callback_data=f"cbatch:view:{b['id']}"
        )])
    if not rows:
        rows.append([InlineKeyboardButton(text="No batches yet", callback_data="cbatch:panel")])
    rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="cbatch:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_batch_detail_keyboard(batch: dict) -> InlineKeyboardMarkup:
    rows = []
    if batch.get("status") == "completed":
        rows.append([
            InlineKeyboardButton(text="🔗 Generate/Regenerate Genlink", callback_data=f"cbatch:regenerate:{batch['id']}"),
        ])
        if batch.get("post_code"):
            rows.append([InlineKeyboardButton(text="🚫 Revoke Genlink", callback_data=f"cbatch:revoke:{batch['id']}")])
    rows.append([InlineKeyboardButton(text="🗑 Delete Batch", callback_data=f"cbatch:delete:{batch['id']}")])
    rows.append([
        InlineKeyboardButton(text="⬅️ Back", callback_data="cbatch:panel"),
        InlineKeyboardButton(text="✖️ Close", callback_data="cbatch:close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_batch_delete_confirm_keyboard(batch_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, delete it", callback_data=f"cbatch:delete_confirm:{batch_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"cbatch:view:{batch_id}"),
    ]])