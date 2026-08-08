from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramForbiddenError
import logging
import asyncio
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
import aiohttp

from config import config
from style import small_caps, heading, label
from database import database
from keyboards import (
    settings_keyboard, custom_button, broadcast_confirm_keyboard, broadcast_progress_keyboard,
    fsub_panel_keyboard, fsub_remove_confirm_keyboard,
    button_manager_keyboard, button_delete_confirm_keyboard, render_configured_buttons,
    welcome_panel_keyboard, welcome_reset_confirm_keyboard,
    custom_batch_list_keyboard, custom_batch_detail_keyboard, custom_batch_delete_confirm_keyboard,
)

LOG = logging.getLogger("bot_handlers")
bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


def is_owner(user_id: int) -> bool:
    return user_id in config.owner_ids


async def is_moderator_or_above(user_id: int) -> bool:
    """MODERATOR is a dynamic, DB-managed tier below the static
    OWNER/ADMIN tiers from config (env-defined). Every admin/owner
    automatically counts as moderator-or-above."""
    if is_admin(user_id):
        return True
    mods = await database.get_moderator_ids()
    return user_id in mods


async def get_role_label(user_id: int) -> str:
    if is_owner(user_id):
        return "👑 Owner"
    if is_admin(user_id):
        return "🛡 Admin"
    if await is_moderator_or_above(user_id):
        return "🧰 Moderator"
    return "👤 User"


# --- New Log Channel: logs every verification attempt (success, expired,
# invalid, bypass) without touching the existing storage channel at all. ---
async def get_log_channel_id() -> int | None:
    raw = await database.get_setting("log_channel_id", "")
    if not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


async def send_verification_log(user, status: str, detail: str = "", link: str = "") -> None:
    """Fire-and-forget log to the configured Log Channel. Never raises -
    a log-channel failure (wrong ID, bot not admin there, etc.) must never
    break the actual user-facing verification flow."""
    channel_id = await get_log_channel_id()
    if not channel_id:
        return

    now = datetime.now(timezone.utc)
    status_labels = {
        "ok": "✅ <b>ᴠᴇʀɪꜰɪᴇᴅ</b>",
        "expired": "⏰ <b>ᴇxᴘɪʀᴇᴅ</b>",
        "missing": "❌ <b>ɪɴᴠᴀʟɪᴅ ᴛᴏᴋᴇɴ</b>",
        "too_fast": "🚫 <b>Bypass Attempt (too fast)</b>",
        "user_mismatch": "🚫 <b>Bypass Attempt (token hijack)</b>",
        "used": "⚠️ <b>Retry — Already Used</b>",
    }
    label = status_labels.get(status, f"ℹ️ <b>{status}</b>")

    full_name = " ".join(filter(None, [user.first_name, user.last_name])) or "Unknown"
    username = f"@{user.username}" if user.username else "N/A"

    text = (
        f"{label}\n\n"
        f"👤 <b>ɴᴀᴍᴇ:</b> {full_name}\n"
        f"🔗 <b>ᴜꜱᴇʀɴᴀᴍᴇ:</b> {username}\n"
        f"🆔 <b>ᴜꜱᴇʀ ɪᴅ:</b> <code>{user.id}</code>\n"
        f"🤖 <b>ʙᴏᴛ:</b> @{config.bot_username}\n"
        f"📅 <b>ᴅᴀᴛᴇ:</b> {now.strftime('%Y-%m-%d')}\n"
        f"⏱ <b>Time (UTC):</b> {now.strftime('%H:%M:%S')}\n"
    )
    if link:
        text += f"🌐 <b>ʟɪɴᴋ:</b> {link}\n"
    if detail:
        text += f"📝 <b>ᴅᴇᴛᴀɪʟ:</b> <i>{detail}</i>\n"

    try:
        await bot.send_message(channel_id, text)
    except TelegramAPIError as exc:
        LOG.warning(f"Failed to send verification log to log channel {channel_id}: {exc}")


# --- Lightweight anti-flood: blocks a user who fires too many messages in
# a short burst, without needing any extra dependency or DB table. ---
_flood_tracker: dict[int, list[float]] = {}
_flood_warned: dict[int, float] = {}
FLOOD_MAX_MESSAGES = 6
FLOOD_WINDOW_SECONDS = 4.0
FLOOD_WARN_COOLDOWN = 5.0


def is_flooding(user_id: int) -> bool:
    now = time.monotonic()
    hits = _flood_tracker.setdefault(user_id, [])
    hits[:] = [t for t in hits if now - t < FLOOD_WINDOW_SECONDS]
    hits.append(now)
    return len(hits) > FLOOD_MAX_MESSAGES


def should_warn_flood(user_id: int) -> bool:
    now = time.monotonic()
    last_warn = _flood_warned.get(user_id, 0)
    if now - last_warn < FLOOD_WARN_COOLDOWN:
        return False
    _flood_warned[user_id] = now
    return True


@router.message.middleware()
async def anti_flood_middleware(handler, event: Message, data: dict):
    user = event.from_user
    if user is None or is_admin(user.id):
        return await handler(event, data)  # never throttle admins

    if is_flooding(user.id):
        if should_warn_flood(user.id):
            try:
                await event.answer(
                    "🚦 <b>ꜱʟᴏᴡ ᴅᴏᴡɴ!</b>\n<i>You're sending messages too fast — please wait a few seconds.</i>"
                )
            except TelegramAPIError:
                pass
        return  # drop this update, don't call the actual handler

    return await handler(event, data)


@router.callback_query.middleware()
async def callback_safety_middleware(handler, event: CallbackQuery, data: dict):
    """Catches any exception a callback handler raises (stale data, expired
    session, unexpected None, etc.) and shows a friendly alert instead of
    leaving the button stuck loading or crashing the update loop."""
    try:
        return await handler(event, data)
    except TelegramAPIError:
        raise  # let real Telegram API errors (flood wait etc.) propagate as before
    except Exception as exc:
        LOG.exception(f"Unhandled error in callback handler for data={event.data!r}: {exc}")
        try:
            await event.answer("⚠️ This action failed or the session expired. Please try again.", show_alert=True)
        except TelegramAPIError:
            pass


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


# --- Helper: schedule a message for deletion after N seconds.
# Persistent: the deletion is recorded in the DB the moment it's scheduled
# and removed once it actually fires, so a bot restart mid-countdown does
# NOT lose it — main.py sweeps and re-applies pending deletions on startup. ---
async def schedule_delete(chat_id: int, message_id: int, delay: int):
    delete_at = int(time.time()) + max(delay, 0)
    try:
        await database.add_scheduled_deletion(chat_id, message_id, delete_at)
    except Exception as e:
        LOG.warning(f"Could not persist scheduled deletion (will still fire this run): {e}")
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # message may already be deleted / too old — safe to ignore
    finally:
        try:
            await database.remove_scheduled_deletion(chat_id, message_id)
        except Exception as e:
            LOG.warning(f"Could not clear persisted scheduled deletion: {e}")


def fire_and_forget_delete(chat_id: int, message_id: int, delay: int):
    asyncio.create_task(schedule_delete(chat_id, message_id, delay))


async def resume_scheduled_deletions() -> int:
    """Called once at startup. Any deletion that was due while the bot was
    offline fires immediately; anything still in the future is
    re-scheduled for its remaining delay. Never raises — a failure here
    must not block bot startup."""
    resumed = 0
    try:
        pending = await database.get_all_scheduled_deletions()
    except Exception as e:
        LOG.warning(f"Could not load persisted scheduled deletions on startup: {e}")
        return 0
    now = int(time.time())
    for row in pending:
        remaining = max(int(row["delete_at"]) - now, 0)
        asyncio.create_task(
            schedule_delete(int(row["chat_id"]), int(row["message_id"]), remaining)
        )
        resumed += 1
    return resumed


# --- Helper: Force-Subscribe membership check (supports multiple channels).
# Only entries the admin has left ENABLED in the Force-Subscribe Manager
# are ever checked or shown to users — disabled entries are skipped
# entirely, same list/storage, no second Force-Sub system. ---
async def get_unjoined_fsub_channels(user_id: int) -> list[dict]:
    channels = await database.get_enabled_fsub_channels()
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


# --- Helper: shorten URL via the configured shortener domain's API ---
async def shorten_with_arolinks(destination_url: str) -> str:
    api_token = await database.get_setting("arolinks_api_token", "")
    domain = (await database.get_setting("shortener_url", "arolinks.com")).strip()
    md_match = re.match(r"^\[.*?\]\((.+?)\)$", domain)
    if md_match:
        domain = md_match.group(1)
    domain = domain.replace("https://", "").replace("http://", "").strip()
    domain = domain.split("/")[0].split("?")[0].strip()

    if not api_token:
        LOG.warning("Shortener API token not set — sending unshortened link (no ad revenue will be generated).")
        return destination_url

    try:
        api_url = f"https://{domain}/api?api={api_token}&url={urllib.parse.quote(destination_url)}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                raw = await resp.text()
                if resp.status == 200:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        LOG.error(f"Shortener API returned non-JSON on {domain}: {raw[:200]}")
                        return destination_url
                    shortened = data.get("shortenedUrl") or data.get("shorten_url") or data.get("short")
                    if shortened:
                        return shortened
                    LOG.error(f"Shortener API on {domain} returned no shortenedUrl field: {raw[:200]}")
                else:
                    LOG.error(f"Shortener API on {domain} returned HTTP {resp.status}: {raw[:200]}")
    except Exception as e:
        LOG.error(f"Shortener API error calling {domain}: {e}")

    # Falling back to the raw destination means NO ad view / NO wallet credit for this click.
    LOG.warning(f"Falling back to unshortened link for {destination_url} — shortener call to {domain} failed.")
    return destination_url


def _default_welcome_text(mention: str) -> str:
    return (
        f"Hey {mention} 👋\n"
        f"<i>I am your secure file-sharing assistant</i> ✨\n\n"
        f"🪽 {heading('Ready To Feel The Power?')}\n\n"
        f"<blockquote>🚀 I share requested files through secure access links.\n"
        f"🛡️ Access is verified before a file is delivered.</blockquote>\n\n"
        f"🔗 <i>Open a valid file link to continue.</i>\n"
        f"📥 <i>Select a quality after verification.</i>"
    )


# Speed name -> per-stage delay in seconds, used by both the real welcome
# flow and the /welcome preview so they always match.
ANIM_SPEED_SECONDS = {"slow": 1.6, "normal": 0.9, "fast": 0.4}


# --- Core Dynamic Welcome Engine — fully driven by the Welcome
# Customization settings (get_welcome_config), the single source of truth
# also used by the /welcome admin UI. ---
async def execution_welcome(message: Message, user):
    first_name = user.first_name or "there"
    mention = f'<a href="tg://user?id={user.id}">{first_name}</a>'

    try:
        cfg = await database.get_welcome_config()

        if not cfg.get("enabled", True):
            # Welcome message disabled entirely — closest stable real
            # behavior is just not sending the fancy flow at all.
            return

        sticker_id = cfg.get("sticker_id")
        photo_id = cfg.get("photo_id")
        spoiler_enabled = cfg.get("spoiler", True)
        delay = ANIM_SPEED_SECONDS.get(cfg.get("anim_speed", "normal"), 0.9)

        if sticker_id:
            try:
                status_msg = await message.answer_sticker(sticker=sticker_id)
                if cfg.get("sticker_anim_enabled", True):
                    # Telegram doesn't expose a "play sticker animation"
                    # API call — the closest real, stable behavior is
                    # showing it briefly before it's replaced by the
                    # welcome text (native animated stickers, e.g. .tgs/
                    # video stickers, already animate on their own once
                    # Telegram renders them; a static sticker has nothing
                    # to animate, so this only controls how long it's
                    # shown for).
                    await asyncio.sleep(delay)
                    await status_msg.delete()
                # sticker_anim_enabled == False -> leave the sticker as a
                # persistent message instead of deleting it.
            except Exception as e:
                LOG.warning(f"Sticker send failed (skipping): {e}")

        anim_msg = None
        if cfg.get("anim_enabled", True):
            stages = [
                f"👋 <b>Hello, {mention}!</b>",
                "🔍 <i>Verifying access parameters...</i>",
                "🔑 <i>Getting permissions ready...</i>",
            ]
            try:
                anim_msg = await message.answer(stages[0])
                for stage_text in stages[1:]:
                    await asyncio.sleep(delay)
                    await anim_msg.edit_text(stage_text)
                await asyncio.sleep(delay)
            except Exception as e:
                LOG.warning(f"Welcome animation failed (skipping): {e}")

        custom_welcome = cfg.get("text") or ""
        if custom_welcome:
            welcome_text = (
                custom_welcome
                .replace("{mention}", mention)
                .replace("{name}", first_name)
                .replace("{first_name}", first_name)
            )
        else:
            welcome_text = _default_welcome_text(mention)

        if anim_msg is not None:
            try:
                await anim_msg.delete()
            except Exception:
                pass

        extra_kb = render_configured_buttons(await database.list_buttons())

        if photo_id:
            await message.answer_photo(
                photo=photo_id,
                caption=welcome_text,
                has_spoiler=spoiler_enabled,
                reply_markup=extra_kb,
            )
        else:
            await message.answer(welcome_text, reply_markup=extra_kb)

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

    if message.from_user:
        await database.increment_download_count(message.from_user.id)
        try:
            first_name = files[0]["original_name"] if files else ""
            await database.record_download(message.from_user.id, post_row["code"], first_name)
        except Exception as e:
            LOG.warning(f"record_download failed (non-fatal, delivery unaffected): {e}")

    btn_text, btn_url = await database.get_custom_button()
    quick_markup = custom_button(btn_text, btn_url) if (btn_text and btn_url) else None
    configured_markup = render_configured_buttons(await database.list_buttons())
    if quick_markup and configured_markup:
        markup = InlineKeyboardMarkup(
            inline_keyboard=quick_markup.inline_keyboard + configured_markup.inline_keyboard
        )
    else:
        markup = quick_markup or configured_markup

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
def _rank_for(verification_count: int, download_count: int) -> str:
    score = verification_count + download_count
    if score >= 500:
        return "💎 Diamond"
    if score >= 200:
        return "🥇 Gold"
    if score >= 50:
        return "🥈 Silver"
    if score >= 10:
        return "🥉 Bronze"
    return "🌱 Newcomer"


@router.message(Command("profile"))
async def profile_handler(message: Message):
    user = message.from_user
    profile = await database.get_profile(user.id)
    if not profile:
        await message.answer("⚠️ <i>No profile found yet — send /start first.</i>")
        return

    joined = datetime.fromtimestamp(profile["joined_at"], tz=timezone.utc).strftime("%Y-%m-%d")
    last_seen = datetime.fromtimestamp(profile["last_seen"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    premium_active = profile["premium_until"] > int(time.time())
    premium_text = (
        f"⭐ Premium (until {datetime.fromtimestamp(profile['premium_until'], tz=timezone.utc).strftime('%Y-%m-%d')})"
        if premium_active else "Free"
    )
    rank = _rank_for(profile["verification_count"], profile["download_count"])
    username = f"@{profile['username']}" if profile["username"] else "N/A"

    text = (
        "👤 <b>ʏᴏᴜʀ ᴘʀᴏꜰɪʟᴇ</b>\n\n"
        f"🆔 <b>ᴜꜱᴇʀ ɪᴅ:</b> <code>{profile['user_id']}</code>\n"
        f"📛 <b>ɴᴀᴍᴇ:</b> {profile['first_name']}\n"
        f"🔗 <b>ᴜꜱᴇʀɴᴀᴍᴇ:</b> {username}\n"
        f"📅 <b>ᴊᴏɪɴᴇᴅ:</b> {joined}\n"
        f"🕒 <b>ʟᴀꜱᴛ ᴀᴄᴛɪᴠɪᴛʏ:</b> {last_seen}\n\n"
        f"🏆 <b>ʀᴀɴᴋ:</b> {rank}\n"
        f"✅ <b>ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴꜱ:</b> <code>{profile['verification_count']}</code>\n"
        f"📥 <b>ᴅᴏᴡɴʟᴏᴀᴅꜱ:</b> <code>{profile['download_count']}</code>\n"
        f"🤝 <b>ʀᴇꜰᴇʀʀᴀʟꜱ:</b> <code>{profile['referral_count']}</code>\n"
        f"⭐ <b>ꜱᴛᴀᴛᴜꜱ:</b> {premium_text}\n"
        f"⚠️ <b>ᴡᴀʀɴɪɴɢꜱ:</b> <code>{profile['warnings']}/{config.strike_limit}</code>\n"
    )
    await message.answer(text)


@router.message(Command("help"))
async def help_handler(message: Message):
    if is_admin(message.from_user.id):
        text = (
            "🧭 <b>ᴀᴅᴍɪɴ ᴄᴏᴍᴍᴀɴᴅ ʟɪꜱᴛ</b>\n\n"
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
            "</blockquote>\n\n"
            "<b>📊 Reports & Data</b>\n"
            "<blockquote>"
            "• <code>/dashboard</code> — advanced live statistics\n"
            "• <code>/search keyword</code> — find a stored file\n"
            "• <code>/categories</code> — file counts by tag\n"
            "• <code>/backup</code> — export a full JSON snapshot\n"
            "• <code>/restore</code> — reply to a backup file to insert-only restore it"
            "</blockquote>\n\n"
            "<b>🪪 Roles & Health</b> <i>(Owner-only marked ⚑)</i>\n"
            "<blockquote>"
            "• <code>/role</code> — show your own role\n"
            "• <code>/addmod [user]</code> / <code>/removemod [user]</code> — manage moderators\n"
            "• <code>/listmods</code> — list owner/admin/moderator staff\n"
            "• <code>/health</code> — bot/DB/storage/shortener status\n"
            "• <code>/setshortner</code> ⚑ / <code>/setapitoken</code> ⚑ / <code>/setmongo</code> ⚑ / <code>/restore</code> ⚑"
            "</blockquote>"
        )
    else:
        text = (
            "🧭 <b>ʜᴇʟᴘ</b>\n\n"
            "<blockquote>Just open a file link that was shared with you — I'll take care of the rest, "
            "including any verification steps.</blockquote>\n\n"
            "⭐ <b>ᴇxᴛʀᴀꜱ</b>\n"
            "<blockquote>"
            "• <code>/fav CODE</code> / <code>/unfav CODE</code> — save or remove a favorite\n"
            "• <code>/favorites</code> — your saved links\n"
            "• <code>/history</code> — your recent downloads\n"
            "• <code>/profile</code> — your stats and rank\n"
            "• <code>/role</code> — check if you have staff permissions"
            "</blockquote>\n\n"
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
        await message.answer("⛔ <b>ᴀᴄᴄᴇꜱꜱ ᴅᴇɴɪᴇᴅ.</b>\n<i>You are permanently banned from using this platform.</i>")
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
        "🔒 <b>ᴏɴᴇ ǫᴜɪᴄᴋ ꜱᴛᴇᴘ!</b>\n\n"
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
            await message.answer("⛔ <b>ʙᴀɴɴᴇᴅ!</b>\n<i>You have been blacklisted for attempting to bypass our shortener protections.</i>")
        else:
            await message.answer(
                f"⚠️ <b>ʙʏᴘᴀꜱꜱ ᴅᴇᴛᴇᴄᴛᴇᴅ!</b>\n\n"
                f"<blockquote>Do not use scraper tools or direct links. Please go through the shortener page verification.</blockquote>\n\n"
                f"🚨 <b>ꜱᴛʀɪᴋᴇꜱ:</b> <code>{count}/{config.strike_limit}</code> — <i>reaching the limit results in a permanent ban.</i>"
            )
        return

    # --- One-time token handler (post-shortener delivery) ---
    if payload.startswith("tok_"):
        token = payload[4:]
        status, post = await database.claim_token(token, user.id)

        if status == "missing":
            await send_verification_log(user, "missing", detail=f"token={token}")
            await message.answer(
                "❌ <b>ɪɴᴠᴀʟɪᴅ ᴛᴏᴋᴇɴ!</b>\n\n"
                "<i>This verification link is invalid. Please request the file link again.</i>"
            )
            return
        elif status == "expired":
            await send_verification_log(user, "expired", detail=f"token={token}")
            await message.answer(
                "⏰ <b>ᴛᴏᴋᴇɴ ᴇxᴘɪʀᴇᴅ!</b>\n\n"
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
            await send_verification_log(user, "too_fast", detail=f"strikes={count}/{config.strike_limit}, banned={banned}")
            if banned:
                await message.answer(
                    "⛔ <b>ʙᴀɴɴᴇᴅ!</b>\n\n"
                    "<i>You have been permanently banned for repeatedly bypassing our shortener verification.</i>"
                )
            else:
                await message.answer(
                    "😏 <b>ɴɪᴄᴇ ᴛʀʏ, ꜱᴍᴀʀᴛᴀꜱꜱ.</b>\n\n"
                    "<blockquote>You grabbed this link way too fast to have actually gone through the "
                    "shortener verification — looks like you used a bypass tool instead of "
                    "doing it the honest way.</blockquote>\n\n"
                    f"🚨 <b>ꜱᴛʀɪᴋᴇꜱ:</b> <code>{count}/{config.strike_limit}</code> — <i>reaching the "
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
            await send_verification_log(user, "user_mismatch", detail=f"strikes={count}/{config.strike_limit}, banned={banned}")
            if banned:
                await message.answer(
                    "⛔ <b>ʙᴀɴɴᴇᴅ!</b>\n"
                    "<i>You have been permanently banned for attempting to steal another user's verification token.</i>"
                )
            else:
                await message.answer(
                    f"🚫 <b>ᴀᴄᴄᴇꜱꜱ ᴅᴇɴɪᴇᴅ!</b>\n\n"
                    f"<blockquote>This verification link was generated for a different account and cannot be reused.</blockquote>\n"
                    f"🚨 <b>ꜱᴛʀɪᴋᴇꜱ:</b> <code>{count}/{config.strike_limit}</code>"
                )
            return
        elif status == "used":
            await send_verification_log(user, "used", detail=f"token={token}")
            await message.answer(
                "⚠️ <b>ᴀʟʀᴇᴀᴅʏ ᴜꜱᴇᴅ!</b>\n\n"
                "<i>This verification link has already been claimed. Each link is single-use only.</i>\n"
                "🔗 <i>Please open the original file link again to generate a new one.</i>"
            )
            return

        # Token is valid — deliver files
        await send_verification_log(user, "ok", detail=f"post_id={post['id'] if post else 'N/A'}")
        await database.increment_verification_count(user.id)
        await deliver_files(message, post)
        return

    # --- Direct file link handler ---
    code = payload.replace("file_", "").replace("get_", "")

    post = await database.get_post(code)
    if not post:
        await message.answer("❌ <b>ʟɪɴᴋ ɪɴᴠᴀʟɪᴅ</b>\n<i>This file record could not be found or has expired.</i>")
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
            "🛡️ <b>ʟɪɴᴋ ᴘʀᴏᴛᴇᴄᴛᴇᴅ!</b>\n\n"
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
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> Reply to an image with <code>/setstartphoto</code>")
        return
    file_id = message.reply_to_message.photo[-1].file_id
    await database.set_start_photo(file_id)
    await message.answer("✅ <b>ꜱᴛᴀʀᴛ ᴘʜᴏᴛᴏ ᴜᴘᴅᴀᴛᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ!</b>")


# --- Admin: /setsticker ---
@router.message(Command("setsticker"))
async def set_sticker_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> Reply to a sticker with <code>/setsticker</code>")
        return
    file_id = message.reply_to_message.sticker.file_id
    await database.set_delivery_sticker(file_id)
    await message.answer("✅ <b>ᴡᴇʟᴄᴏᴍᴇ ꜱᴛɪᴄᴋᴇʀ ꜱᴀᴠᴇᴅ!</b>")


# --- In-memory batch sessions: admin_id -> list of stored_file DB ids ---
BATCH_SESSIONS: dict[int, list[int]] = {}

# --- Transient "waiting for the admin's next text message" capture, used
# by the Force-Subscribe / Button Manager / Welcome Customization admin
# panels for the handful of fields that need free-text input (a label, a
# URL, a caption...). This is intentionally NOT where any durable state
# lives — every actual setting is written to the database as soon as the
# admin sends it, so a restart mid-input just means they type it again,
# never that a saved setting is lost. admin_id -> {"action": str, ...}. ---
PENDING_ADMIN_INPUT: dict[int, dict] = {}


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
        "📦 <b>ʙᴀᴛᴄʜ ᴍᴏᴅᴇ ꜱᴛᴀʀᴛᴇᴅ!</b>\n\n"
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
        f"📦 <b>ʙᴀᴛᴄʜ ʟɪɴᴋ ᴄʀᴇᴀᴛᴇᴅ!</b>\n\n"
        f"📊 <b>ꜰɪʟᴇꜱ:</b> <code>{len(file_ids)}</code>\n"
        f"📥 <b>ʟɪɴᴋ:</b> <code>{share_url}</code>"
    )


# --- Admin: /cancelbatch ---
@router.message(Command("cancelbatch"))
async def cancel_batch_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    if BATCH_SESSIONS.pop(message.from_user.id, None) is not None:
        await message.answer("✅ <b>ʙᴀᴛᴄʜ ꜱᴇꜱꜱɪᴏɴ ᴄᴀɴᴄᴇʟʟᴇᴅ.</b>")
    else:
        await message.answer("⚠️ <i>No active batch session.</i>")


# --- Admin: /setshortner ---
@router.message(Command("setshortner"))
async def set_shortener_handler(message: Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        await message.answer("❌ <i>Owner-only command — shortener config affects the whole verification flow.</i>")
        return
    raw = (command.args or "").strip()
    if not raw:
        current = await database.get_setting("shortener_url", "")
        await message.answer(
            "⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/setshortner arolinks.com</code>\n\n"
            "<i>Just the bare domain — no https://, no path, no markdown links.</i>\n\n"
            f"Current: <code>{current or 'not set'}</code>"
        )
        return

    # Strip markdown-link syntax like [text](url) if pasted from somewhere
    md_match = re.match(r"^\[.*?\]\((.+?)\)$", raw)
    if md_match:
        raw = md_match.group(1)

    # Strip scheme, then keep only the host (drop any /path, query, etc.)
    cleaned = raw.replace("https://", "").replace("http://", "").strip()
    domain = cleaned.split("/")[0].split("?")[0].strip().lower()

    if not domain or "." not in domain or " " in domain:
        await message.answer(
            f"❌ <b>ᴛʜᴀᴛ ᴅᴏᴇꜱɴ'ᴛ ʟᴏᴏᴋ ʟɪᴋᴇ ᴀ ᴠᴀʟɪᴅ ᴅᴏᴍᴀɪɴ:</b> <code>{raw}</code>\n\n"
            f"<i>Enter just the bare domain, e.g.</i> <code>/setshortner arolinks.com</code>"
        )
        return

    await database.set_setting("shortener_url", domain)
    await message.answer(
        f"✅ <b>ꜱʜᴏʀᴛᴇɴᴇʀ ᴅᴏᴍᴀɪɴ ꜱᴇᴛ ᴛᴏ:</b> <code>{domain}</code>\n\n"
        f"<i>Run</i> <code>/testshortner</code> <i>to confirm it actually works.</i>"
    )


# --- Admin: /setlogchannel — configures the NEW log channel (separate
# from the existing storage channel, which this never touches). ---
@router.message(Command("setlogchannel"))
async def set_log_channel_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        current = await get_log_channel_id()
        await message.answer(
            "⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/setlogchannel -1001234567890</code>\n\n"
            "<i>Add this bot as admin in that channel first, then run this command with its numeric ID.</i>\n"
            "<i>Send</i> <code>/setlogchannel off</code> <i>to disable logging.</i>\n\n"
            f"Current: <code>{current if current else 'not set'}</code>"
        )
        return

    if raw.lower() == "off":
        await database.set_setting("log_channel_id", "")
        await message.answer("✅ <b>ʟᴏɢ ᴄʜᴀɴɴᴇʟ ʟᴏɢɢɪɴɢ ᴅɪꜱᴀʙʟᴇᴅ.</b>")
        return

    try:
        channel_id = int(raw)
    except ValueError:
        await message.answer("❌ <b>ɪɴᴠᴀʟɪᴅ ᴄʜᴀɴɴᴇʟ ɪᴅ.</b> It must be a numeric ID like <code>-1001234567890</code>.")
        return

    try:
        await bot.send_message(channel_id, "✅ <b>ᴛʜɪꜱ ᴄʜᴀɴɴᴇʟ ɪꜱ ɴᴏᴡ ꜱᴇᴛ ᴀꜱ ᴛʜᴇ ʟᴏɢ ᴄʜᴀɴɴᴇʟ.</b>\n<i>Verification events will be posted here.</i>")
    except TelegramAPIError as exc:
        await message.answer(
            f"❌ <b>ᴄᴏᴜʟᴅ ɴᴏᴛ ꜱᴇɴᴅ ᴀ ᴛᴇꜱᴛ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴛʜᴀᴛ ᴄʜᴀɴɴᴇʟ:</b> <code>{exc}</code>\n\n"
            f"<i>Make sure the bot is an admin there and the ID is correct.</i>"
        )
        return

    await database.set_setting("log_channel_id", str(channel_id))
    await message.answer(f"✅ <b>ʟᴏɢ ᴄʜᴀɴɴᴇʟ ꜱᴇᴛ ᴛᴏ:</b> <code>{channel_id}</code>")


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
            "⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/setwaittime [min_seconds] [max_seconds]</code>\n\n"
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
            "⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/addfsub [@channel_or_chat_id] [invite_link] [display_name]</code>\n\n"
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
        f"✅ <b>ᴀᴅᴅᴇᴅ ꜰᴏʀᴄᴇ-ꜱᴜʙꜱᴄʀɪʙᴇ ᴄʜᴀɴɴᴇʟ:</b> <code>{chat_ref}</code>\n"
        f"📊 <b>ᴛᴏᴛᴀʟ ᴄʜᴀɴɴᴇʟꜱ:</b> <code>{len(channels)}</code>"
    )


# --- Admin: /removefsub ---
@router.message(Command("removefsub"))
async def remove_fsub_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    chat_ref = (command.args or "").strip()
    if not chat_ref:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/removefsub [@channel_or_chat_id]</code>")
        return

    channels = await database.get_fsub_channels()
    remaining = [c for c in channels if c.get("chat") != chat_ref]
    if len(remaining) == len(channels):
        await message.answer(f"⚠️ <code>{chat_ref}</code> was not found in the Force-Subscribe list.")
        return
    await database.set_fsub_channels(remaining)
    await message.answer(
        f"✅ <b>ʀᴇᴍᴏᴠᴇᴅ:</b> <code>{chat_ref}</code>\n"
        f"📊 <b>ʀᴇᴍᴀɪɴɪɴɢ ᴄʜᴀɴɴᴇʟꜱ:</b> <code>{len(remaining)}</code>"
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
    await message.answer("✅ <b>ᴀʟʟ ꜰᴏʀᴄᴇ-ꜱᴜʙꜱᴄʀɪʙᴇ ᴄʜᴀɴɴᴇʟꜱ ᴄʟᴇᴀʀᴇᴅ.</b>")


# --- Admin: /setautodelete ---
@router.message(Command("setautodelete"))
async def set_autodelete_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg:
        current = int(await database.get_setting("autodelete_seconds", str(DEFAULT_AUTODELETE_SECONDS)))
        await message.answer(
            "⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/setautodelete [duration]</code>\n\n"
            "<i>Examples: 5m, 10min, 1h, 24h, 0 (disable)</i>\n"
            f"Current: <b>{format_duration(current) if current > 0 else 'disabled'}</b>"
        )
        return

    if arg in ("0", "off", "disable", "none"):
        await database.set_setting("autodelete_seconds", "0")
        await message.answer("✅ <b>ᴀᴜᴛᴏ-ᴅᴇʟᴇᴛᴇ ᴅɪꜱᴀʙʟᴇᴅ.</b> Files will stay until manually removed.")
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
    await message.answer(f"✅ <b>ᴀᴜᴛᴏ-ᴅᴇʟᴇᴛᴇ ꜱᴇᴛ ᴛᴏ:</b> {format_duration(seconds)}")


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
            "⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/setwelcome [your message]</code>\n\n"
            "<i>Format the text using Telegram's own formatting toolbar (bold, italic, "
            "underline, quote, links, etc.) — it will carry over exactly.</i>\n\n"
            "<i>Placeholders you can use:</i>\n"
            "<code>{mention}</code> <i>— clickable mention of the user</i>\n"
            "<code>{name}</code> <i>— plain first name (no link)</i>\n\n"
            "<i>Use</i> <code>/resetwelcome</code> <i>to restore the default message.</i>"
        )
        return

    await database.set_setting("custom_welcome_html", text)
    await message.answer("✅ <b>ᴄᴜꜱᴛᴏᴍ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ ꜱᴀᴠᴇᴅ!</b>\n\nHere's a preview:")
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
    await message.answer("✅ <b>ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ.</b>")


# --- Admin: /settutorial ---
@router.message(Command("settutorial"))
async def set_tutorial_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    url = (command.args or "").strip()
    if not url:
        current = await database.get_setting("tutorial_link", "")
        await message.answer(
            "⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/settutorial [url]</code>\n\n"
            "<i>This link is shown as a \"🎬 See Tutorial Video\" button on every shortener "
            "verification message — point it at your public tutorial channel/video.</i>\n\n"
            f"Current: <code>{current or 'not set'}</code>"
        )
        return
    await database.set_setting("tutorial_link", url)
    await message.answer(f"✅ <b>ᴛᴜᴛᴏʀɪᴀʟ ʟɪɴᴋ ꜱᴇᴛ ᴛᴏ:</b> {url}")


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
    if not is_owner(message.from_user.id):
        await message.answer("❌ <i>Owner-only command — database backend config.</i>")
        return
    args = (command.args or "").strip().split(maxsplit=1)
    if not args:
        current_uri = await database.get_setting("mongo_uri", "")
        current_db = await database.get_setting("mongo_db_name", "filesharebot")
        await message.answer(
            "⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/setmongo [connection_uri] [db_name]</code>\n\n"
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
        await message.answer(f"✅ <b>ᴍᴏɴɢᴏᴅʙ ᴄᴏɴɴᴇᴄᴛᴇᴅ!</b>\nDatabase: <code>{db_name}</code>")
    else:
        await message.answer(
            "❌ <b>ᴄᴏᴜʟᴅ ɴᴏᴛ ᴄᴏɴɴᴇᴄᴛ ᴛᴏ ᴍᴏɴɢᴏᴅʙ.</b>\n"
            "<i>Double-check the connection URI and that the</i> <code>motor</code> <i>package is installed.</i>"
        )


# --- Admin: /testshortner ---
@router.message(Command("testshortner"))
async def test_shortener_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    domain = (await database.get_setting("shortener_url", "arolinks.com")).strip()
    token = await database.get_setting("arolinks_api_token", "")

    if not token:
        await message.answer("❌ <b>ɴᴏ ᴀᴘɪ ᴛᴏᴋᴇɴ ꜱᴇᴛ.</b> Use <code>/setapitoken [token]</code> first.")
        return

    test_url = "https://t.me/"
    result = await shorten_with_arolinks(test_url)

    if result == test_url:
        await message.answer(
            f"❌ <b>ꜱʜᴏʀᴛᴇɴᴇʀ ᴄᴀʟʟ ꜰᴀɪʟᴇᴅ.</b>\n\n"
            f"🌐 <b>ᴅᴏᴍᴀɪɴ ᴜꜱᴇᴅ:</b> <code>{domain}</code>\n"
            f"🔑 <b>ᴛᴏᴋᴇɴ ꜱᴇᴛ:</b> <code>{token[:6]}...{token[-4:] if len(token) > 10 else ''}</code>\n\n"
            f"<i>The bot silently fell back to an unshortened link, which means no ad view / no wallet "
            f"credit happens on real links either. Check the bot's logs for the exact API error, and "
            f"double-check that</i> <code>{domain}</code> <i>is the exact domain shown on your shortener "
            f"account's API/dashboard page (not just a display name).</i>"
        )
    else:
        await message.answer(
            f"✅ <b>ꜱʜᴏʀᴛᴇɴᴇʀ ᴄᴀʟʟ ꜱᴜᴄᴄᴇᴇᴅᴇᴅ!</b>\n\n"
            f"🌐 <b>ᴅᴏᴍᴀɪɴ ᴜꜱᴇᴅ:</b> <code>{domain}</code>\n"
            f"🔗 <b>ɢᴇɴᴇʀᴀᴛᴇᴅ ʟɪɴᴋ:</b> {result}\n\n"
            f"<i>Open this link yourself and complete it once — if your wallet balance still doesn't move, "
            f"the issue is on the shortener platform's side (wrong account linked to this token), not the bot.</i>"
        )


# --- Admin: /setapitoken ---
@router.message(Command("setapitoken"))
async def set_api_token_handler(message: Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        await message.answer("❌ <i>Owner-only command — shortener API credentials.</i>")
        return
    token = (command.args or "").strip()
    if not token:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/setapitoken [your_arolinks_api_token]</code>")
        return
    await database.set_setting("arolinks_api_token", token)
    await message.answer("✅ <b>ᴀʀᴏʟɪɴᴋꜱ ᴀᴘɪ ᴛᴏᴋᴇɴ ꜱᴀᴠᴇᴅ!</b>")


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
        f"🌐 <b>ꜱʜᴏʀᴛᴇɴᴇʀ ᴅᴏᴍᴀɪɴ:</b> <code>{short_domain}</code>\n"
        f"🔑 <b>ᴀᴘɪ ᴛᴏᴋᴇɴ:</b> <code>{'YES ✅' if has_api_token else 'NO ❌'}</code>\n"
        f"🔗 <b>ᴘʀᴏᴛᴇᴄᴛɪᴏɴ ᴍᴏᴅᴇ:</b> <code>{link_mode.upper()}</code>",
        reply_markup=kb
    )


# --- Admin: /users ---
@router.message(Command("users"))
async def users_count_handler(message: Message):
    if not await is_moderator_or_above(message.from_user.id):
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
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/ban [user_id or @username]</code>")
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
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/unban [user_id or @username]</code>")
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
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/setbutton Button Text | https://link.com</code>")
        return
    text, url = map(str.strip, args.split("|", 1))
    if not text or not url:
        await message.answer("⚠️ Both button text and URL are required.")
        return
    await database.set_custom_button(text, url)
    await message.answer(
        f"✅ <b>ᴄᴜꜱᴛᴏᴍ ʙᴜᴛᴛᴏɴ ꜱᴇᴛ!</b>\n"
        f"📝 Text: <code>{text}</code>\n"
        f"🔗 URL: {url}"
    )


# --- Admin: /broadcast — with confirmation, live progress, cancel, and
# real FloodWait handling instead of silently swallowing every error. ---
BROADCAST_PENDING: dict[int, str] = {}   # admin_id -> text awaiting confirmation
BROADCAST_STOP_FLAGS: dict[int, bool] = {}  # admin_id -> True means "stop now"


@router.message(Command("broadcast"))
async def broadcast_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/broadcast [message text]</code>")
        return

    users = await database.broadcast_user_ids()
    BROADCAST_PENDING[message.from_user.id] = text
    preview = text if len(text) <= 500 else text[:497] + "..."
    await message.answer(
        "📢 <b>ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴘʀᴇᴠɪᴇᴡ</b>\n\n"
        f"{preview}\n\n"
        f"👥 <b>ʀᴇᴄɪᴘɪᴇɴᴛꜱ:</b> <code>{len(users)}</code> users\n\n"
        "<i>This will send the message above to every user. Confirm to proceed.</i>",
        reply_markup=broadcast_confirm_keyboard(message.from_user.id),
    )


@router.callback_query(F.data.startswith("bcast:"))
async def broadcast_callback_handler(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Unauthorized.", show_alert=True)
        return

    _, action, admin_id_str = cb.data.split(":")
    admin_id = int(admin_id_str)
    if cb.from_user.id != admin_id:
        await cb.answer("❌ Only the admin who started this can control it.", show_alert=True)
        return

    if action == "abort":
        BROADCAST_PENDING.pop(admin_id, None)
        await cb.message.edit_text("❌ <b>ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴄᴀɴᴄᴇʟʟᴇᴅ.</b> Nothing was sent.")
        await cb.answer()
        return

    if action == "stop":
        BROADCAST_STOP_FLAGS[admin_id] = True
        await cb.answer("Stopping after the current batch...")
        return

    if action == "go":
        text = BROADCAST_PENDING.pop(admin_id, None)
        if not text:
            await cb.answer("⚠️ This broadcast already expired — run /broadcast again.", show_alert=True)
            return
        await cb.answer()
        BROADCAST_STOP_FLAGS[admin_id] = False
        asyncio.create_task(_run_broadcast(admin_id, cb.message.chat.id, text))


async def _run_broadcast(admin_id: int, progress_chat_id: int, text: str) -> None:
    """Runs as a background task so it never blocks the event loop.
    Honors Telegram's FloodWait by sleeping the exact retry_after duration
    Telegram requests, instead of dropping/losing the send."""
    users = await database.broadcast_user_ids()
    total = len(users)
    success = 0
    failed = 0
    stopped_early = False

    status = await bot.send_message(
        progress_chat_id,
        f"📢 <b>ʙʀᴏᴀᴅᴄᴀꜱᴛɪɴɢ...</b>\n\n<code>0/{total}</code> sent",
        reply_markup=broadcast_progress_keyboard(admin_id),
    )

    last_edit = time.monotonic()
    for i, user_id in enumerate(users, start=1):
        if BROADCAST_STOP_FLAGS.get(admin_id):
            stopped_early = True
            break
        try:
            await bot.send_message(chat_id=user_id, text=text)
            success += 1
        except TelegramRetryAfter as flood:
            await asyncio.sleep(flood.retry_after + 0.5)
            try:
                await bot.send_message(chat_id=user_id, text=text)
                success += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            failed += 1  # user blocked the bot — expected, not an error
        except Exception as e:
            LOG.warning(f"Broadcast send failed for {user_id}: {e}")
            failed += 1

        await asyncio.sleep(0.05)  # gentle pacing to avoid tripping global flood limits

        if time.monotonic() - last_edit > 2.0 or i == total:
            last_edit = time.monotonic()
            try:
                await status.edit_text(
                    f"📢 <b>ʙʀᴏᴀᴅᴄᴀꜱᴛɪɴɢ...</b>\n\n<code>{i}/{total}</code> sent "
                    f"(<code>{success}</code> ok, <code>{failed}</code> failed)",
                    reply_markup=broadcast_progress_keyboard(admin_id),
                )
            except TelegramAPIError:
                pass

    BROADCAST_STOP_FLAGS.pop(admin_id, None)
    outcome = "🛑 <b>ʙʀᴏᴀᴅᴄᴀꜱᴛ ꜱᴛᴏᴘᴘᴇᴅ ʙʏ ᴀᴅᴍɪɴ.</b>" if stopped_early else "✅ <b>ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴄᴏᴍᴘʟᴇᴛᴇ!</b>"
    try:
        await status.edit_text(
            f"{outcome}\n\n<code>{success}/{total}</code> delivered, "
            f"<code>{failed}</code> failed."
        )
    except TelegramAPIError:
        await bot.send_message(
            progress_chat_id,
            f"{outcome} <code>{success}/{total}</code> delivered, <code>{failed}</code> failed."
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
                f"🌐 <b>ꜱʜᴏʀᴛᴇɴᴇʀ ᴅᴏᴍᴀɪɴ:</b> <code>{short_domain}</code>\n"
                f"🔑 <b>ᴀᴘɪ ᴛᴏᴋᴇɴ:</b> <code>{'YES ✅' if has_api_token else 'NO ❌'}</code>\n"
                f"🔗 <b>ᴘʀᴏᴛᴇᴄᴛɪᴏɴ ᴍᴏᴅᴇ:</b> <code>{link_mode.upper()}</code>"
            ),
            reply_markup=kb
        )
    except Exception as e:
        LOG.warning(f"Settings edit_text failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# NEW FEATURES — Favorites, Download History, Smart Search, Categories,
# Advanced Admin Dashboard, Backup/Restore.
#
# None of these touch: storage channel logic, file delivery/copy_message
# logic, the shortener call, token creation/claiming, or strike/ban logic.
# They only read existing posts/files and write to the new favorites /
# download_history tables added in database.py.
# ══════════════════════════════════════════════════════════════════════

@router.message(Command("fav"))
async def fav_handler(message: Message, command: CommandObject):
    if not message.from_user:
        return
    code = (command.args or "").strip()
    if not code:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/fav CODE</code>\n<i>CODE is the part after ?start=file_ or ?start=get_ in a link you've received.</i>")
        return
    post = await database.get_post(code)
    if not post:
        await message.answer("❌ <i>No such link found — check the code and try again.</i>")
        return
    added = await database.add_favorite(message.from_user.id, code)
    await message.answer("⭐ <b>ᴀᴅᴅᴇᴅ ᴛᴏ ꜰᴀᴠᴏʀɪᴛᴇꜱ!</b>" if added else "ℹ️ <i>Already in your favorites.</i>")


@router.message(Command("unfav"))
async def unfav_handler(message: Message, command: CommandObject):
    if not message.from_user:
        return
    code = (command.args or "").strip()
    if not code:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/unfav CODE</code>")
        return
    removed = await database.remove_favorite(message.from_user.id, code)
    await message.answer("🗑 <b>ʀᴇᴍᴏᴠᴇᴅ ꜰʀᴏᴍ ꜰᴀᴠᴏʀɪᴛᴇꜱ.</b>" if removed else "ℹ️ <i>That wasn't in your favorites.</i>")


@router.message(Command("favorites"))
async def favorites_handler(message: Message):
    if not message.from_user:
        return
    favs = await database.list_favorites(message.from_user.id, limit=20)
    if not favs:
        await message.answer(
            "⭐ <b>ꜰᴀᴠᴏʀɪᴛᴇꜱ</b>\n\n<i>You haven't favorited anything yet.</i>\n"
            "<i>Use</i> <code>/fav CODE</code> <i>on a link's code to save it here.</i>"
        )
        return
    lines = ["⭐ <b>ʏᴏᴜʀ ꜰᴀᴠᴏʀɪᴛᴇꜱ</b>\n"]
    for f in favs:
        link = f"https://t.me/{config.bot_username}?start=file_{f['post_code']}"
        lines.append(f"• <code>{f['post_code']}</code> — {link}")
    await message.answer("\n".join(lines))


@router.message(Command("history"))
async def history_handler(message: Message):
    if not message.from_user:
        return
    rows = await database.get_download_history(message.from_user.id, limit=20)
    if not rows:
        await message.answer("📥 <b>ᴅᴏᴡɴʟᴏᴀᴅ ʜɪꜱᴛᴏʀʏ</b>\n\n<i>Nothing here yet — files you receive will show up in this list.</i>")
        return
    lines = ["📥 <b>ʀᴇᴄᴇɴᴛ ᴅᴏᴡɴʟᴏᴀᴅꜱ</b>\n"]
    for r in rows:
        when = datetime.fromtimestamp(int(r["created_at"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        label = r["file_name"] or "File"
        lines.append(f"• <b>{label}</b> — <code>{r['post_code']}</code> <i>({when})</i>")
    await message.answer("\n".join(lines))


@router.message(Command("search"))
async def search_handler(message: Message, command: CommandObject):
    """Moderator-or-above: searching raw storage filenames is a lookup tool
    for staff, not a public file browser — keeping it role-gated preserves
    the existing model where files are only ever reachable through an
    issued post link."""
    if not message.from_user or not await is_moderator_or_above(message.from_user.id):
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/search keyword</code>")
        return
    results = await database.search_files(query, limit=15)
    if not results:
        await message.answer(f"🔎 <i>No stored files matched</i> <code>{query}</code>.")
        return
    lines = [f"🔎 <b>ꜱᴇᴀʀᴄʜ ʀᴇꜱᴜʟᴛꜱ ꜰᴏʀ</b> <code>{query}</code>\n"]
    for r in results:
        lines.append(f"• <code>{r['original_name']}</code> — <i>{r['tag'] or 'untagged'}</i> (msg #{r['storage_message_id']})")
    await message.answer("\n".join(lines))


@router.message(Command("categories"))
async def categories_handler(message: Message):
    if not message.from_user or not await is_moderator_or_above(message.from_user.id):
        return
    cats = await database.get_categories()
    if not cats:
        await message.answer("📂 <i>No tagged files yet.</i>")
        return
    lines = ["📂 <b>ꜰɪʟᴇ ᴄᴀᴛᴇɢᴏʀɪᴇꜱ</b>\n"]
    for tag, count in cats:
        lines.append(f"• <b>{tag}</b> — <code>{count}</code> file{'s' if count != 1 else ''}")
    await message.answer("\n".join(lines))


@router.message(Command("dashboard"))
async def dashboard_handler(message: Message):
    if not message.from_user or not await is_moderator_or_above(message.from_user.id):
        return
    s = await database.get_advanced_stats()
    top = "\n".join(
        f"   {i+1}. <code>{code}</code> — {cnt} downloads"
        for i, (code, cnt) in enumerate(s["most_downloaded"])
    ) or "   <i>No downloads recorded yet.</i>"
    await message.answer(
        "📊 <b>ᴀᴅᴍɪɴ ᴅᴀꜱʜʙᴏᴀʀᴅ</b>\n\n"
        f"👥 <b>ᴜꜱᴇʀꜱ:</b> <code>{s['total_users']}</code> total · "
        f"<code>{s['today_users']}</code> today · <code>{s['yesterday_users']}</code> yesterday\n"
        f"📈 <b>ɢʀᴏᴡᴛʜ:</b> <code>{s['week_users']}</code> this week · <code>{s['month_users']}</code> this month\n"
        f"🟢 <b>Online (last 15m):</b> <code>{s['online_users']}</code>\n"
        f"🚫 <b>ʙᴀɴɴᴇᴅ:</b> <code>{s['total_bans']}</code>\n\n"
        f"📂 <b>ꜰɪʟᴇꜱ:</b> <code>{s['total_files']}</code> · <b>ʟɪɴᴋꜱ:</b> <code>{s['total_posts']}</code>\n"
        f"📥 <b>ᴅᴏᴡɴʟᴏᴀᴅꜱ:</b> <code>{s['total_downloads']}</code>\n"
        f"✅ <b>ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴꜱ:</b> <code>{s['total_verifications']}</code>\n\n"
        f"🏆 <b>ᴍᴏꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ:</b>\n{top}\n\n"
        f"🗄 <b>ʙᴀᴄᴋᴇɴᴅ:</b> <code>{config.db_backend.upper()}</code>"
    )


@router.message(Command("backup"))
async def backup_handler(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    import json
    from aiogram.types import BufferedInputFile
    backup = await database.export_backup()
    payload = json.dumps(backup, indent=2, default=str).encode("utf-8")
    filename = f"backup_{int(time.time())}.json"
    table_count = len(backup.get("tables", {}))
    await message.answer_document(
        BufferedInputFile(payload, filename=filename),
        caption=(
            "🗄 <b>ʙᴀᴄᴋᴜᴘ ᴄᴏᴍᴘʟᴇᴛᴇ</b>\n\n"
            f"📋 <b>Tables/collections covered:</b> <code>{table_count}</code>\n"
            f"🗃 <b>ʙᴀᴄᴋᴇɴᴅ:</b> <code>{backup.get('backend', config.db_backend)}</code>\n"
            f"🏷 <b>ꜱᴄʜᴇᴍᴀ ᴠᴇʀꜱɪᴏɴ:</b> <code>{backup.get('schema_version')}</code>\n\n"
            "<i>This is a full snapshot of every persistent table/collection. "
            "Restoring it (with /restore) only ever ADDS missing rows — it can "
            "never overwrite or delete anything already in your database.</i>"
        ),
    )


@router.message(Command("restore"))
async def restore_handler(message: Message):
    if not message.from_user or not is_owner(message.from_user.id):
        if message.from_user:
            await message.answer("❌ <i>Owner-only command — database restore.</i>")
        return
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.answer("⚠️ <i>Reply to a backup</i> <code>.json</code> <i>file with</i> <code>/restore</code>.")
        return
    import json
    from aiogram.types import BufferedInputFile
    doc = message.reply_to_message.document
    try:
        file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(file.file_path)
        backup = json.loads(buf.read())
    except Exception as e:
        await message.answer(f"❌ <b>ᴄᴏᴜʟᴅ ɴᴏᴛ ʀᴇᴀᴅ ʙᴀᴄᴋᴜᴘ ꜰɪʟᴇ:</b> <code>{e}</code>")
        return

    # 1) Validate the backup BEFORE touching the live database.
    validation_error = database._validate_backup(backup) if hasattr(database, "_validate_backup") else None
    if validation_error:
        await message.answer(f"❌ <b>ʙᴀᴄᴋᴜᴘ ꜰᴀɪʟᴇᴅ ᴠᴀʟɪᴅᴀᴛɪᴏɴ:</b> <code>{validation_error}</code>\n\n<i>Nothing was touched.</i>")
        return

    # 2) Automatic safety backup of the CURRENT (pre-restore) state, sent
    # to this admin, before anything is written.
    status = await message.answer("🛡 <i>Creating automatic safety backup before restoring...</i>")
    try:
        safety_backup = await database.export_backup()
        safety_payload = json.dumps(safety_backup, indent=2, default=str).encode("utf-8")
        await message.answer_document(
            BufferedInputFile(safety_payload, filename=f"pre_restore_safety_{int(time.time())}.json"),
            caption="🛡 <b>ᴀᴜᴛᴏᴍᴀᴛɪᴄ ꜱᴀꜰᴇᴛʏ ʙᴀᴄᴋᴜᴘ</b> (taken right before this restore ran).",
        )
    except Exception as e:
        await status.edit_text(f"❌ <b>Could not create the automatic safety backup — restore aborted:</b> <code>{e}</code>")
        return

    # 3) Restore (insert-only — can never overwrite/delete existing rows).
    try:
        report = await database.restore_backup(backup)
    except Exception as e:
        await status.edit_text(
            f"❌ <b>ʀᴇꜱᴛᴏʀᴇ ꜰᴀɪʟᴇᴅ ᴀɴᴅ ᴡᴀꜱ ʀᴏʟʟᴇᴅ ʙᴀᴄᴋ:</b> <code>{e}</code>\n\n"
            "<i>Nothing was permanently written — your database is unchanged.</i>"
        )
        return

    # 4) Verify row counts / relationships after restoring.
    verify_ok = True
    verify_note = ""
    if hasattr(database, "verify_backup_integrity"):
        try:
            integrity = await database.verify_backup_integrity()
            verify_ok = integrity.get("healthy", True)
            if not verify_ok:
                verify_note = f"\n\n⚠️ <b>ɪɴᴛᴇɢʀɪᴛʏ ᴄʜᴇᴄᴋ ꜰᴏᴜɴᴅ ᴏʀᴘʜᴀɴᴇᴅ ʀᴏᴡꜱ:</b> <code>{integrity['orphans']}</code>"
        except Exception as e:
            verify_note = f"\n\n⚠️ <i>Could not run the post-restore integrity check:</i> <code>{e}</code>"

    lines = ["✅ <b>ʀᴇꜱᴛᴏʀᴇ ᴄᴏᴍᴘʟᴇᴛᴇ</b> <i>(insert-only — nothing existing was touched)</i>\n"]
    for table, count in report.items():
        lines.append(f"• {table}: <code>{count}</code> new rows added")
    lines.append(verify_note)
    try:
        await status.edit_text("\n".join(lines))
    except Exception:
        await message.answer("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════
# ROLE MANAGEMENT (Owner/Admin/Moderator) + HEALTH MONITOR
# ══════════════════════════════════════════════════════════════════════

@router.message(Command("addmod"))
async def add_mod_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    target_id = await _resolve_target_user_id(arg) if arg else None
    if target_id is None:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/addmod [user_id or @username]</code>")
        return
    if is_admin(target_id):
        await message.answer("ℹ️ <i>That user is already an admin/owner — moderator tier is redundant for them.</i>")
        return
    added = await database.add_moderator(target_id)
    await message.answer(
        f"✅ <code>{target_id}</code> is now a moderator." if added
        else f"ℹ️ <code>{target_id}</code> was already a moderator."
    )


@router.message(Command("removemod"))
async def remove_mod_handler(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    target_id = await _resolve_target_user_id(arg) if arg else None
    if target_id is None:
        await message.answer("⚠️ <b>ᴜꜱᴀɢᴇ:</b> <code>/removemod [user_id or @username]</code>")
        return
    removed = await database.remove_moderator(target_id)
    await message.answer(
        f"✅ <code>{target_id}</code> removed from moderators." if removed
        else f"ℹ️ <code>{target_id}</code> wasn't a moderator."
    )


@router.message(Command("listmods"))
async def list_mods_handler(message: Message):
    if not await is_moderator_or_above(message.from_user.id):
        return
    mods = await database.get_moderator_ids()
    lines = ["🧰 <b>ꜱᴛᴀꜰꜰ ʀᴏʟᴇꜱ</b>\n"]
    lines.append(f"👑 <b>Owner(s):</b> " + ", ".join(f"<code>{u}</code>" for u in sorted(config.owner_ids)))
    admin_only = config.admin_ids - config.owner_ids
    if admin_only:
        lines.append(f"🛡 <b>Admin(s):</b> " + ", ".join(f"<code>{u}</code>" for u in sorted(admin_only)))
    if mods:
        lines.append(f"🧰 <b>Moderator(s):</b> " + ", ".join(f"<code>{u}</code>" for u in sorted(mods)))
    else:
        lines.append("🧰 <b>Moderator(s):</b> <i>none</i>")
    await message.answer("\n".join(lines))


@router.message(Command("role"))
async def role_handler(message: Message):
    if not message.from_user:
        return
    label = await get_role_label(message.from_user.id)
    await message.answer(f"🪪 <b>ʏᴏᴜʀ ʀᴏʟᴇ:</b> {label}")


@router.message(Command("health"))
async def health_handler(message: Message):
    if not message.from_user or not await is_moderator_or_above(message.from_user.id):
        return

    checks: list[tuple[str, bool, str]] = []

    # Database
    try:
        await database.get_setting("link_mode", "direct")
        checks.append(("Database", True, config.db_backend.upper()))
    except Exception as e:
        checks.append(("Database", False, str(e)[:60]))

    # Storage channel reachability
    try:
        await bot.get_chat(config.storage_channel_id)
        checks.append(("Storage Channel", True, str(config.storage_channel_id)))
    except Exception as e:
        checks.append(("Storage Channel", False, str(e)[:60]))

    # Log channel (optional — not a failure if unset)
    log_id = await get_log_channel_id()
    if log_id:
        try:
            await bot.get_chat(log_id)
            checks.append(("Log Channel", True, str(log_id)))
        except Exception as e:
            checks.append(("Log Channel", False, str(e)[:60]))
    else:
        checks.append(("Log Channel", True, "not configured"))

    # Shortener config presence (a real network probe would consume the
    # verification API's rate limit; we check credentials are present,
    # which is what actually gates whether links get shortened at all)
    domain = await database.get_setting("shortener_url", "")
    api_token = await database.get_setting("arolinks_api_token", "")
    checks.append(("Shortener Config", bool(domain and api_token), domain or "not configured"))

    lines = ["🩺 <b>ʜᴇᴀʟᴛʜ ᴍᴏɴɪᴛᴏʀ</b>\n"]
    all_ok = True
    for name, ok, detail in checks:
        icon = "✅" if ok else "❌"
        if not ok:
            all_ok = False
        lines.append(f"{icon} <b>{name}:</b> <code>{detail}</code>")
    lines.append(f"\n🗄 <b>ʙᴀᴄᴋᴇɴᴅ:</b> <code>{config.db_backend.upper()}</code>")
    lines.append("\n" + ("🟩 <b>ᴀʟʟ ꜱʏꜱᴛᴇᴍꜱ ᴏᴘᴇʀᴀᴛɪᴏɴᴀʟ.</b>" if all_ok else "🟧 <b>ᴏɴᴇ ᴏʀ ᴍᴏʀᴇ ᴄʜᴇᴄᴋꜱ ɴᴇᴇᴅ ᴀᴛᴛᴇɴᴛɪᴏɴ.</b>"))
    await message.answer("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════
# ADVANCED FORCE-SUBSCRIBE MANAGER — real Telegram admin UI on top of the
# existing fsub_channels storage / get_unjoined_fsub_channels membership
# check. /addfsub /removefsub /listfsub /clearfsub above still work
# unchanged — this panel is just a UI layer over the same data.
# ══════════════════════════════════════════════════════════════════════
@router.message(Command("fsub"))
async def fsub_panel_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    channels = await database.get_fsub_channels()
    await message.answer(
        "🔒 <b>ꜰᴏʀᴄᴇ-ꜱᴜʙꜱᴄʀɪʙᴇ ᴍᴀɴᴀɢᴇʀ</b>\n\n"
        f"📊 <b>ᴄᴏɴꜰɪɢᴜʀᴇᴅ:</b> <code>{len(channels)}</code>\n"
        "<i>Tap a channel to enable/disable it. Only ✅ enabled channels are "
        "checked before a user can access a link.</i>",
        reply_markup=fsub_panel_keyboard(channels),
    )


async def _render_fsub_panel(cb: CallbackQuery) -> None:
    channels = await database.get_fsub_channels()
    try:
        await cb.message.edit_text(
            "🔒 <b>ꜰᴏʀᴄᴇ-ꜱᴜʙꜱᴄʀɪʙᴇ ᴍᴀɴᴀɢᴇʀ</b>\n\n"
            f"📊 <b>ᴄᴏɴꜰɪɢᴜʀᴇᴅ:</b> <code>{len(channels)}</code>\n"
            "<i>Tap a channel to enable/disable it. Only ✅ enabled channels are "
            "checked before a user can access a link.</i>",
            reply_markup=fsub_panel_keyboard(channels),
        )
    except Exception as e:
        LOG.warning(f"fsub panel refresh failed: {e}")


@router.callback_query(F.data.startswith("fsub:"))
async def fsub_callback_handler(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Unauthorized.", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]

    if action == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass
        return

    if action == "panel":
        await _render_fsub_panel(cb)
        await cb.answer()
        return

    if action == "add":
        PENDING_ADMIN_INPUT[cb.from_user.id] = {"action": "fsub_add"}
        await cb.message.answer(
            "➕ <b>Add Force-Subscribe Channel/Group</b>\n\n"
            "Send it now as: <code>@channel invite_link display_name</code>\n"
            "<i>invite_link is required for private chats (numeric IDs) — use "
            "<code>-</code> to skip it for public @channels.</i>\n"
            "<i>display_name is optional.</i>"
        )
        await cb.answer()
        return

    if action == "toggle":
        idx = int(parts[2])
        await database.toggle_fsub_channel(idx)
        await _render_fsub_panel(cb)
        await cb.answer("Toggled.")
        return

    if action == "up":
        idx = int(parts[2])
        await database.reorder_fsub_channel(idx, -1)
        await _render_fsub_panel(cb)
        await cb.answer("Moved up.")
        return

    if action == "down":
        idx = int(parts[2])
        await database.reorder_fsub_channel(idx, 1)
        await _render_fsub_panel(cb)
        await cb.answer("Moved down.")
        return

    if action == "edit":
        idx = int(parts[2])
        PENDING_ADMIN_INPUT[cb.from_user.id] = {"action": "fsub_edit", "index": idx}
        await cb.message.answer(
            "🏷 <b>Set label/folder</b>\n\nSend as: <code>folder | display name</code>\n"
            "<i>Either part can be left blank, e.g.</i> <code>Movies |</code>"
        )
        await cb.answer()
        return

    if action == "remove":
        idx = int(parts[2])
        channels = await database.get_fsub_channels()
        if not (0 <= idx < len(channels)):
            await cb.answer("That entry no longer exists.", show_alert=True)
            await _render_fsub_panel(cb)
            return
        label = channels[idx].get("name") or channels[idx].get("chat")
        try:
            await cb.message.edit_text(
                f"⚠️ <b>ʀᴇᴍᴏᴠᴇ ꜰᴏʀᴄᴇ-ꜱᴜʙꜱᴄʀɪʙᴇ ᴄʜᴀɴɴᴇʟ</b> <code>{label}</code><b>?</b>",
                reply_markup=fsub_remove_confirm_keyboard(idx),
            )
        except Exception as e:
            LOG.warning(f"fsub remove-confirm render failed: {e}")
        await cb.answer()
        return

    if action == "remove_confirm":
        idx = int(parts[2])
        removed = await database.remove_fsub_channel_at(idx)
        await _render_fsub_panel(cb)
        await cb.answer("Removed." if removed else "Already gone.")
        return

    await cb.answer()


async def _handle_pending_fsub_input(message: Message, pending: dict) -> bool:
    admin_id = message.from_user.id
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("⚠️ <i>Please send this as text.</i>")
        return True

    if pending["action"] == "fsub_add":
        args = text.split(maxsplit=2)
        chat_ref = args[0]
        link = args[1] if len(args) > 1 and args[1] != "-" else ""
        name = args[2] if len(args) > 2 else ""
        if not chat_ref.startswith("@") and not link:
            await message.answer(
                "⚠️ <i>Private chat IDs need an invite link. Try again, or</i> "
                "<code>/fsub</code> <i>to cancel.</i>"
            )
            return True
        channels = await database.get_fsub_channels()
        channels = [c for c in channels if c.get("chat") != chat_ref]
        channels.append({"chat": chat_ref, "link": link, "name": name, "enabled": True, "folder": ""})
        await database.set_fsub_channels(channels)
        del PENDING_ADMIN_INPUT[admin_id]
        await message.answer(
            f"✅ <b>ᴀᴅᴅᴇᴅ ꜰᴏʀᴄᴇ-ꜱᴜʙꜱᴄʀɪʙᴇ ᴄʜᴀɴɴᴇʟ:</b> <code>{chat_ref}</code>",
            reply_markup=fsub_panel_keyboard(channels),
        )
        return True

    if pending["action"] == "fsub_edit":
        idx = pending["index"]
        if "|" in text:
            folder, name = (p.strip() for p in text.split("|", 1))
        else:
            folder, name = text.strip(), ""
        ok = await database.update_fsub_channel(idx, folder=folder, **({"name": name} if name else {}))
        del PENDING_ADMIN_INPUT[admin_id]
        channels = await database.get_fsub_channels()
        await message.answer(
            "✅ <b>ʟᴀʙᴇʟ ᴜᴘᴅᴀᴛᴇᴅ.</b>" if ok else "⚠️ <i>That entry no longer exists.</i>",
            reply_markup=fsub_panel_keyboard(channels),
        )
        return True

    return False


# ══════════════════════════════════════════════════════════════════════
# BUTTON MANAGER — real admin UI over button_configs, rendered on actual
# bot messages (welcome + file delivery) via render_configured_buttons.
# Only real Telegram inline keyboard buttons (url / callback_data) —
# there is no colored-button API in Telegram to fake.
# ══════════════════════════════════════════════════════════════════════
@router.message(Command("buttons"))
async def button_manager_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    buttons = await database.list_buttons()
    await message.answer(
        "🔘 <b>ʙᴜᴛᴛᴏɴ ᴍᴀɴᴀɢᴇʀ</b>\n\n"
        f"📊 <b>ᴄᴏɴꜰɪɢᴜʀᴇᴅ:</b> <code>{len(buttons)}</code>\n"
        "<i>These buttons are attached to the welcome message and to delivered "
        "files. Tap a button below to enable/disable it.</i>",
        reply_markup=button_manager_keyboard(buttons),
    )


async def _render_button_panel(cb: CallbackQuery) -> None:
    buttons = await database.list_buttons()
    try:
        await cb.message.edit_text(
            "🔘 <b>ʙᴜᴛᴛᴏɴ ᴍᴀɴᴀɢᴇʀ</b>\n\n"
            f"📊 <b>ᴄᴏɴꜰɪɢᴜʀᴇᴅ:</b> <code>{len(buttons)}</code>\n"
            "<i>These buttons are attached to the welcome message and to delivered "
            "files. Tap a button below to enable/disable it.</i>",
            reply_markup=button_manager_keyboard(buttons),
        )
    except Exception as e:
        LOG.warning(f"button panel refresh failed: {e}")


@router.callback_query(F.data.startswith("btn:"))
async def button_manager_callback_handler(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Unauthorized.", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]

    if action == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass
        return

    if action == "panel":
        await _render_button_panel(cb)
        await cb.answer()
        return

    if action == "add":
        PENDING_ADMIN_INPUT[cb.from_user.id] = {"action": "button_add_text"}
        await cb.message.answer("✏️ <b>Send the button's text/label:</b>")
        await cb.answer()
        return

    if action == "edit":
        btn_id = int(parts[2])
        PENDING_ADMIN_INPUT[cb.from_user.id] = {"action": "button_edit_text", "id": btn_id}
        await cb.message.answer("✏️ <b>Send the new button text/label:</b>")
        await cb.answer()
        return

    if action == "toggle":
        btn_id = int(parts[2])
        await database.toggle_button(btn_id)
        await _render_button_panel(cb)
        await cb.answer("Toggled.")
        return

    if action == "up":
        btn_id = int(parts[2])
        await database.reorder_button(btn_id, -1)
        await _render_button_panel(cb)
        await cb.answer("Moved up.")
        return

    if action == "down":
        btn_id = int(parts[2])
        await database.reorder_button(btn_id, 1)
        await _render_button_panel(cb)
        await cb.answer("Moved down.")
        return

    if action == "delete":
        btn_id = int(parts[2])
        buttons = {b["id"]: b for b in await database.list_buttons()}
        btn = buttons.get(btn_id)
        if not btn:
            await cb.answer("Already gone.", show_alert=True)
            await _render_button_panel(cb)
            return
        try:
            await cb.message.edit_text(
                f"⚠️ <b>ᴅᴇʟᴇᴛᴇ ʙᴜᴛᴛᴏɴ</b> <code>{btn['text']}</code><b>?</b>",
                reply_markup=button_delete_confirm_keyboard(btn_id),
            )
        except Exception as e:
            LOG.warning(f"button delete-confirm render failed: {e}")
        await cb.answer()
        return

    if action == "delete_confirm":
        btn_id = int(parts[2])
        deleted = await database.delete_button(btn_id)
        await _render_button_panel(cb)
        await cb.answer("Deleted." if deleted else "Already gone.")
        return

    if action == "preview":
        buttons = await database.list_buttons()
        kb = render_configured_buttons(buttons)
        if kb is None:
            await cb.answer("No enabled buttons to preview.", show_alert=True)
            return
        await cb.message.answer("👁 <b>Preview — how these buttons appear on a message:</b>", reply_markup=kb)
        await cb.answer()
        return

    await cb.answer()


# Real handler for every admin-configured CALLBACK-type button rendered on
# an actual bot message (url-type buttons need no handler — Telegram
# opens the URL directly).
@router.callback_query(F.data.startswith("cfgbtn:"))
async def configured_callback_button_handler(cb: CallbackQuery):
    btn_id = int(cb.data.split(":")[1])
    buttons = {b["id"]: b for b in await database.list_buttons()}
    btn = buttons.get(btn_id)
    if not btn or not btn.get("enabled", 1):
        await cb.answer("This button is no longer available.", show_alert=True)
        return
    action_text = (btn.get("callback") or "").strip()
    await cb.answer(action_text or f"{btn['text']}", show_alert=True)


async def _handle_pending_button_input(message: Message, pending: dict) -> bool:
    admin_id = message.from_user.id
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("⚠️ <i>Please send this as text.</i>")
        return True

    if pending["action"] == "button_add_text":
        PENDING_ADMIN_INPUT[admin_id] = {"action": "button_add_target", "text": text}
        await message.answer(
            "🔗 <b>ɴᴏᴡ ꜱᴇɴᴅ ᴛʜᴇ ᴅᴇꜱᴛɪɴᴀᴛɪᴏɴ:</b>\n\n"
            "A URL (<code>https://...</code>) for a link button, or "
            "<code>cb:some text</code> for a callback button that shows an alert."
        )
        return True

    if pending["action"] == "button_add_target":
        btn_text = pending["text"]
        if text.startswith("cb:"):
            btn_id = await database.add_button(btn_text, url="", callback=text[3:].strip() or btn_text)
        elif text.startswith("http://") or text.startswith("https://"):
            btn_id = await database.add_button(btn_text, url=text, callback="")
        else:
            await message.answer(
                "❌ <i>That doesn't look like a URL or</i> <code>cb:...</code><i>. Try again.</i>"
            )
            return True
        del PENDING_ADMIN_INPUT[admin_id]
        buttons = await database.list_buttons()
        await message.answer(
            f"✅ <b>ʙᴜᴛᴛᴏɴ ᴀᴅᴅᴇᴅ</b> (<code>#{btn_id}</code>).",
            reply_markup=button_manager_keyboard(buttons),
        )
        return True

    if pending["action"] == "button_edit_text":
        ok = await database.update_button(pending["id"], text=text)
        del PENDING_ADMIN_INPUT[admin_id]
        buttons = await database.list_buttons()
        await message.answer(
            "✅ <b>ʙᴜᴛᴛᴏɴ ᴜᴘᴅᴀᴛᴇᴅ.</b>" if ok else "⚠️ <i>That button no longer exists.</i>",
            reply_markup=button_manager_keyboard(buttons),
        )
        return True

    return False


# ══════════════════════════════════════════════════════════════════════
# WELCOME CUSTOMIZATION — real admin UI over get_welcome_config /
# update_welcome_config, which is what execution_welcome() (the actual
# /start handler) reads. No setting here is ever stored without also
# being used by that flow.
# ══════════════════════════════════════════════════════════════════════
def _welcome_panel_text(cfg: dict) -> str:
    return (
        "🎨 <b>ᴡᴇʟᴄᴏᴍᴇ ᴄᴜꜱᴛᴏᴍɪᴢᴀᴛɪᴏɴ</b>\n\n"
        f"📝 <b>ᴛᴇxᴛ:</b> <code>{'custom' if cfg.get('text') else 'default'}</code>\n"
        f"🖼 <b>ᴘʜᴏᴛᴏ:</b> <code>{'set' if cfg.get('photo_id') else 'none'}</code>\n"
        f"🎟 <b>ꜱᴛɪᴄᴋᴇʀ:</b> <code>{'set' if cfg.get('sticker_id') else 'none'}</code>\n"
        f"🔌 <b>ꜱᴛᴀᴛᴜꜱ:</b> <code>{'ENABLED' if cfg.get('enabled') else 'DISABLED'}</code>"
    )


@router.message(Command("welcome"))
async def welcome_panel_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    cfg = await database.get_welcome_config()
    await message.answer(_welcome_panel_text(cfg), reply_markup=welcome_panel_keyboard(cfg))


async def _render_welcome_panel(cb: CallbackQuery) -> None:
    cfg = await database.get_welcome_config()
    try:
        await cb.message.edit_text(_welcome_panel_text(cfg), reply_markup=welcome_panel_keyboard(cfg))
    except Exception as e:
        LOG.warning(f"welcome panel refresh failed: {e}")


@router.callback_query(F.data.startswith("welcome:"))
async def welcome_callback_handler(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Unauthorized.", show_alert=True)
        return
    action = cb.data.split(":", 1)[1]

    if action == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass
        return

    if action == "panel":
        await _render_welcome_panel(cb)
        await cb.answer()
        return

    if action == "edit_text":
        PENDING_ADMIN_INPUT[cb.from_user.id] = {"action": "welcome_edit_text"}
        await cb.message.answer(
            "✏️ <b>ꜱᴇɴᴅ ᴛʜᴇ ɴᴇᴡ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ</b> (formatting carries over).\n\n"
            "<i>Placeholders:</i> <code>{mention}</code>, <code>{name}</code>"
        )
        await cb.answer()
        return

    if action == "set_photo":
        PENDING_ADMIN_INPUT[cb.from_user.id] = {"action": "welcome_set_photo"}
        await cb.message.answer("🖼 <b>ꜱᴇɴᴅ ᴛʜᴇ ᴘʜᴏᴛᴏ ɴᴏᴡ.</b>")
        await cb.answer()
        return

    if action == "set_sticker":
        PENDING_ADMIN_INPUT[cb.from_user.id] = {"action": "welcome_set_sticker"}
        await cb.message.answer("🎟 <b>ꜱᴇɴᴅ ᴛʜᴇ ꜱᴛɪᴄᴋᴇʀ ɴᴏᴡ.</b>")
        await cb.answer()
        return

    if action == "remove_photo":
        await database.update_welcome_config(photo_id="")
        await _render_welcome_panel(cb)
        await cb.answer("Photo removed.")
        return

    if action == "remove_sticker":
        await database.update_welcome_config(sticker_id="")
        await _render_welcome_panel(cb)
        await cb.answer("Sticker removed.")
        return

    if action == "toggle_anim":
        cfg = await database.get_welcome_config()
        await database.update_welcome_config(anim_enabled=not cfg.get("anim_enabled", True))
        await _render_welcome_panel(cb)
        await cb.answer("Text animation toggled.")
        return

    if action == "toggle_sticker_anim":
        cfg = await database.get_welcome_config()
        await database.update_welcome_config(sticker_anim_enabled=not cfg.get("sticker_anim_enabled", True))
        await _render_welcome_panel(cb)
        await cb.answer("Sticker animation toggled.")
        return

    if action == "cycle_speed":
        cfg = await database.get_welcome_config()
        order = ["slow", "normal", "fast"]
        current = cfg.get("anim_speed", "normal")
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else "normal"
        await database.update_welcome_config(anim_speed=nxt)
        await _render_welcome_panel(cb)
        await cb.answer(f"Speed: {nxt}")
        return

    if action == "toggle_spoiler":
        cfg = await database.get_welcome_config()
        await database.update_welcome_config(spoiler=not cfg.get("spoiler", True))
        await _render_welcome_panel(cb)
        await cb.answer("Spoiler toggled.")
        return

    if action == "toggle_enabled":
        cfg = await database.get_welcome_config()
        await database.update_welcome_config(enabled=not cfg.get("enabled", True))
        await _render_welcome_panel(cb)
        await cb.answer("Welcome message toggled.")
        return

    if action == "preview":
        await cb.answer("Sending preview...")
        await execution_welcome(cb.message, cb.from_user)
        return

    if action == "reset":
        try:
            await cb.message.edit_text(
                "⚠️ <b>ʀᴇꜱᴇᴛ ᴀʟʟ ᴡᴇʟᴄᴏᴍᴇ ᴄᴜꜱᴛᴏᴍɪᴢᴀᴛɪᴏɴ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ?</b>\n"
                "<i>This clears the custom text, photo, sticker and animation settings.</i>",
                reply_markup=welcome_reset_confirm_keyboard(),
            )
        except Exception as e:
            LOG.warning(f"welcome reset-confirm render failed: {e}")
        await cb.answer()
        return

    if action == "reset_confirm":
        await database.reset_welcome_config()
        await _render_welcome_panel(cb)
        await cb.answer("Reset to default.")
        return

    await cb.answer()


async def _handle_pending_welcome_input(message: Message, pending: dict) -> bool:
    admin_id = message.from_user.id
    action = pending["action"]

    if action == "welcome_edit_text":
        raw_html = message.html_text or ""
        if not raw_html.strip():
            await message.answer("⚠️ <i>Please send this as text.</i>")
            return True
        await database.update_welcome_config(text=raw_html.strip())
        del PENDING_ADMIN_INPUT[admin_id]
        cfg = await database.get_welcome_config()
        await message.answer("✅ <b>ᴡᴇʟᴄᴏᴍᴇ ᴛᴇxᴛ ᴜᴘᴅᴀᴛᴇᴅ!</b>", reply_markup=welcome_panel_keyboard(cfg))
        return True

    if action == "welcome_set_photo":
        if not message.photo:
            await message.answer("⚠️ <i>Please send that as a photo.</i>")
            return True
        await database.update_welcome_config(photo_id=message.photo[-1].file_id)
        del PENDING_ADMIN_INPUT[admin_id]
        cfg = await database.get_welcome_config()
        await message.answer("✅ <b>ᴡᴇʟᴄᴏᴍᴇ ᴘʜᴏᴛᴏ ᴜᴘᴅᴀᴛᴇᴅ!</b>", reply_markup=welcome_panel_keyboard(cfg))
        return True

    if action == "welcome_set_sticker":
        if not message.sticker:
            await message.answer("⚠️ <i>Please send that as a sticker.</i>")
            return True
        await database.update_welcome_config(sticker_id=message.sticker.file_id)
        del PENDING_ADMIN_INPUT[admin_id]
        cfg = await database.get_welcome_config()
        await message.answer("✅ <b>ᴡᴇʟᴄᴏᴍᴇ ꜱᴛɪᴄᴋᴇʀ ᴜᴘᴅᴀᴛᴇᴅ!</b>", reply_markup=welcome_panel_keyboard(cfg))
        return True

    return False


# Dispatches to whichever pending-input handler matches. Returns True if
# the message was consumed (caller must stop processing it as an upload).
async def _consume_pending_admin_input(message: Message) -> bool:
    pending = PENDING_ADMIN_INPUT.get(message.from_user.id)
    if not pending:
        return False
    action = pending["action"]
    if action.startswith("fsub_"):
        return await _handle_pending_fsub_input(message, pending)
    if action.startswith("button_"):
        return await _handle_pending_button_input(message, pending)
    if action.startswith("welcome_"):
        return await _handle_pending_welcome_input(message, pending)
    return False


# ══════════════════════════════════════════════════════════════════════
# CUSTOM BATCH — First Message -> Last Message.
# /newbatch marks the very next admin message as batch_start (it's
# collected too, not just a marker), every message after that is
# collected in order, /finishbatch marks the most recently collected
# message as batch_end and generates a Genlink through the EXISTING
# create_post()/Storage Channel pipeline — same delivery system the
# normal /batch and single-file uploads already use.
# ══════════════════════════════════════════════════════════════════════
@router.message(Command("newbatch"))
async def newbatch_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    if await database.get_active_message_batch(message.from_user.id):
        await message.answer(
            "⚠️ <i>You already have a Custom Batch in progress. "
            "Use</i> <code>/finishbatch</code> <i>or</i> <code>/cancelnewbatch</code> <i>first.</i>"
        )
        return
    if message.from_user.id in BATCH_SESSIONS:
        await message.answer("⚠️ <i>Finish or cancel your active</i> <code>/batch</code> <i>session first.</i>")
        return
    batch_id = await database.start_message_batch(message.from_user.id, first_message_id=message.message_id)
    await message.answer(
        f"📦 <b>Custom Batch #{batch_id} started!</b>\n\n"
        "<blockquote>This message is the batch's FIRST message. Send all the files "
        "you want included — each one is stored in order as you send it.</blockquote>\n\n"
        "✅ <code>/finishbatch</code> <i>to finalize and generate the Genlink.</i>\n"
        "🚫 <code>/cancelnewbatch</code> <i>to abort.</i>\n\n"
        "<i>This survives a bot restart — you can resume sending files any time before finishing.</i>"
    )
    # The /newbatch command message itself IS the first message per the
    # spec — store it as the batch's first collected item too.
    name = _extract_storable_label(message)
    try:
        copied = await bot.copy_message(
            chat_id=int(config.storage_channel_id), from_chat_id=message.chat.id, message_id=message.message_id
        )
        file_row = await database.add_stored_file(
            storage_message_id=copied.message_id, original_name=name, tag="custom_batch"
        )
        await database.add_file_to_custom_batch(batch_id, int(file_row["id"]))
    except Exception as e:
        LOG.warning(f"Could not store /newbatch marker message as first item: {e}")


@router.message(Command("finishbatch"))
async def finishbatch_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    active = await database.get_active_message_batch(message.from_user.id)
    if not active:
        await message.answer("⚠️ <i>No active Custom Batch. Start one with</i> <code>/newbatch</code>.")
        return
    batch = await database.get_custom_batch(int(active["id"]))
    if not batch["files"]:
        await message.answer("⚠️ <i>No files were added to this batch yet.</i>")
        return
    await database.finish_message_batch(int(active["id"]), last_message_id=message.message_id)

    is_protected = (await database.get_setting("link_mode", "direct") == "shortener")
    file_ids = [int(f["id"]) for f in batch["files"]]
    post_row = await database.create_post(kind="batch", file_ids=file_ids, protected=is_protected)
    await database.set_batch_post_code(int(active["id"]), post_row["code"])
    share_url = f"https://t.me/{config.bot_username}?start=file_{post_row['code']}"
    await message.answer(
        f"📦 <b>Custom Batch #{active['id']} finalized!</b>\n\n"
        f"📊 <b>ꜰɪʟᴇꜱ:</b> <code>{len(file_ids)}</code>\n"
        f"🆔 <b>ꜰɪʀꜱᴛ ᴍᴇꜱꜱᴀɢᴇ ɪᴅ:</b> <code>{active['first_message_id']}</code>\n"
        f"🆔 <b>ʟᴀꜱᴛ ᴍᴇꜱꜱᴀɢᴇ ɪᴅ:</b> <code>{message.message_id}</code>\n"
        f"📥 <b>ʟɪɴᴋ:</b> <code>{share_url}</code>"
    )


@router.message(Command("cancelnewbatch"))
async def cancelnewbatch_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    active = await database.get_active_message_batch(message.from_user.id)
    if not active:
        await message.answer("⚠️ <i>No active Custom Batch.</i>")
        return
    await database.cancel_message_batch(int(active["id"]))
    await message.answer(f"✅ <b>Custom Batch #{active['id']} cancelled.</b>")


@router.message(Command("batches"))
async def custom_batches_panel_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    batches = await database.list_custom_batches()
    # Only ever list message-range batches here (status is set by
    # start_message_batch/finish_message_batch) — rows from the legacy
    # editor default to status='completed' with no first/last message id
    # and simply won't have been created by this flow.
    await message.answer(
        "📦 <b>ᴄᴜꜱᴛᴏᴍ ʙᴀᴛᴄʜᴇꜱ</b>\n\n"
        f"<i>Showing the {min(len(batches), 20)} most recent (of {len(batches)}).</i>",
        reply_markup=custom_batch_list_keyboard(batches[:20]),
    )


def _batch_detail_text(batch: dict) -> str:
    lines = [
        f"📦 <b>Custom Batch #{batch['id']}</b>",
        f"📊 <b>ꜱᴛᴀᴛᴜꜱ:</b> <code>{batch.get('status')}</code>",
        f"🗂 <b>ꜰɪʟᴇꜱ:</b> <code>{len(batch.get('files', []))}</code>",
    ]
    if batch.get("first_message_id"):
        lines.append(f"🆔 <b>ꜰɪʀꜱᴛ ᴍᴇꜱꜱᴀɢᴇ:</b> <code>{batch['first_message_id']}</code>")
    if batch.get("last_message_id"):
        lines.append(f"🆔 <b>ʟᴀꜱᴛ ᴍᴇꜱꜱᴀɢᴇ:</b> <code>{batch['last_message_id']}</code>")
    if batch.get("post_code"):
        lines.append(f"📥 <b>ʟɪɴᴋ:</b> <code>https://t.me/{config.bot_username}?start=file_{batch['post_code']}</code>")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("cbatch:"))
async def custom_batch_callback_handler(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Unauthorized.", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]

    if action == "close":
        try:
            await cb.message.delete()
        except Exception:
            pass
        return

    if action == "panel":
        batches = await database.list_custom_batches()
        try:
            await cb.message.edit_text(
                "📦 <b>ᴄᴜꜱᴛᴏᴍ ʙᴀᴛᴄʜᴇꜱ</b>\n\n"
                f"<i>Showing the {min(len(batches), 20)} most recent (of {len(batches)}).</i>",
                reply_markup=custom_batch_list_keyboard(batches[:20]),
            )
        except Exception as e:
            LOG.warning(f"batch panel refresh failed: {e}")
        await cb.answer()
        return

    batch_id = int(parts[2])

    if action == "view":
        batch = await database.get_custom_batch(batch_id)
        if not batch:
            await cb.answer("That batch no longer exists.", show_alert=True)
            return
        try:
            await cb.message.edit_text(_batch_detail_text(batch), reply_markup=custom_batch_detail_keyboard(batch))
        except Exception as e:
            LOG.warning(f"batch detail render failed: {e}")
        await cb.answer()
        return

    if action == "delete":
        batch = await database.get_custom_batch(batch_id)
        if not batch:
            await cb.answer("Already gone.", show_alert=True)
            return
        try:
            await cb.message.edit_text(
                f"⚠️ <b>Delete Custom Batch #{batch_id}?</b>\n"
                "<i>This removes the batch record. Any Genlink already generated for it "
                "will stop working.</i>",
                reply_markup=custom_batch_delete_confirm_keyboard(batch_id),
            )
        except Exception as e:
            LOG.warning(f"batch delete-confirm render failed: {e}")
        await cb.answer()
        return

    if action == "delete_confirm":
        batch = await database.get_custom_batch(batch_id)
        if batch and batch.get("post_code"):
            await database.revoke_post(batch["post_code"])
        deleted = await database.delete_custom_batch(batch_id)
        batches = await database.list_custom_batches()
        try:
            await cb.message.edit_text(
                "📦 <b>ᴄᴜꜱᴛᴏᴍ ʙᴀᴛᴄʜᴇꜱ</b>\n\n"
                f"<i>Showing the {min(len(batches), 20)} most recent (of {len(batches)}).</i>",
                reply_markup=custom_batch_list_keyboard(batches[:20]),
            )
        except Exception as e:
            LOG.warning(f"batch panel refresh failed: {e}")
        await cb.answer("Deleted." if deleted else "Already gone.")
        return

    if action == "regenerate":
        batch = await database.get_custom_batch(batch_id)
        if not batch or not batch.get("files"):
            await cb.answer("Nothing to link — this batch has no files.", show_alert=True)
            return
        if batch.get("post_code"):
            await database.revoke_post(batch["post_code"])
        is_protected = (await database.get_setting("link_mode", "direct") == "shortener")
        file_ids = [int(f["id"]) for f in batch["files"]]
        post_row = await database.create_post(kind="batch", file_ids=file_ids, protected=is_protected)
        await database.set_batch_post_code(batch_id, post_row["code"])
        batch = await database.get_custom_batch(batch_id)
        try:
            await cb.message.edit_text(_batch_detail_text(batch), reply_markup=custom_batch_detail_keyboard(batch))
        except Exception as e:
            LOG.warning(f"batch detail render failed: {e}")
        await cb.answer("Genlink generated.")
        return

    if action == "revoke":
        batch = await database.get_custom_batch(batch_id)
        if batch and batch.get("post_code"):
            await database.revoke_post(batch["post_code"])
        batch = await database.get_custom_batch(batch_id)
        try:
            await cb.message.edit_text(_batch_detail_text(batch), reply_markup=custom_batch_detail_keyboard(batch))
        except Exception as e:
            LOG.warning(f"batch detail render failed: {e}")
        await cb.answer("Genlink revoked.")
        return

    await cb.answer()


# --- Admin: send ANYTHING to the bot (file, text, link, sticker, etc.) to store + link it ---
# NOTE: this is a broad catch-all (no filter) — it MUST be the LAST handler registered
# in this router, or it will shadow every command/callback handler defined above it.
@router.message()
async def handle_direct_upload(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type != "private":
        return

    admin_id = message.from_user.id

    # --- 1) Free-text input the admin panels (Force-Sub / Buttons /
    # Welcome) are waiting on. Handled first and unconditionally (even for
    # "/"-prefixed text) so an admin can paste a URL starting with a
    # command-like prefix without it being swallowed elsewhere. ---
    if admin_id in PENDING_ADMIN_INPUT:
        handled = await _consume_pending_admin_input(message)
        if handled:
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

    # --- 2) Custom Batch (First Message -> Last Message) collection.
    # Tracked in the DATABASE (not memory) via get_active_message_batch,
    # so it survives a bot restart mid-collection. Independent of the old
    # BATCH_SESSIONS in-memory /batch flow below — both can't be active at
    # once for sane UX, but neither duplicates the other's storage. ---
    active_msg_batch = await database.get_active_message_batch(admin_id)
    if active_msg_batch:
        file_row = await database.add_stored_file(
            storage_message_id=copied.message_id, original_name=name, tag="custom_batch"
        )
        await database.add_file_to_custom_batch(int(active_msg_batch["id"]), int(file_row["id"]))
        count = len((await database.get_custom_batch(int(active_msg_batch["id"])))["files"])
        await message.answer(
            f"📦 <b>Added to Custom Batch #{active_msg_batch['id']}</b> "
            f"(<code>{count}</code> item{'s' if count != 1 else ''} so far).\n"
            f"<i>Send more, or</i> <code>/finishbatch</code> <i>to finalize.</i>"
        )
        return

    file_row = await database.add_stored_file(
        storage_message_id=copied.message_id,
        original_name=name,
        tag="batch" if admin_id in BATCH_SESSIONS else "single"
    )

    if admin_id in BATCH_SESSIONS:
        BATCH_SESSIONS[admin_id].append(int(file_row["id"]))
        count = len(BATCH_SESSIONS[admin_id])
        await message.answer(
            f"✅ <b>ᴀᴅᴅᴇᴅ ᴛᴏ ʙᴀᴛᴄʜ</b> (<code>{count}</code> item{'s' if count != 1 else ''} so far).\n"
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
        f"🔗 <b>ʟɪɴᴋ ɢᴇɴᴇʀᴀᴛᴇᴅ!</b>\n\n"
        f"📄 <b>ɪᴛᴇᴍ:</b> <code>{name}</code>\n"
        f"📥 <b>ʟɪɴᴋ:</b> <code>{share_url}</code>"
    )