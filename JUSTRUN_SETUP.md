# JustRun.app setup

This package is made for JustRun.app. It does not include a `.env` file, so your existing secret values remain untouched when you replace files.

## 1. Upload / extract
Upload the ZIP in the same folder where JustRun currently runs the bot, then extract it there. Replace the project files when prompted. Do **not** delete `bot.db` and do **not** replace your `.env`.

The project files must be directly inside the folder JustRun runs:

```text
main.py
bot.py
config.py
database.py
web.py
requirements.txt
.env
```

## 2. JustRun variables
Keep these in JustRun's Environment Variables panel, or in `.env`:

```env
BOT_TOKEN=123456:replace_me
BOT_USERNAME=YourBotUsernameWithoutAt
ADMIN_IDS=123456789
STORAGE_CHANNEL_ID=-1001234567890
BASE_URL=https://your-project.a.jrnm.app
HOST=0.0.0.0
PORT=8000
DATABASE_PATH=bot.db
PROTECT_CONTENT=true
SESSION_MINUTES=10
STRIKE_LIMIT=3
```

`STORAGE_CHANNEL_ID` is the private database channel; add the bot there as an administrator.

## 3. Run command
Set the JustRun **Run command** to:

```bash
pip install -r requirements.txt && python main.py
```

Then start the project. You should see:

```text
Webhook cleared. Polling as @YourBotUsername
```

Open:

```text
https://your-project.a.jrnm.app/health
```

It should return `{"status":"ok"}`.

## 4. Arolinks anti-bypass setup
1. In `@BotFather`, run `/setdomain`, choose this bot, and send only the JustRun domain, for example `your-project.a.jrnm.app`.
2. In the bot PM, open `/settings` and switch **Shortener Protection** to ON.
3. Run `/genlink` or `/batch` again.
4. A protected `/g/...` URL is returned. Paste only that `/g/...` URL into Arolinks.

Never shorten a direct `t.me/...start=get_...` URL; it is a normal direct Telegram link and bypass bots can use it.

## Main commands

```text
/genlink             Send any number of files, then /done. One encrypted link per file.
/batch FIRST LAST    Batch link using storage message IDs.
/settings            Custom button, start photo/spoiler, delivery sticker, shortener protection.
/setstartphoto       Then send the welcome image as a normal Telegram photo.
/spoiler on|off      Toggle the welcome-image spoiler.
/setsticker          Then send a sticker; it is sent after completed file/batch delivery.
/delsticker          Remove delivery sticker.
/broadcast           Send or forward one message to broadcast it.
/ban USER_ID
/unban USER_ID
/strikes USER_ID
/resetstrikes USER_ID
/cancel
```
