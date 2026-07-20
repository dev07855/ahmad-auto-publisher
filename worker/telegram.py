#!/usr/bin/env python3
"""
Telegram publisher.
Large IPA files (>50MB, up to 2GB) are uploaded via MTProto using the BOT token
(Telethon start(bot_token=...)) — needs API_ID + API_HASH (one-time, from my.telegram.org),
NO user phone login. Photos/text go through the same bot session.

Env / config keys:
  TG_API_ID, TG_API_HASH, TG_BOT_TOKEN, TG_CHANNEL  (channel @username or -100... id)
"""
import os, asyncio, requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import FloodWaitError

# جلسة البوت تُحفظ بالعقل وتُعاد للاستخدام — نتفادى تسجيل دخول جديد كل مرة (يمنع FloodWait)
def _load_session(cfg):
    try:
        r = requests.get(cfg["brain"].rstrip("/") + "/tgsession",
                         headers={"x-secret": cfg["enqueue"]}, timeout=15)
        return r.json().get("session", "") or ""
    except Exception:
        return ""

def _save_session(cfg, s):
    try:
        requests.post(cfg["brain"].rstrip("/") + "/tgsession",
                      headers={"x-secret": cfg["enqueue"]}, json={"session": s}, timeout=15)
    except Exception as e:
        print("[tg] save session failed:", e)

def _norm_chan(c):
    """معرّف القناة: @username كما هو، والرقم (-100…) يُحوّل int ليحلّه Telethon."""
    c = str(c).strip()
    if not c or c.startswith("@"):
        return c
    try:
        return int(c)
    except ValueError:
        return c

async def _publish_once(cfg, ipa_path, caption, thumb):
    saved = _load_session(cfg) if cfg.get("brain") else ""
    client = TelegramClient(StringSession(saved), int(cfg["api_id"]), cfg["api_hash"])
    await client.connect()
    # سجّل دخول البوت فقط إن لم تكن الجلسة صالحة، ثم احفظها للمرات القادمة
    if not await client.is_user_authorized():
        await client.sign_in(bot_token=cfg["bot_token"])
        if cfg.get("brain"):
            _save_session(cfg, client.session.save())
    try:
        channels = cfg.get("channels") or ([cfg["channel"]] if cfg.get("channel") else [])
        channels = [_norm_chan(c) for c in channels if str(c).strip()]
        if not channels:
            raise RuntimeError("no target channel(s) to publish to")
        fname = os.path.basename(ipa_path)

        # رسالة واحدة نظيفة وفخمة: الملف (IPA) يحمل أيقونة التطبيق كصورة مصغّرة + الوصف تعليقاً.
        # يُرفع الملف مرة للقناة الأولى، ثم يُعاد استخدام نفس ملف تلقرام لباقي القنوات (بلا إعادة رفع).
        first = await client.send_file(
            channels[0], ipa_path, caption=caption, parse_mode="html",
            force_document=True, thumb=thumb, part_size_kb=512,
            attributes=[DocumentAttributeFilename(fname)],
        )
        for chan in channels[1:]:
            await client.send_file(
                chan, first.media, caption=caption, parse_mode="html",
                force_document=True,
                attributes=[DocumentAttributeFilename(fname)],
            )
    finally:
        await client.disconnect()

async def _publish(cfg, ipa_path, caption, thumb):
    # transient errors (network blips, Telegram FloodWait) shouldn't fail a good app
    last = None
    for attempt in range(3):
        try:
            return await _publish_once(cfg, ipa_path, caption, thumb)
        except FloodWaitError as e:
            wait = min(int(getattr(e, "seconds", 30)) + 2, 120)
            print(f"[tg] flood wait {wait}s (attempt {attempt+1})")
            await asyncio.sleep(wait)
            last = e
        except Exception as e:
            print(f"[tg] error (attempt {attempt+1}): {e}")
            last = e
            await asyncio.sleep(5 * (attempt + 1))
    raise last

def publish(cfg, ipa_path, caption, thumb=None):
    """cfg = {api_id, api_hash, bot_token, channel}. thumb = local jpg path for the doc icon."""
    asyncio.run(_publish(cfg, ipa_path, caption, thumb))

def cfg_from_env():
    return {
        "api_id": os.environ["TG_API_ID"],
        "api_hash": os.environ["TG_API_HASH"],
        "bot_token": os.environ["TG_BOT_TOKEN"],
        "channel": os.environ["TG_CHANNEL"],
        "brain": os.environ.get("BRAIN_URL", ""),
        "enqueue": os.environ.get("ENQUEUE_SECRET", ""),
    }
