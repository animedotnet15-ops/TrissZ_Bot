from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command, CommandObject
from html import escape as html_escape
import logging
import asyncio
import urllib.parse
import aiohttp

from config import config
from database import database
from keyboards import settings_keyboard, custom_button

LOG = logging.getLogger("bot_handlers")
bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


# --- Helper: Force-Subscribe membership check (supports multiple channels) ---
async def get_unjoined_fsub_channels(user_id: int) -> list[dict]:
    channels = await database.get_fsub_channels()
    if not channels:
        return []
    unjoined = []
    for ch in channels:
        chat_ref_raw = ch.get("chat", "")
        if not chat_ref_raw:
            continue
        chat_ref = int(chat_ref_raw) if chat_ref_raw.lstrip("-").isdigit() else chat_ref_raw
        try:
            member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
            if member.status in ("left", "kicked"):
                unjoined.append(ch)
        except Exception as e:
            LOG.warning(f"FSUB membership check failed for {user_id} on {chat_ref_raw}: {e}")
            # fail-open per channel so one misconfigured channel doesn't lock everyone out
            continue
    return unjoined


async def check_fsub_membership(user_id: int) -> bool:
    return len(await get_unjoined_fsub_channels(user_id)) == 0


def fsub_join_link(ch: dict) -> str:
    if ch.get("link"):
        return ch["link"]
    chat = ch.get("chat", "")
    if chat.startswith("@"):
        return f"https://t.me/{chat.lstrip('@')}"
    return ""


# --- Helper: shorten URL via Arolinks API ---
async def shorten_with_arolinks(destination_url: str) -> str:
    api_token = await database.get_setting("arolinks_api_token", "")
    if not api_token:
        return destination_url
    try:
        api_url = f"https://arolinks.com/api?api={api_token}&url={urllib.parse.quote(destination_url)}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    shortened = data.get("shortenedUrl")
                    if shortened:
                        return shortened
    except Exception as e:
        LOG.error(f"Arolinks API error: {e}")
    return destination_url


# --- Core Dynamic Welcome Engine ---
async def execution_welcome(message: Message, first_name: str):
    first_name = html_escape(first_name)
    try:
        sticker_id = await database.delivery_sticker()
        photo_id, spoiler_enabled = await database.start_photo()

        if sticker_id:
            try:
                status_msg = await message.answer_sticker(sticker=sticker_id)
                await asyncio.sleep(1.2)
                await status_msg.delete()
            except Exception as e:
                LOG.warning(f"Sticker send failed (skipping): {e}")

        try:
            status_msg = await message.answer(f"⏳ <i>Hello {first_name}, verifying access parameters...</i>")
            await asyncio.sleep(1.0)
            await status_msg.delete()
        except Exception as e:
            LOG.warning(f"Status message failed (skipping): {e}")

        welcome_text = (
            f"Hey {first_name} 👋\n"
            f"<i>I am your secure file-sharing assistant</i> ✨\n\n"
            f"🪽 <b>Ready To Feel The Power?</b>\n\n"
            f"<blockquote>🚀 I share requested files through secure access links.\n"
            f"🛡️ Access is verified before a file is delivered.</blockquote>\n\n"
            f"🔗 <i>Open a valid file link to continue.</i>\n"
            f"📥 <i>Select a quality after verification.</i>"
        )

        if photo_id:
            await message.answer_photo(
                photo=photo_id,
                caption=welcome_text,
                has_spoiler=spoiler_enabled
            )
        else:
            await message.answer(welcome_text)

    except Exception as e:
        LOG.error(f"execution_welcome critical failure: {e}")
        try:
            await message.answer(f"Hey {first_name} 👋 Welcome! Send a valid file link to get started.")
        except Exception:
            pass


# --- Helper: deliver files to a user ---
async def deliver_files(message: Message, post_row) -> None:
    files = await database.get_post_files(int(post_row["id"]))
    if not files:
        await message.answer("⚠️ <i>This link appears to be empty or missing active storage tracks.</i>")
        return

    btn_text, btn_url = await database.get_custom_button()
    markup = custom_button(btn_text, btn_url) if (btn_text and btn_url) else None

    for file in files:
        try:
            await bot.copy_message(
                chat_id=message.chat.id,
                from_chat_id=int(config.storage_channel_id),
                message_id=int(file["storage_message_id"]),
                reply_markup=markup
            )
        except Exception as e:
            LOG.warning(f"Copy with markup failed, trying without: {e}")
            try:
                await bot.copy_message(
                    chat_id=message.chat.id,
                    from_chat_id=int(config.storage_channel_id),
                    message_id=int(file["storage_message_id"])
                )
            except Exception as inner_err:
                LOG.error(f"Critical file dispatch failure: {inner_err}")
                await message.answer(f"❌ Failed to extract: <code>{html_escape(file['original_name'])}</code>")


# --- Command Handler: /start ---
@router.message(Command("start"))
async def start_handler(message: Message, command: CommandObject):
    if not message.from_user:
        return
    user = message.from_user

    await database.touch_user(user.id, user.first_name or "", user.username or "")

    if await database.is_banned(user.id):
        await message.answer("⛔ <b>Access Denied.</b>\n<i>You are permanently banned from using this platform.</i>")
        return

    payload = (command.args or "").strip()

    unjoined = await get_unjoined_fsub_channels(user.id)
    if unjoined:
        await send_fsub_prompt(message, payload, unjoined)
        return

    await process_start_payload(message, user, payload)


# --- Helper: send the "join our channel(s)" prompt ---
async def send_fsub_prompt(message: Message, payload: str, unjoined: list[dict]):
    buttons = []
    for ch in unjoined:
        link = fsub_join_link(ch)
        label = ch.get("name") or "📢 Join Channel"
        if link:
            buttons.append([InlineKeyboardButton(text=label, url=link)])
    buttons.append([InlineKeyboardButton(text="🔄 I've Joined", callback_data=f"fs:{payload}")])

    plural = "s" if len(unjoined) > 1 else ""
    await message.answer(
        "🔒 <b>One Quick Step!</b>\n\n"
        f"<blockquote>You need to join our channel{plural} before using this bot.</blockquote>\n\n"
        "👇 <i>Tap join, then tap the button below to continue.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# --- Callback Handler: FSUB recheck ---
@router.callback_query(F.data.startswith("fs:"))
async def fsub_recheck_handler(cb: CallbackQuery):
    if not cb.from_user:
        return
    user = cb.from_user
    payload = cb.data[3:]

    unjoined = await get_unjoined_fsub_channels(user.id)
    if unjoined:
        await cb.answer("❌ You haven't joined all the required channels yet!", show_alert=True)
        return

    await cb.answer("✅ Verified!")
    try:
        await cb.message.delete()
    except Exception:
        pass

    await process_start_payload(cb.message, user, payload)


# --- Core /start payload dispatcher (shared by start_handler and fsub recheck) ---
async def process_start_payload(message: Message, user, payload: str):
    if not payload:
        await execution_welcome(message, user.first_name or "User")
        return

    # --- Bypass warning handler ---
    if payload.startswith("warn_"):
        code = payload.replace("warn_", "")
        event_key = f"bypass:{code}"
        count, banned, was_new = await database.record_bypass(
            user.id, event_key, f"Direct shortener bypass attempt on {code}"
        )
        if banned:
            await message.answer("⛔ <b>Banned!</b>\n<i>You have been blacklisted for attempting to bypass our shortener protections.</i>")
        else:
            await message.answer(
                f"⚠️ <b>Bypass Detected!</b>\n\n"
                f"<blockquote>Do not use scraper tools or direct links. Please go through the shortener page verification.</blockquote>\n\n"
                f"🚨 <b>Strikes:</b> <code>{count}/{config.strike_limit}</code> — <i>reaching the limit results in a permanent ban.</i>"
            )
        return

    # --- One-time token handler (post-shortener delivery) ---
    if payload.startswith("tok_"):
        token = payload[4:]
        status, post = await database.claim_token(token, user.id)

        if status == "missing":
            await message.answer(
                "❌ <b>Invalid Token!</b>\n\n"
                "<i>This verification link is invalid. Please request the file link again.</i>"
            )
            return
        elif status == "expired":
            await message.answer(
                "⏰ <b>Token Expired!</b>\n\n"
                "<i>The verification link expired (5 minute limit).</i>\n"
                "🔗 <i>Please open the original file link again to get a fresh one.</i>"
            )
            return
        elif status == "too_fast":
            count, banned, _ = await database.record_bypass(
                user.id,
                f"too_fast:{token}",
                "Claimed verification token suspiciously fast (shortener bypass)"
            )
            if banned:
                await message.answer(
                    "⛔ <b>Banned!</b>\n\n"
                    "<i>You have been permanently banned for repeatedly bypassing our shortener verification.</i>"
                )
            else:
                await message.answer(
                    "😏 <b>Nice try, smartass.</b>\n\n"
                    "<blockquote>You grabbed this link way too fast to have actually gone through the "
                    "shortener verification — looks like you used a bypass tool instead of "
                    "doing it the honest way.</blockquote>\n\n"
                    f"🚨 <b>Strikes:</b> <code>{count}/{config.strike_limit}</code> — <i>reaching the "
                    "limit results in a permanent ban.</i>\n\n"
                    "🔗 <i>Please open the original file link again and complete the verification properly.</i>"
                )
            return
        elif status == "user_mismatch":
            count, banned, _ = await database.record_bypass(
                user.id,
                f"token_hijack:{token}",
                "Attempted to use another user's verification token"
            )
            if banned:
                await message.answer(
                    "⛔ <b>Banned!</b>\n"
                    "<i>You have been permanently banned for attempting to steal another user's verification token.</i>"
                )
            else:
                await message.answer(
                    f"🚫 <b>Access Denied!</b>\n\n"
                    f"<blockquote>This verification link was generated for a different account and cannot be reused.</blockquote>\n"
                    f"🚨 <b>Strikes:</b> <code>{count}/{config.strike_limit}</code>"
                )
            return
        elif status == "used":
            await message.answer(
                "⚠️ <b>Already Used!</b>\n\n"
                "<i>This verification link has already been claimed. Each link is single-use only.</i>\n"
                "🔗 <i>Please open the original file link again to generate a new one.</i>"
            )
            return

        # Token is valid — deliver files
        await deliver_files(message, post)
        return

    # --- Direct file link handler ---
    code = payload.replace("file_", "").replace("get_", "")

    post = await database.get_post(code)
    if not post:
        await message.answer("❌ <b>Link Invalid</b>\n<i>This file record could not be found or has expired.</i>")
        return

    if int(post["protected"]):
        # Generate a fresh one-time token tied to this specific user
        token = await database.create_pending_token(
            post_id=int(post["id"]),
            user_id=user.id
        )

        # Wrap the bot callback URL in the shortener
        bot_callback = f"https://t.me/{config.bot_username}?start=tok_{token}"
        verification_link = await shorten_with_arolinks(bot_callback)

        btn = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔐 Complete Verification", url=verification_link)
        ]])
        await message.answer(
            "🛡️ <b>Link Protected!</b>\n\n"
            "<blockquote>You must complete human verification before this file can be delivered.</blockquote>\n\n"
            "⏳ <i>This verification link is valid for 5 minutes and is tied to your account only.</i>",
            reply_markup=btn
        )
        return

    # Unprotected — deliver directly
    await deliver_files(message, post)


# --- Admin: /setstartphoto ---
@router.message(Command("setstartphoto"))
async def set_start_photo_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.photo:
        await message.answer("⚠️ <b>Usage:</b> Reply to an image with <code>/setstartphoto</code>")
        return
    file_id = message.reply_to_message.photo[-1].file_id
    await database.set_start_photo(file_id)
    await message.answer("✅ <b>Start photo updated successfully!</b>")


# --- Admin: /setsticker ---
@router.message(Command("setsticker"))
async def set_sticker_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.answer("⚠️ <b>Usage:</b> Reply to a sticker with <code>/setsticker</code>")
        return
    file_id = message.reply_to_message.sticker.file_id
    await database.set_delivery_sticker(file_id)
    await message.answer("✅ <b>Welcome sticker saved!</b>")


# --- Admin: /genlink ---
@router.message(Command("genlink"))
async def genlink_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return

    msg_id = None
    file_name = "Document Entry"

    args = (command.args or "").strip()
    if args.isdigit():
        msg_id = int(args)
    elif message.reply_to_message:
        msg_id = message.reply_to_message.message_id
        if message.reply_to_message.document:
            file_name = message.reply_to_message.document.file_name or file_name

    if not msg_id:
        await message.answer(
            "⚠️ <b>Usage:</b> Reply to a file with <code>/genlink</code> "
            "or <code>/genlink [msg_id]</code>"
        )
        return

    try:
        file_row = await database.add_stored_file(
            storage_message_id=msg_id,
            original_name=file_name,
            tag="single"
        )
    except Exception as e:
        LOG.error(f"add_stored_file failed: {e}")
        await message.answer(
            f"❌ Could not register file. It may already be registered.\n"
            f"Try <code>/batch {msg_id} {msg_id}</code> instead."
        )
        return

    is_protected = (await database.get_setting("link_mode", "direct") == "shortener")
    post_row = await database.create_post(
        kind="single",
        file_ids=[int(file_row["id"])],
        protected=is_protected
    )
    share_url = f"https://t.me/{config.bot_username}?start=file_{post_row['code']}"

    await message.answer(
        f"🔗 <b>Link Generated!</b>\n\n"
        f"📦 <b>File ID:</b> <code>{file_row['id']}</code>\n"
        f"📥 <b>Link:</b> <code>{share_url}</code>"
    )


# --- Admin: /batch ---
@router.message(Command("batch"))
async def batch_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").strip().split()
    if len(args) < 2 or not (args[0].isdigit() and args[1].isdigit()):
        await message.answer("⚠️ <b>Usage:</b> <code>/batch [start_msg_id] [end_msg_id]</code>")
        return

    start_id, end_id = sorted((int(args[0]), int(args[1])))

    existing = await database.get_files_by_storage_range(start_id, end_id)
    existing_ids = {int(f["storage_message_id"]) for f in existing}

    registered = 0
    for msg_id in range(start_id, end_id + 1):
        if msg_id not in existing_ids:
            try:
                await database.add_stored_file(
                    storage_message_id=msg_id,
                    original_name=f"File_{msg_id}",
                    tag="batch"
                )
                registered += 1
            except Exception:
                pass

    files, resolution_kind = await database.resolve_batch_range(start_id, end_id)
    if not files:
        await message.answer("❌ No files found in that message range.")
        return

    file_ids = [int(f["id"]) for f in files]
    is_protected = (await database.get_setting("link_mode", "direct") == "shortener")
    post_row = await database.create_post(kind="batch", file_ids=file_ids, protected=is_protected)

    share_url = f"https://t.me/{config.bot_username}?start=file_{post_row['code']}"
    await message.answer(
        f"📦 <b>Batch Link Created!</b>\n\n"
        f"📊 <b>Files:</b> <code>{len(files)}</code> ({resolution_kind} mapping)"
        + (f"\n🆕 <b>Auto-registered:</b> <code>{registered}</code> new entries" if registered else "")
        + f"\n📥 <b>Link:</b> <code>{share_url}</code>"
    )


# --- Admin: /setshortner ---
@router.message(Command("setshortner"))
async def set_shortener_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    url = (command.args or "").strip()
    if not url:
        await message.answer("⚠️ <b>Usage:</b> <code>/setshortner arolinks.com</code>")
        return
    await database.set_setting("shortener_url", url)
    await message.answer(f"✅ <b>Shortener domain set to:</b> <code>{url}</code>")


# --- Admin: /setwaittime ---
@router.message(Command("setwaittime"))
async def set_wait_time_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").strip().split()
    if not args or not args[0].isdigit():
        current_min = await database.get_setting("min_verify_seconds", "120")
        current_max = await database.get_setting("token_validity_seconds", "300")
        await message.answer(
            "⚠️ <b>Usage:</b> <code>/setwaittime [min_seconds] [max_seconds]</code>\n\n"
            f"Current minimum (anti-bypass floor): <code>{current_min}s</code>\n"
            f"Current link validity (max): <code>{current_max}s</code>"
        )
        return

    min_seconds = int(args[0])
    await database.set_setting("min_verify_seconds", str(min_seconds))

    reply = f"✅ Minimum verification time set to <code>{min_seconds}s</code>."
    if len(args) > 1 and args[1].isdigit():
        max_seconds = int(args[1])
        await database.set_setting("token_validity_seconds", str(max_seconds))
        reply += f"\n✅ Link validity window set to <code>{max_seconds}s</code>."

    await message.answer(reply)


# --- Admin: /addfsub ---
@router.message(Command("addfsub"))
async def add_fsub_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").strip().split(maxsplit=2)
    if not args:
        await message.answer(
            "⚠️ <b>Usage:</b> <code>/addfsub [@channel_or_chat_id] [invite_link] [display_name]</code>\n\n"
            "<i>invite_link is required for private channels (numeric chat IDs).</i>\n"
            "<i>display_name is optional — shown on the join button (defaults to \"📢 Join Channel\").</i>\n\n"
            "Use <code>/listfsub</code> to see current channels."
        )
        return

    chat_ref = args[0]
    link = args[1] if len(args) > 1 and args[1].lower() != "-" else ""
    name = args[2] if len(args) > 2 else ""

    if not chat_ref.startswith("@") and not link:
        await message.answer(
            "⚠️ <i>Private chat IDs need an invite link:</i>\n"
            "<code>/addfsub -100xxxxxxxxxx https://t.me/+invitehash</code>"
        )
        return

    channels = await database.get_fsub_channels()
    channels = [c for c in channels if c.get("chat") != chat_ref]  # replace if already present
    channels.append({"chat": chat_ref, "link": link, "name": name})
    await database.set_fsub_channels(channels)

    await message.answer(
        f"✅ <b>Added Force-Subscribe channel:</b> <code>{chat_ref}</code>\n"
        f"📊 <b>Total channels:</b> <code>{len(channels)}</code>"
    )


# --- Admin: /removefsub ---
@router.message(Command("removefsub"))
async def remove_fsub_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    chat_ref = (command.args or "").strip()
    if not chat_ref:
        await message.answer("⚠️ <b>Usage:</b> <code>/removefsub [@channel_or_chat_id]</code>")
        return

    channels = await database.get_fsub_channels()
    remaining = [c for c in channels if c.get("chat") != chat_ref]
    if len(remaining) == len(channels):
        await message.answer(f"⚠️ <code>{chat_ref}</code> was not found in the Force-Subscribe list.")
        return
    await database.set_fsub_channels(remaining)
    await message.answer(
        f"✅ <b>Removed:</b> <code>{chat_ref}</code>\n"
        f"📊 <b>Remaining channels:</b> <code>{len(remaining)}</code>"
    )


# --- Admin: /listfsub ---
@router.message(Command("listfsub"))
async def list_fsub_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    channels = await database.get_fsub_channels()
    if not channels:
        await message.answer("📋 <i>No Force-Subscribe channels configured.</i>")
        return

    lines = [f"📋 <b>Force-Subscribe Channels ({len(channels)}):</b>\n"]
    for i, ch in enumerate(channels, start=1):
        link = fsub_join_link(ch)
        lines.append(
            f"{i}. <code>{ch.get('chat')}</code>"
            + (f" — {ch.get('name')}" if ch.get("name") else "")
            + (f"\n   🔗 {link}" if link else "")
        )
    await message.answer("\n".join(lines))


# --- Admin: /clearfsub ---
@router.message(Command("clearfsub"))
async def clear_fsub_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    await database.set_fsub_channels([])
    await message.answer("✅ <b>All Force-Subscribe channels cleared.</b>")


# --- Admin: /setapitoken ---
@router.message(Command("setapitoken"))
async def set_api_token_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    token = (command.args or "").strip()
    if not token:
        await message.answer("⚠️ <b>Usage:</b> <code>/setapitoken [your_arolinks_api_token]</code>")
        return
    await database.set_setting("arolinks_api_token", token)
    await message.answer("✅ <b>Arolinks API token saved!</b>")


# --- Admin: /settings ---
@router.message(Command("settings"))
async def settings_menu_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    photo_id, photo_spoiler = await database.start_photo()
    sticker_id = await database.delivery_sticker()
    btn_text, btn_url = await database.get_custom_button()
    link_mode = await database.get_setting("link_mode", "direct")
    short_domain = await database.get_setting("shortener_url", "arolinks.com")
    has_api_token = bool(await database.get_setting("arolinks_api_token", ""))

    kb = settings_keyboard(
        has_button=bool(btn_text),
        has_start_photo=bool(photo_id),
        has_delivery_sticker=bool(sticker_id),
        protected=(link_mode == "shortener"),
        spoiler=photo_spoiler
    )

    await message.answer(
        f"⚙️ <b>@{config.bot_username} Control Dashboard</b>\n\n"
        f"🌐 <b>Shortener Domain:</b> <code>{short_domain}</code>\n"
        f"🔑 <b>API Token:</b> <code>{'YES ✅' if has_api_token else 'NO ❌'}</code>\n"
        f"🔗 <b>Protection Mode:</b> <code>{link_mode.upper()}</code>",
        reply_markup=kb
    )


# --- Admin: /users ---
@router.message(Command("users"))
async def users_count_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    async with database.connection() as db:
        rows = await (await db.execute(
            "SELECT user_id, first_name, username FROM users"
        )).fetchall()

    if not rows:
        await message.answer("👥 No registered users found.")
        return

    text = f"👥 <b>Registered Users ({len(rows)}):</b>\n\n"
    for r in rows:
        uname = f"@{html_escape(r['username'])}" if r['username'] else "No Username"
        text += f"• <code>{r['user_id']}</code> — {html_escape(r['first_name'] or '')} ({uname})\n"

    if len(text) > 4096:
        for x in range(0, len(text), 4000):
            await message.answer(text[x:x + 4000])
    else:
        await message.answer(text)


# --- Helper: resolve a /ban or /unban argument (user_id or @username) ---
async def _resolve_target_user_id(arg: str) -> int | None:
    arg = arg.strip()
    if not arg:
        return None
    if arg.lstrip("-").isdigit():
        return int(arg)
    if arg.startswith("@") or not arg.isdigit():
        row = await database.get_user_by_username(arg)
        if row:
            return int(row["user_id"])
    return None


# --- Admin: /ban ---
@router.message(Command("ban"))
async def ban_user_command(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("⚠️ <b>Usage:</b> <code>/ban [user_id or @username]</code>")
        return
    target_id = await _resolve_target_user_id(arg)
    if target_id is None:
        await message.answer(
            f"❌ Could not resolve <code>{arg}</code> to a known user. "
            "The user must have started the bot at least once for username lookup to work — "
            "try their numeric user ID instead."
        )
        return
    await database.ban_user(target_id, "Admin manual ban.")
    await message.answer(f"✅ User <code>{target_id}</code> has been banned.")


# --- Admin: /unban ---
@router.message(Command("unban"))
async def unban_user_command(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("⚠️ <b>Usage:</b> <code>/unban [user_id or @username]</code>")
        return
    target_id = await _resolve_target_user_id(arg)
    if target_id is None:
        await message.answer(
            f"❌ Could not resolve <code>{arg}</code> to a known user. "
            "The user must have started the bot at least once for username lookup to work — "
            "try their numeric user ID instead."
        )
        return
    unbanned = await database.unban_user(target_id)
    if unbanned:
        await database.reset_strikes(target_id)
        await message.answer(f"✅ User <code>{target_id}</code> has been unbanned and their strikes reset.")
    else:
        await message.answer(f"⚠️ User <code>{target_id}</code> was not found in the ban list.")


# --- Admin: /setbutton ---
@router.message(Command("setbutton"))
async def set_button_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").strip()
    if "|" not in args:
        await message.answer("⚠️ <b>Usage:</b> <code>/setbutton Button Text | https://link.com</code>")
        return
    text, url = map(str.strip, args.split("|", 1))
    if not text or not url:
        await message.answer("⚠️ Both button text and URL are required.")
        return
    await database.set_custom_button(text, url)
    await message.answer(
        f"✅ <b>Custom button set!</b>\n"
        f"📝 Text: <code>{text}</code>\n"
        f"🔗 URL: {url}"
    )


# --- Admin: /broadcast ---
@router.message(Command("broadcast"))
async def broadcast_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("⚠️ <b>Usage:</b> <code>/broadcast [message text]</code>")
        return

    users = await database.broadcast_user_ids()
    await message.answer(f"📢 <i>Broadcasting to {len(users)} users...</i>")

    success_count = 0
    for user_id in users:
        try:
            await bot.send_message(chat_id=user_id, text=text)
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await message.answer(
        f"✅ <b>Broadcast complete!</b> Reached <code>{success_count}/{len(users)}</code> users."
    )


# --- Callback Handler: settings inline buttons ---
@router.callback_query(F.data.startswith("settings:"))
async def settings_callback_handler(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Unauthorized.", show_alert=True)
        return

    action = cb.data.split(":")[-1]

    if action == "toggle_protection":
        current = await database.get_setting("link_mode", "direct")
        new_mode = "shortener" if current == "direct" else "direct"
        await database.set_setting("link_mode", new_mode)
        await cb.answer(f"Protection set to: {new_mode.upper()}")

    elif action == "toggle_spoiler":
        current = await database.get_setting("start_photo_spoiler", "1")
        new_val = "0" if current == "1" else "1"
        await database.set_setting("start_photo_spoiler", new_val)
        await cb.answer("Spoiler toggled!")

    elif action == "remove_start_photo":
        await database.clear_start_photo()
        await cb.answer("Start photo removed.")

    elif action == "remove_delivery_sticker":
        await database.clear_delivery_sticker()
        await cb.answer("Delivery sticker removed.")

    elif action == "remove_button":
        await database.clear_custom_button()
        await cb.answer("Custom button removed.")

    elif action == "button":
        await cb.message.answer(
            "⌨️ To set or update the custom button, use:\n"
            "<code>/setbutton Text | https://url.com</code>"
        )
        await cb.answer()
        return

    elif action == "start_photo":
        await cb.message.answer(
            "🖼 To set a start photo, reply to an image with:\n<code>/setstartphoto</code>"
        )
        await cb.answer()
        return

    elif action == "delivery_sticker":
        await cb.message.answer(
            "🎟 To set a delivery sticker, reply to a sticker with:\n<code>/setsticker</code>"
        )
        await cb.answer()
        return

    elif action == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass
        return

    # Refresh dashboard after any toggle/remove
    photo_id, photo_spoiler = await database.start_photo()
    sticker_id = await database.delivery_sticker()
    btn_text, btn_url = await database.get_custom_button()
    link_mode = await database.get_setting("link_mode", "direct")
    short_domain = await database.get_setting("shortener_url", "arolinks.com")
    has_api_token = bool(await database.get_setting("arolinks_api_token", ""))

    kb = settings_keyboard(
        has_button=bool(btn_text),
        has_start_photo=bool(photo_id),
        has_delivery_sticker=bool(sticker_id),
        protected=(link_mode == "shortener"),
        spoiler=photo_spoiler
    )

    try:
        await cb.message.edit_text(
            text=(
                f"⚙️ <b>@{config.bot_username} Control Dashboard</b>\n\n"
                f"🌐 <b>Shortener Domain:</b> <code>{short_domain}</code>\n"
                f"🔑 <b>API Token:</b> <code>{'YES ✅' if has_api_token else 'NO ❌'}</code>\n"
                f"🔗 <b>Protection Mode:</b> <code>{link_mode.upper()}</code>"
            ),
            reply_markup=kb
        )
    except Exception as e:
        LOG.warning(f"Settings edit_text failed: {e}")