from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command, CommandObject
import logging
import asyncio
import re
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


# --- Helper: parse duration strings like "5m", "10min", "1h", "24h", "300s" ---
def parse_duration(text: str) -> int | None:
    text = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?", text)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2) or "m"
    if unit.startswith("s"):
        seconds = value
    elif unit.startswith("h"):
        seconds = value * 3600
    else:
        seconds = value * 60
    return seconds


def format_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''}"
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{seconds} seconds"


MAX_AUTODELETE_SECONDS = 24 * 3600
DEFAULT_AUTODELETE_SECONDS = 5 * 60


# --- Helper: schedule a message for deletion after N seconds ---
async def schedule_delete(chat_id: int, message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # message may already be deleted / too old — safe to ignore


def fire_and_forget_delete(chat_id: int, message_id: int, delay: int):
    asyncio.create_task(schedule_delete(chat_id, message_id, delay))


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
async def execution_welcome(message: Message, user):
    first_name = user.first_name or "there"
    mention = f'<a href="tg://user?id={user.id}">{first_name}</a>'

    try:
        sticker_id = await database.delivery_sticker()
        photo_id, spoiler_enabled = await database.start_photo()

        if sticker_id:
            try:
                status_msg = await message.answer_sticker(sticker=sticker_id)
                await asyncio.sleep(1.0)
                await status_msg.delete()
            except Exception as e:
                LOG.warning(f"Sticker send failed (skipping): {e}")

        # --- 3-stage loading animation ---
        stages = [
            f"👋 <b>Hello, {mention}!</b>",
            "🔍 <i>Verifying access parameters...</i>",
            "🔑 <i>Getting permissions ready...</i>",
        ]
        anim_msg = None
        try:
            anim_msg = await message.answer(stages[0])
            for stage_text in stages[1:]:
                await asyncio.sleep(0.9)
                await anim_msg.edit_text(stage_text)
            await asyncio.sleep(0.9)
        except Exception as e:
            LOG.warning(f"Welcome animation failed (skipping): {e}")

        custom_welcome = await database.get_setting("custom_welcome_html", "")
        if custom_welcome:
            welcome_text = (
                custom_welcome
                .replace("{mention}", mention)
                .replace("{name}", first_name)
                .replace("{first_name}", first_name)
            )
        else:
            welcome_text = (
                f"Hey {mention} 👋\n"
                f"<i>I am your secure file-sharing assistant</i> ✨\n\n"
                f"🪽 <b>Ready To Feel The Power?</b>\n\n"
                f"<blockquote>🚀 I share requested files through secure access links.\n"
                f"🛡️ Access is verified before a file is delivered.</blockquote>\n\n"
                f"🔗 <i>Open a valid file link to continue.</i>\n"
                f"📥 <i>Select a quality after verification.</i>"
            )

        if anim_msg is not None:
            try:
                await anim_msg.delete()
            except Exception:
                pass

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

    autodelete_seconds = int(await database.get_setting("autodelete_seconds", str(DEFAULT_AUTODELETE_SECONDS)))
    chat_id = message.chat.id

    if autodelete_seconds > 0:
        notice = await message.answer(
            f"⏱️ <i>These files will auto-delete in <b>{format_duration(autodelete_seconds)}</b> — "
            f"save or forward them now!</i>"
        )
        fire_and_forget_delete(chat_id, notice.message_id, autodelete_seconds)

    for file in files:
        try:
            sent = await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=int(config.storage_channel_id),
                message_id=int(file["storage_message_id"]),
                reply_markup=markup
            )
        except Exception as e:
            LOG.warning(f"Copy with markup failed, trying without: {e}")
            try:
                sent = await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=int(config.storage_channel_id),
                    message_id=int(file["storage_message_id"])
                )
            except Exception as inner_err:
                LOG.error(f"Critical file dispatch failure: {inner_err}")
                await message.answer(f"❌ Failed to extract: <code>{file['original_name']}</code>")
                continue

        if autodelete_seconds > 0:
            fire_and_forget_delete(chat_id, sent.message_id, autodelete_seconds)


# --- Command Handler: /help ---
@router.message(Command("help"))
async def help_handler(message: Message):
    if is_admin(message.from_user.id):
        text = (
            "🧭 <b>Admin Command List</b>\n\n"
            "<b>📤 File Sharing</b>\n"
            "<blockquote>"
            "• Just send/forward any file directly to me → instant link\n"
            "• <code>/batch</code> — start a multi-file batch session\n"
            "• <code>/done</code> — finish the batch and get one link\n"
            "• <code>/cancelbatch</code> — abort the current batch"
            "</blockquote>\n\n"
            "<b>⚙️ Protection & Delivery</b>\n"
            "<blockquote>"
            "• <code>/setshortner [domain]</code> — set shortener domain\n"
            "• <code>/setapitoken [token]</code> — set shortener API token\n"
            "• <code>/setwaittime [min] [max]</code> — anti-bypass timing window\n"
            "• <code>/setautodelete [duration]</code> — e.g. 5m, 1h, 24h, or 0 to disable\n"
            "• <code>/settutorial [url]</code> — tutorial video button on verify links\n"
            "• <code>/removetutorial</code> — remove tutorial button"
            "</blockquote>\n\n"
            "<b>🔒 Force-Subscribe</b>\n"
            "<blockquote>"
            "• <code>/addfsub [@chan|id] [link] [name]</code> — add a required channel\n"
            "• <code>/removefsub [@chan|id]</code> — remove one\n"
            "• <code>/listfsub</code> — list all\n"
            "• <code>/clearfsub</code> — remove all"
            "</blockquote>\n\n"
            "<b>🎨 Customization</b>\n"
            "<blockquote>"
            "• <code>/setwelcome [text]</code> — custom start message (rich formatting supported)\n"
            "• <code>/resetwelcome</code> — restore default start message\n"
            "• <code>/setstartphoto</code> — reply to an image to set start photo\n"
            "• <code>/setsticker</code> — reply to a sticker to set delivery sticker\n"
            "• <code>/setbutton Text | URL</code> — set custom delivery button\n"
            "• <code>/settings</code> — interactive settings dashboard"
            "</blockquote>\n\n"
            "<b>👥 Users & Bans</b>\n"
            "<blockquote>"
            "• <code>/ban [user_id|@username]</code>\n"
            "• <code>/unban [user_id|@username]</code>\n"
            "• <code>/users</code> — list all registered users\n"
            "• <code>/broadcast [text]</code> — message all users"
            "</blockquote>"
        )
    else:
        text = (
            "🧭 <b>Help</b>\n\n"
            "<blockquote>Just open a file link that was shared with you — I'll take care of the rest, "
            "including any verification steps.</blockquote>\n\n"
            "🔗 <i>Don't have a link? Ask wherever this bot was shared from.</i>"
        )
    await message.answer(text)


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
        await execution_welcome(message, user)
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

        keyboard_rows = [[InlineKeyboardButton(text="🔐 Complete Verification", url=verification_link)]]

        tutorial_link = await database.get_setting("tutorial_link", "")
        if tutorial_link:
            keyboard_rows.append([InlineKeyboardButton(text="🎬 See Tutorial Video", url=tutorial_link)])

        btn = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
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


# --- In-memory batch sessions: admin_id -> list of stored_file DB ids ---
BATCH_SESSIONS: dict[int, list[int]] = {}


def _extract_storable_label(message: Message) -> str:
    if message.document:
        return message.document.file_name or "Document"
    if message.video:
        return message.video.file_name or "Video"
    if message.audio:
        return message.audio.file_name or message.audio.title or "Audio"
    if message.photo:
        return "Photo"
    if message.voice:
        return "Voice Message"
    if message.animation:
        return message.animation.file_name or "Animation"
    if message.sticker:
        return "Sticker"
    if message.video_note:
        return "Video Note"
    if message.location:
        return "Location"
    if message.contact:
        return "Contact"
    if message.poll:
        return f"Poll: {message.poll.question[:30]}"
    if message.text:
        snippet = " ".join(message.text.strip().split())[:40]
        return snippet or "Text Message"
    return "Message"


# --- Admin: /batch (start a multi-file batch session) ---
@router.message(Command("batch"))
async def batch_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    BATCH_SESSIONS[message.from_user.id] = []
    await message.answer(
        "📦 <b>Batch mode started!</b>\n\n"
        "<blockquote>Send all the files you want in this batch now.</blockquote>\n\n"
        "✅ <i>Send</i> <code>/done</code> <i>when finished.</i>\n"
        "🚫 <i>Send</i> <code>/cancelbatch</code> <i>to abort.</i>"
    )


# --- Admin: /done (finalize the batch session into one link) ---
@router.message(Command("done"))
async def done_batch_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    file_ids = BATCH_SESSIONS.pop(message.from_user.id, None)
    if file_ids is None:
        await message.answer("⚠️ <i>No active batch session. Start one with</i> <code>/batch</code>.")
        return
    if not file_ids:
        await message.answer("⚠️ <i>No files were added to this batch.</i>")
        return

    is_protected = (await database.get_setting("link_mode", "direct") == "shortener")
    post_row = await database.create_post(kind="batch", file_ids=file_ids, protected=is_protected)
    share_url = f"https://t.me/{config.bot_username}?start=file_{post_row['code']}"
    await message.answer(
        f"📦 <b>Batch Link Created!</b>\n\n"
        f"📊 <b>Files:</b> <code>{len(file_ids)}</code>\n"
        f"📥 <b>Link:</b> <code>{share_url}</code>"
    )


# --- Admin: /cancelbatch ---
@router.message(Command("cancelbatch"))
async def cancel_batch_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    if BATCH_SESSIONS.pop(message.from_user.id, None) is not None:
        await message.answer("✅ <b>Batch session cancelled.</b>")
    else:
        await message.answer("⚠️ <i>No active batch session.</i>")


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


# --- Admin: /setautodelete ---
@router.message(Command("setautodelete"))
async def set_autodelete_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg:
        current = int(await database.get_setting("autodelete_seconds", str(DEFAULT_AUTODELETE_SECONDS)))
        await message.answer(
            "⚠️ <b>Usage:</b> <code>/setautodelete [duration]</code>\n\n"
            "<i>Examples: 5m, 10min, 1h, 24h, 0 (disable)</i>\n"
            f"Current: <b>{format_duration(current) if current > 0 else 'disabled'}</b>"
        )
        return

    if arg in ("0", "off", "disable", "none"):
        await database.set_setting("autodelete_seconds", "0")
        await message.answer("✅ <b>Auto-delete disabled.</b> Files will stay until manually removed.")
        return

    seconds = parse_duration(arg)
    if seconds is None:
        await message.answer("❌ <i>Couldn't parse that duration. Try something like</i> <code>5m</code>, <code>1h</code>, <code>30s</code>.")
        return
    if seconds > MAX_AUTODELETE_SECONDS:
        await message.answer(f"❌ <i>Maximum auto-delete time is 24 hours.</i>")
        return
    if seconds <= 0:
        await message.answer("❌ <i>Duration must be greater than 0 (or use</i> <code>0</code> <i>to disable).</i>")
        return

    await database.set_setting("autodelete_seconds", str(seconds))
    await message.answer(f"✅ <b>Auto-delete set to:</b> {format_duration(seconds)}")


# --- Admin: /setwelcome ---
@router.message(Command("setwelcome"))
async def set_welcome_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return

    # Use html_text to preserve bold/italic/underline/quote/links exactly as typed
    # using Telegram's own formatting toolbar, not just plain command args.
    raw_html = message.html_text or ""
    text = re.sub(r"^/setwelcome(@\w+)?\s*", "", raw_html, count=1).strip()

    if not text:
        await message.answer(
            "⚠️ <b>Usage:</b> <code>/setwelcome [your message]</code>\n\n"
            "<i>Format the text using Telegram's own formatting toolbar (bold, italic, "
            "underline, quote, links, etc.) — it will carry over exactly.</i>\n\n"
            "<i>Placeholders you can use:</i>\n"
            "<code>{mention}</code> <i>— clickable mention of the user</i>\n"
            "<code>{name}</code> <i>— plain first name (no link)</i>\n\n"
            "<i>Use</i> <code>/resetwelcome</code> <i>to restore the default message.</i>"
        )
        return

    await database.set_setting("custom_welcome_html", text)
    await message.answer("✅ <b>Custom welcome message saved!</b>\n\nHere's a preview:")
    mention = f'<a href="tg://user?id={message.from_user.id}">{message.from_user.first_name}</a>'
    await message.answer(
        text.replace("{mention}", mention).replace("{name}", message.from_user.first_name or "User")
    )


# --- Admin: /resetwelcome ---
@router.message(Command("resetwelcome"))
async def reset_welcome_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    await database.set_setting("custom_welcome_html", "")
    await message.answer("✅ <b>Welcome message reset to default.</b>")


# --- Admin: /settutorial ---
@router.message(Command("settutorial"))
async def set_tutorial_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    url = (command.args or "").strip()
    if not url:
        current = await database.get_setting("tutorial_link", "")
        await message.answer(
            "⚠️ <b>Usage:</b> <code>/settutorial [url]</code>\n\n"
            "<i>This link is shown as a \"🎬 See Tutorial Video\" button on every shortener "
            "verification message — point it at your public tutorial channel/video.</i>\n\n"
            f"Current: <code>{current or 'not set'}</code>"
        )
        return
    await database.set_setting("tutorial_link", url)
    await message.answer(f"✅ <b>Tutorial link set to:</b> {url}")


# --- Admin: /removetutorial ---
@router.message(Command("removetutorial"))
async def remove_tutorial_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    await database.set_setting("tutorial_link", "")
    await message.answer("✅ <b>Tutorial button removed from verification messages.</b>")


# --- Admin: /setmongo ---
@router.message(Command("setmongo"))
async def set_mongo_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").strip().split(maxsplit=1)
    if not args:
        current_uri = await database.get_setting("mongo_uri", "")
        current_db = await database.get_setting("mongo_db_name", "filesharebot")
        await message.answer(
            "⚠️ <b>Usage:</b> <code>/setmongo [connection_uri] [db_name]</code>\n\n"
            "<i>Every link/file/message gets mirrored to MongoDB. If the SQLite database is "
            "ever reset or redeployed, old links are automatically recovered from Mongo the "
            "first time they're opened.</i>\n\n"
            f"Current URI: <code>{'set' if current_uri else 'not set'}</code>\n"
            f"Current DB name: <code>{current_db}</code>"
        )
        return

    uri = args[0]
    db_name = args[1].strip() if len(args) > 1 else "filesharebot"
    await database.set_setting("mongo_uri", uri)
    await database.set_setting("mongo_db_name", db_name)
    await database.reset_mongo_connection()

    mongo_db = await database._get_mongo()
    if mongo_db is not None:
        await message.answer(f"✅ <b>MongoDB connected!</b>\nDatabase: <code>{db_name}</code>")
    else:
        await message.answer(
            "❌ <b>Could not connect to MongoDB.</b>\n"
            "<i>Double-check the connection URI and that the</i> <code>motor</code> <i>package is installed.</i>"
        )


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
        uname = f"@{r['username']}" if r['username'] else "No Username"
        text += f"• <code>{r['user_id']}</code> — {r['first_name']} ({uname})\n"

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


# --- Admin: send ANYTHING to the bot (file, text, link, sticker, etc.) to store + link it ---
# NOTE: this is a broad catch-all (no filter) — it MUST be the LAST handler registered
# in this router, or it will shadow every command/callback handler defined above it.
@router.message()
async def handle_direct_upload(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type != "private":
        return
    if message.text and message.text.startswith("/"):
        return  # unrecognized command — don't store it as content

    name = _extract_storable_label(message)

    try:
        copied = await bot.copy_message(
            chat_id=int(config.storage_channel_id),
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
    except Exception as e:
        LOG.error(f"Failed to copy message to storage channel: {e}")
        await message.answer(
            "❌ <i>Failed to store this — make sure the bot is an admin in the storage channel.</i>"
        )
        return

    file_row = await database.add_stored_file(
        storage_message_id=copied.message_id,
        original_name=name,
        tag="batch" if message.from_user.id in BATCH_SESSIONS else "single"
    )

    if message.from_user.id in BATCH_SESSIONS:
        BATCH_SESSIONS[message.from_user.id].append(int(file_row["id"]))
        count = len(BATCH_SESSIONS[message.from_user.id])
        await message.answer(
            f"✅ <b>Added to batch</b> (<code>{count}</code> item{'s' if count != 1 else ''} so far).\n"
            f"<i>Send more, or</i> <code>/done</code> <i>to finish.</i>"
        )
        return

    # Single item — generate its link immediately
    is_protected = (await database.get_setting("link_mode", "direct") == "shortener")
    post_row = await database.create_post(
        kind="single", file_ids=[int(file_row["id"])], protected=is_protected
    )
    share_url = f"https://t.me/{config.bot_username}?start=file_{post_row['code']}"
    await message.answer(
        f"🔗 <b>Link Generated!</b>\n\n"
        f"📄 <b>Item:</b> <code>{name}</code>\n"
        f"📥 <b>Link:</b> <code>{share_url}</code>"
    )