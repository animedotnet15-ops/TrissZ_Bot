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
from database import database
from keyboards import settings_keyboard, custom_button, broadcast_confirm_keyboard, broadcast_progress_keyboard

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
        "ok": "✅ <b>Verified</b>",
        "expired": "⏰ <b>Expired</b>",
        "missing": "❌ <b>Invalid Token</b>",
        "too_fast": "🚫 <b>Bypass Attempt (too fast)</b>",
        "user_mismatch": "🚫 <b>Bypass Attempt (token hijack)</b>",
        "used": "⚠️ <b>Retry — Already Used</b>",
    }
    label = status_labels.get(status, f"ℹ️ <b>{status}</b>")

    full_name = " ".join(filter(None, [user.first_name, user.last_name])) or "Unknown"
    username = f"@{user.username}" if user.username else "N/A"

    text = (
        f"{label}\n\n"
        f"👤 <b>Name:</b> {full_name}\n"
        f"🔗 <b>Username:</b> {username}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"🤖 <b>Bot:</b> @{config.bot_username}\n"
        f"📅 <b>Date:</b> {now.strftime('%Y-%m-%d')}\n"
        f"⏱ <b>Time (UTC):</b> {now.strftime('%H:%M:%S')}\n"
    )
    if link:
        text += f"🌐 <b>Link:</b> {link}\n"
    if detail:
        text += f"📝 <b>Detail:</b> <i>{detail}</i>\n"

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
                    "🚦 <b>Slow down!</b>\n<i>You're sending messages too fast — please wait a few seconds.</i>"
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

    if message.from_user:
        await database.increment_download_count(message.from_user.id)
        try:
            first_name = files[0]["original_name"] if files else ""
            await database.record_download(message.from_user.id, post_row["code"], first_name)
        except Exception as e:
            LOG.warning(f"record_download failed (non-fatal, delivery unaffected): {e}")

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
        "👤 <b>Your Profile</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{profile['user_id']}</code>\n"
        f"📛 <b>Name:</b> {profile['first_name']}\n"
        f"🔗 <b>Username:</b> {username}\n"
        f"📅 <b>Joined:</b> {joined}\n"
        f"🕒 <b>Last Activity:</b> {last_seen}\n\n"
        f"🏆 <b>Rank:</b> {rank}\n"
        f"✅ <b>Verifications:</b> <code>{profile['verification_count']}</code>\n"
        f"📥 <b>Downloads:</b> <code>{profile['download_count']}</code>\n"
        f"🤝 <b>Referrals:</b> <code>{profile['referral_count']}</code>\n"
        f"⭐ <b>Status:</b> {premium_text}\n"
        f"⚠️ <b>Warnings:</b> <code>{profile['warnings']}/{config.strike_limit}</code>\n"
    )
    await message.answer(text)


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
            "🧭 <b>Help</b>\n\n"
            "<blockquote>Just open a file link that was shared with you — I'll take care of the rest, "
            "including any verification steps.</blockquote>\n\n"
            "⭐ <b>Extras</b>\n"
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
            await send_verification_log(user, "missing", detail=f"token={token}")
            await message.answer(
                "❌ <b>Invalid Token!</b>\n\n"
                "<i>This verification link is invalid. Please request the file link again.</i>"
            )
            return
        elif status == "expired":
            await send_verification_log(user, "expired", detail=f"token={token}")
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
            await send_verification_log(user, "too_fast", detail=f"strikes={count}/{config.strike_limit}, banned={banned}")
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
            await send_verification_log(user, "user_mismatch", detail=f"strikes={count}/{config.strike_limit}, banned={banned}")
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
            await send_verification_log(user, "used", detail=f"token={token}")
            await message.answer(
                "⚠️ <b>Already Used!</b>\n\n"
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
    if not is_owner(message.from_user.id):
        await message.answer("❌ <i>Owner-only command — shortener config affects the whole verification flow.</i>")
        return
    raw = (command.args or "").strip()
    if not raw:
        current = await database.get_setting("shortener_url", "")
        await message.answer(
            "⚠️ <b>Usage:</b> <code>/setshortner arolinks.com</code>\n\n"
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
            f"❌ <b>That doesn't look like a valid domain:</b> <code>{raw}</code>\n\n"
            f"<i>Enter just the bare domain, e.g.</i> <code>/setshortner arolinks.com</code>"
        )
        return

    await database.set_setting("shortener_url", domain)
    await message.answer(
        f"✅ <b>Shortener domain set to:</b> <code>{domain}</code>\n\n"
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
            "⚠️ <b>Usage:</b> <code>/setlogchannel -1001234567890</code>\n\n"
            "<i>Add this bot as admin in that channel first, then run this command with its numeric ID.</i>\n"
            "<i>Send</i> <code>/setlogchannel off</code> <i>to disable logging.</i>\n\n"
            f"Current: <code>{current if current else 'not set'}</code>"
        )
        return

    if raw.lower() == "off":
        await database.set_setting("log_channel_id", "")
        await message.answer("✅ <b>Log channel logging disabled.</b>")
        return

    try:
        channel_id = int(raw)
    except ValueError:
        await message.answer("❌ <b>Invalid channel ID.</b> It must be a numeric ID like <code>-1001234567890</code>.")
        return

    try:
        await bot.send_message(channel_id, "✅ <b>This channel is now set as the Log Channel.</b>\n<i>Verification events will be posted here.</i>")
    except TelegramAPIError as exc:
        await message.answer(
            f"❌ <b>Could not send a test message to that channel:</b> <code>{exc}</code>\n\n"
            f"<i>Make sure the bot is an admin there and the ID is correct.</i>"
        )
        return

    await database.set_setting("log_channel_id", str(channel_id))
    await message.answer(f"✅ <b>Log channel set to:</b> <code>{channel_id}</code>")


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
    if not is_owner(message.from_user.id):
        await message.answer("❌ <i>Owner-only command — database backend config.</i>")
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


# --- Admin: /testshortner ---
@router.message(Command("testshortner"))
async def test_shortener_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    domain = (await database.get_setting("shortener_url", "arolinks.com")).strip()
    token = await database.get_setting("arolinks_api_token", "")

    if not token:
        await message.answer("❌ <b>No API token set.</b> Use <code>/setapitoken [token]</code> first.")
        return

    test_url = "https://t.me/"
    result = await shorten_with_arolinks(test_url)

    if result == test_url:
        await message.answer(
            f"❌ <b>Shortener call FAILED.</b>\n\n"
            f"🌐 <b>Domain used:</b> <code>{domain}</code>\n"
            f"🔑 <b>Token set:</b> <code>{token[:6]}...{token[-4:] if len(token) > 10 else ''}</code>\n\n"
            f"<i>The bot silently fell back to an unshortened link, which means no ad view / no wallet "
            f"credit happens on real links either. Check the bot's logs for the exact API error, and "
            f"double-check that</i> <code>{domain}</code> <i>is the exact domain shown on your shortener "
            f"account's API/dashboard page (not just a display name).</i>"
        )
    else:
        await message.answer(
            f"✅ <b>Shortener call succeeded!</b>\n\n"
            f"🌐 <b>Domain used:</b> <code>{domain}</code>\n"
            f"🔗 <b>Generated link:</b> {result}\n\n"
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
        await message.answer("⚠️ <b>Usage:</b> <code>/broadcast [message text]</code>")
        return

    users = await database.broadcast_user_ids()
    BROADCAST_PENDING[message.from_user.id] = text
    preview = text if len(text) <= 500 else text[:497] + "..."
    await message.answer(
        "📢 <b>Broadcast Preview</b>\n\n"
        f"{preview}\n\n"
        f"👥 <b>Recipients:</b> <code>{len(users)}</code> users\n\n"
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
        await cb.message.edit_text("❌ <b>Broadcast cancelled.</b> Nothing was sent.")
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
        f"📢 <b>Broadcasting...</b>\n\n<code>0/{total}</code> sent",
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
                    f"📢 <b>Broadcasting...</b>\n\n<code>{i}/{total}</code> sent "
                    f"(<code>{success}</code> ok, <code>{failed}</code> failed)",
                    reply_markup=broadcast_progress_keyboard(admin_id),
                )
            except TelegramAPIError:
                pass

    BROADCAST_STOP_FLAGS.pop(admin_id, None)
    outcome = "🛑 <b>Broadcast stopped by admin.</b>" if stopped_early else "✅ <b>Broadcast complete!</b>"
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
                f"🌐 <b>Shortener Domain:</b> <code>{short_domain}</code>\n"
                f"🔑 <b>API Token:</b> <code>{'YES ✅' if has_api_token else 'NO ❌'}</code>\n"
                f"🔗 <b>Protection Mode:</b> <code>{link_mode.upper()}</code>"
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
        await message.answer("⚠️ <b>Usage:</b> <code>/fav CODE</code>\n<i>CODE is the part after ?start=file_ or ?start=get_ in a link you've received.</i>")
        return
    post = await database.get_post(code)
    if not post:
        await message.answer("❌ <i>No such link found — check the code and try again.</i>")
        return
    added = await database.add_favorite(message.from_user.id, code)
    await message.answer("⭐ <b>Added to favorites!</b>" if added else "ℹ️ <i>Already in your favorites.</i>")


@router.message(Command("unfav"))
async def unfav_handler(message: Message, command: CommandObject):
    if not message.from_user:
        return
    code = (command.args or "").strip()
    if not code:
        await message.answer("⚠️ <b>Usage:</b> <code>/unfav CODE</code>")
        return
    removed = await database.remove_favorite(message.from_user.id, code)
    await message.answer("🗑 <b>Removed from favorites.</b>" if removed else "ℹ️ <i>That wasn't in your favorites.</i>")


@router.message(Command("favorites"))
async def favorites_handler(message: Message):
    if not message.from_user:
        return
    favs = await database.list_favorites(message.from_user.id, limit=20)
    if not favs:
        await message.answer(
            "⭐ <b>Favorites</b>\n\n<i>You haven't favorited anything yet.</i>\n"
            "<i>Use</i> <code>/fav CODE</code> <i>on a link's code to save it here.</i>"
        )
        return
    lines = ["⭐ <b>Your Favorites</b>\n"]
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
        await message.answer("📥 <b>Download History</b>\n\n<i>Nothing here yet — files you receive will show up in this list.</i>")
        return
    lines = ["📥 <b>Recent Downloads</b>\n"]
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
        await message.answer("⚠️ <b>Usage:</b> <code>/search keyword</code>")
        return
    results = await database.search_files(query, limit=15)
    if not results:
        await message.answer(f"🔎 <i>No stored files matched</i> <code>{query}</code>.")
        return
    lines = [f"🔎 <b>Search results for</b> <code>{query}</code>\n"]
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
    lines = ["📂 <b>File Categories</b>\n"]
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
        "📊 <b>Admin Dashboard</b>\n\n"
        f"👥 <b>Users:</b> <code>{s['total_users']}</code> total · "
        f"<code>{s['today_users']}</code> today · <code>{s['yesterday_users']}</code> yesterday\n"
        f"📈 <b>Growth:</b> <code>{s['week_users']}</code> this week · <code>{s['month_users']}</code> this month\n"
        f"🟢 <b>Online (last 15m):</b> <code>{s['online_users']}</code>\n"
        f"🚫 <b>Banned:</b> <code>{s['total_bans']}</code>\n\n"
        f"📂 <b>Files:</b> <code>{s['total_files']}</code> · <b>Links:</b> <code>{s['total_posts']}</code>\n"
        f"📥 <b>Downloads:</b> <code>{s['total_downloads']}</code>\n"
        f"✅ <b>Verifications:</b> <code>{s['total_verifications']}</code>\n\n"
        f"🏆 <b>Most Downloaded:</b>\n{top}\n\n"
        f"🗄 <b>Backend:</b> <code>{config.db_backend.upper()}</code>"
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
    await message.answer_document(
        BufferedInputFile(payload, filename=filename),
        caption=(
            "🗄 <b>Backup Complete</b>\n\n"
            "<i>This is a full JSON snapshot of every table. Restoring it (with "
            "/restore) only ever ADDS missing rows — it can never overwrite or "
            "delete anything already in your database.</i>"
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
    doc = message.reply_to_message.document
    try:
        file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(file.file_path)
        backup = json.loads(buf.read())
    except Exception as e:
        await message.answer(f"❌ <b>Could not read backup file:</b> <code>{e}</code>")
        return
    report = await database.restore_backup(backup)
    lines = ["✅ <b>Restore Complete</b> <i>(insert-only — nothing existing was touched)</i>\n"]
    for table, count in report.items():
        lines.append(f"• {table}: <code>{count}</code> new rows added")
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
        await message.answer("⚠️ <b>Usage:</b> <code>/addmod [user_id or @username]</code>")
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
        await message.answer("⚠️ <b>Usage:</b> <code>/removemod [user_id or @username]</code>")
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
    lines = ["🧰 <b>Staff Roles</b>\n"]
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
    await message.answer(f"🪪 <b>Your role:</b> {label}")


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

    lines = ["🩺 <b>Health Monitor</b>\n"]
    all_ok = True
    for name, ok, detail in checks:
        icon = "✅" if ok else "❌"
        if not ok:
            all_ok = False
        lines.append(f"{icon} <b>{name}:</b> <code>{detail}</code>")
    lines.append(f"\n🗄 <b>Backend:</b> <code>{config.db_backend.upper()}</code>")
    lines.append("\n" + ("🟩 <b>All systems operational.</b>" if all_ok else "🟧 <b>One or more checks need attention.</b>"))
    await message.answer("\n".join(lines))


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