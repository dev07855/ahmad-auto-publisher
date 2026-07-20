#!/usr/bin/env python3
"""
Telegram publisher.
Large IPA files (>50MB, up to 2GB) are uploaded via MTProto using the BOT token
(Telethon start(bot_token=...)) — needs API_ID + API_HASH (one-time, from my.telegram.org),
NO user phone login. Photos/text go through the same bot session.

Env / config keys:
  TG_API_ID, TG_API_HASH, TG_BOT_TOKEN, TG_CHANNEL  (channel @username or -100... id)
"""
import os, asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

def _client(cfg):
    # in-memory session; bot login is instant and stateless
    return TelegramClient(StringSession(), int(cfg["api_id"]), cfg["api_hash"])

async def _publish(cfg, ipa_path, caption, photos):
    client = _client(cfg)
    await client.start(bot_token=cfg["bot_token"])
    try:
        chan = cfg["channel"]
        # 1) album of screenshots + caption (photos are small, well under limits)
        if photos:
            await client.send_file(chan, photos, caption=caption, parse_mode="html")
            file_caption = None
        else:
            file_caption = caption
        # 2) the injected IPA as a document (progress-friendly, up to 2GB via MTProto)
        await client.send_file(
            chan, ipa_path, caption=file_caption, parse_mode="html",
            force_document=True, part_size_kb=512,
        )
    finally:
        await client.disconnect()

def publish(cfg, ipa_path, caption, photos=None):
    """cfg = {api_id, api_hash, bot_token, channel}. photos = list of local paths/urls."""
    asyncio.run(_publish(cfg, ipa_path, caption, photos or []))

def cfg_from_env():
    return {
        "api_id": os.environ["TG_API_ID"],
        "api_hash": os.environ["TG_API_HASH"],
        "bot_token": os.environ["TG_BOT_TOKEN"],
        "channel": os.environ["TG_CHANNEL"],
    }
