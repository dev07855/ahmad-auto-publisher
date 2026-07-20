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
from telethon.tl.types import DocumentAttributeFilename

def _client(cfg):
    # in-memory session; bot login is instant and stateless
    return TelegramClient(StringSession(), int(cfg["api_id"]), cfg["api_hash"])

async def _publish(cfg, ipa_path, caption, thumb):
    client = _client(cfg)
    await client.start(bot_token=cfg["bot_token"])
    try:
        chan = cfg["channel"]
        fname = os.path.basename(ipa_path)

        # ONE cohesive premium message: the IPA document carries the app icon as its
        # thumbnail AND the formatted features as its caption. Uploading (the slow part)
        # finishes before the single message is posted, so nothing shows without its file.
        handle = await client.upload_file(ipa_path, part_size_kb=512)
        await client.send_file(
            chan, handle, caption=caption, parse_mode="html",
            force_document=True, thumb=thumb,
            attributes=[DocumentAttributeFilename(fname)],
        )
    finally:
        await client.disconnect()

def publish(cfg, ipa_path, caption, thumb=None):
    """cfg = {api_id, api_hash, bot_token, channel}. thumb = local jpg path for the doc icon."""
    asyncio.run(_publish(cfg, ipa_path, caption, thumb))

def cfg_from_env():
    return {
        "api_id": os.environ["TG_API_ID"],
        "api_hash": os.environ["TG_API_HASH"],
        "bot_token": os.environ["TG_BOT_TOKEN"],
        "channel": os.environ["TG_CHANNEL"],
    }
