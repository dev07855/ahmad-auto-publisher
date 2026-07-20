#!/usr/bin/env python3
"""
Worker orchestrator: process ONE app id end-to-end.
  read metadata -> download IPA -> inject dylib (mandatory gate) -> build caption -> publish

Called by the GitHub Actions job with an app id (chosen by the Cloudflare brain / queue).
Login is only needed to resolve the download link when it is not passed in.

Usage:
  python main.py <app_id> [download_url]
Env (secrets):
  AHMAD_EMAIL, AHMAD_PASSWORD           # to read listing / resolve download link
  DYLIB_PATH                            # path to fixipa.dylib (checked into repo or secret file)
  TG_API_ID, TG_API_HASH, TG_BOT_TOKEN, TG_CHANNEL
  CHANNEL_FOOTER (optional)             # branding footer appended to caption
"""
import os, sys, re, html, tempfile
from ahmad import Ahmad, clean_name
import inject as injector

def _clean_desc(desc):
    # strip any ahmad references / links
    desc = re.sub(r'https?://\S*ahmad\S*', '', desc, flags=re.I)
    desc = re.sub(r'(?i)ahmad[\s\-_]*up|ahmad\s*dev|@\w*ahmad\w*', '', desc)
    return desc


def _format_features(desc):
    """Turn Ahmad's raw '- feature .' lines into a tidy premium list with a bullet each."""
    lines = []
    for raw in desc.splitlines():
        t = raw.strip()
        if not t:
            continue
        # drop leading list markers (-, •, *, ▪) and trailing lone dots/spaces
        t = re.sub(r'^[\-\*•▪▫•·]+\s*', '', t)
        t = re.sub(r'\s*\.\s*$', '', t).strip()
        if not t:
            continue
        lines.append(t)
    return lines


def build_caption(info):
    """Premium one-message caption: title • version/size, then tidy feature bullets, then footer.
    Used as the CAPTION of the IPA document (icon shown as its thumbnail)."""
    name = info.get("name", "").strip()
    ver = info.get("version", "").strip()
    size_mb = round(int(info.get("size", 0)) / 1048576, 1) if str(info.get("size", "")).isdigit() else None
    desc = _clean_desc((info.get("description") or "").strip())
    footer = os.environ.get("CHANNEL_FOOTER", "").strip()
    feats = _format_features(desc)

    parts = [f"📲 <b>{html.escape(name)}</b>"]
    meta = []
    if ver: meta.append(f"الإصدار {html.escape(ver)}")
    if size_mb: meta.append(f"{size_mb} MB")
    if meta: parts.append("🔖 " + " • ".join(meta))
    if feats:
        parts.append("")
        parts.append("✨ <b>المميزات:</b>")
        body = "\n".join(f"▫️ {html.escape(f)}" for f in feats)
        # Telegram caption hard-limit is 1024 chars; keep room for header/footer
        if len(body) > 750:
            body = body[:750].rsplit("\n", 1)[0]
        parts.append(body)
    if footer:
        parts += ["", footer]
    return "\n".join(parts)

def process(app_id, download_url=None):
    a = Ahmad()
    # metadata (public, no auth)
    info = a.app_info(app_id)
    print(f"[info] {info['name']} v{info.get('version')}")

    # resolve download link if not supplied (needs login)
    if not download_url:
        email = os.environ.get("AHMAD_EMAIL"); pw = os.environ.get("AHMAD_PASSWORD")
        if not (email and pw):
            raise SystemExit("download_url not given and AHMAD_EMAIL/PASSWORD missing")
        ok, msg = a.login(email, pw)
        if not ok:
            raise SystemExit(f"ahmad login failed: {msg}")
        row = next((r for r in a.list_recent() if r["id"] == str(app_id)), None)
        if not row:
            raise SystemExit(f"app {app_id} not found in recent listing")
        download_url = row["download_url"]

    work = tempfile.mkdtemp(prefix="app_")
    raw = os.path.join(work, "raw.ipa")
    print("[download] ...")
    _, total, done = a.download(download_url, raw)
    print(f"[download] {done} bytes")

    # MANDATORY dylib injection gate — nothing publishes without it
    out = os.path.join(work, clean_name(info["name"], info.get("version", "")))
    dylib = os.environ.get("DYLIB_PATH", "fixipa.dylib")
    injector.main(raw, dylib, out)
    print(f"[inject] -> {os.path.basename(out)}")

    caption = build_caption(info)
    # download the icon and turn it into a small JPEG thumbnail for the document
    # (shown ON the file itself, like a premium post — one cohesive message).
    thumb = None
    if info.get("icon"):
        try:
            icon_path = os.path.join(work, "icon.png")
            a.download(info["icon"], icon_path)
            thumb = os.path.join(work, "thumb.jpg")
            from PIL import Image
            img = Image.open(icon_path).convert("RGB")
            img.thumbnail((320, 320))
            img.save(thumb, "JPEG", quality=85)
        except Exception as e:
            print("[thumb] skip:", e)
            thumb = None
    return out, caption, thumb, info

if __name__ == "__main__":
    app_id = sys.argv[1]
    dl = sys.argv[2] if len(sys.argv) > 2 else None
    out, caption, thumb, info = process(app_id, dl)
    print("=== caption ===")
    print(caption)
    print("=== file ===", out)
    if os.environ.get("PUBLISH") == "1":
        import telegram
        telegram.publish(telegram.cfg_from_env(), out, caption, thumb)
        print("[publish] done")
