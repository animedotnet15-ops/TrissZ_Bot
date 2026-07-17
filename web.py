"""Protected shortener guard without Telegram-account binding."""
from __future__ import annotations

import time
from html import escape
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import config
from database import database

app = FastAPI(title="Songoku File Guard", docs_url=None, redoc_url=None)

def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    document = f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1524;color:#edf4ff;font-family:system-ui,-apple-system,Segoe UI,sans-serif}}
.card{{width:min(88vw,520px);padding:32px;border-radius:22px;background:#1c2d49;border:1px solid rgba(255,255,255,.1);text-align:center;box-shadow:0 18px 50px rgba(0,0,0,.3)}}
h1{{margin:0 0 14px;font-size:25px}}p{{line-height:1.55;color:#d4deef}}small{{display:block;margin-top:18px;color:#aebbd3}}
.btn{{display:inline-block;padding:14px 28px;background:#2563eb;color:#fff;text-decoration:none;border-radius:12px;font-weight:bold;margin-top:20px;transition:background 0.2s;}}
.btn:hover{{background:#1d4ed8;}}
</style></head><body><main class="card">{body}</main></body></html>"""
    return HTMLResponse(document, status_code=status)

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/g/{code}")
async def guard(code: str, request: Request) -> HTMLResponse:
    post = await database.get_post(code)
    if not post or not int(post["protected"]):
        return page("Invalid link", "<h1>❌ Link unavailable</h1><p>This protected link is invalid, disabled, or expired.</p>", 404)
    if not config.base_url.startswith("https://"):
        return page("Public HTTPS required", "<h1>⚠️ Guard is not public yet</h1><p>This bot needs a public HTTPS BASE_URL before protected Arolinks links can work.</p>", 503)

    # 🕵️ Anti-Bypass: Detect if the user-agent is an automated scraping script
    user_agent = request.headers.get("user-agent", "").lower()
    bot_keywords = ["python", "aiohttp", "requests", "curl", "wget", "scrape", "selenium", "puppeteer"]
    
    if not user_agent or any(keyword in user_agent for keyword in bot_keywords):
        # Scrapers get automatically booted back to Telegram with a penalty token
        return RedirectResponse(f"https://t.me/{config.bot_username}?start=warn_{code}", status_code=303)

    # For normal human users, display a simple unlock button with no login required
    tok = request.query_params.get("tok", "")
    verify_url = f"{config.base_url}/g/{code}/verify?tok={tok}"
    return page(
        "Verify Access",
        "<h1>🔐 Complete Verification</h1>"
        "<p>Click the button below to prove you are human and unlock your secure file delivery.</p>"
        f"<a class='btn' href='{verify_url}'>✓ CLAIM YOUR FILE</a>"
    )

@app.get("/g/{code}/verify")
async def verify_human_click(code: str, request: Request):
    post = await database.get_post(code)
    if not post or not int(post["protected"]):
        return page("Invalid link", "<h1>❌ Link unavailable</h1><p>This protected link no longer exists.</p>", 404)
    
    tok = request.query_params.get("tok", "")
    if not tok:
        return page("Invalid link", "<h1>❌ Missing verification token</h1><p>Please open the original file link again.</p>", 400)

    # Hand the already-issued one-time token back to the bot so it can be
    # validated and claimed there (expiry / single-use / owner checks live in bot.py).
    return RedirectResponse(f"https://t.me/{config.bot_username}?start=tok_{tok}", status_code=303)
