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
    # strip any ahmad references / links (latin + arabic + telegram handles)
    desc = re.sub(r'https?://\S*ahmad\S*', '', desc, flags=re.I)
    desc = re.sub(r'(?i)ahmad[\s\-_]*up|ahmad\s*dev|@\w*ahmad\w*', '', desc)
    desc = re.sub(r'أحمد\s*ديف|احمد\s*ديف|متجر\s*أحمد|متجر\s*احمد', '', desc)
    desc = re.sub(r'(?i)ahmad', '', desc)
    return desc


def _format_features(desc):
    """Turn Ahmad's raw '- feature .' lines into a tidy premium list with a bullet each."""
    lines = []
    for raw in desc.splitlines():
        t = raw.strip()
        if not t:
            continue
        # drop leading list markers (-, •, *, وكل أنواع المربّعات) and trailing lone dots/spaces
        t = re.sub(r'^[\-\*•▪▫◾◽■□●○·►▶‣∙]+\s*', '', t)
        t = re.sub(r'\s*\.\s*$', '', t).strip()
        if not t:
            continue
        lines.append(t)
    return lines


CAPTION_LIMIT = 1024  # Telegram media-caption hard limit (visible chars)


def build_caption(info, footer=None):
    """Premium one-message caption: title • version/size, then tidy feature bullets, then footer.
    Used as the CAPTION of the IPA document (icon shown as its thumbnail).

    Truncation is done on the RAW feature list (whole bullets only) BEFORE HTML-escaping,
    so we never cut an HTML entity in half (which Telegram would reject), and the final
    visible length is kept within Telegram's limit.
    """
    name = (info.get("name") or "").strip()
    ver = (info.get("version") or "").strip()
    size_mb = round(int(info.get("size", 0)) / 1048576, 1) if str(info.get("size", "")).isdigit() else None
    desc = _clean_desc((info.get("description") or "").strip())
    if footer is None:
        footer = os.environ.get("CHANNEL_FOOTER", "").strip()
    feats = _format_features(desc)

    header = [f"📲 <b>{html.escape(name)}</b>"]
    meta = []
    if ver: meta.append(f"الإصدار {html.escape(ver)}")
    if size_mb: meta.append(f"{size_mb} MB")
    if meta: header.append("🔖 " + " • ".join(meta))

    footer_block = (["", html.escape(footer)] if footer else [])
    # budget for the feature body (in visible chars): limit minus header + footer + label
    fixed_len = len("\n".join(header + ["", "✨ المميزات:"] + [x for x in footer_block]))
    budget = CAPTION_LIMIT - fixed_len - 8  # small safety margin

    body_lines, used = [], 0
    for f in feats:
        line = f"✦ {f}"                        # measure on RAW text (visible length)
        if used + len(line) + 1 > budget:
            break
        body_lines.append(f"✦ {html.escape(f)}")  # escape only what we keep
        used += len(line) + 1

    parts = list(header)
    if body_lines:
        parts += ["", "✨ <b>المميزات:</b>", "\n".join(body_lines)]
    parts += footer_block
    return "\n".join(parts)

def process(app_id, download_url=None, footer=None):
    # validate inputs (defence-in-depth: app_id numeric, download_url on ahmad only)
    if not re.fullmatch(r'\d+', str(app_id)):
        raise RuntimeError(f"invalid app_id: {app_id!r}")
    if download_url and not download_url.startswith("https://ahmad-up.com/"):
        raise RuntimeError("download_url must be on ahmad-up.com")

    a = Ahmad()
    # metadata (public, no auth)
    info = a.app_info(app_id)
    print(f"[info] {info.get('name')} v{info.get('version')}")

    # حد تلقرام للرفع عبر البوت ≈ 2 جيجا — تخطٍّ فوري قبل تحميل ملف ضخم بلا فائدة
    TG_MAX_BYTES = 2_095_000_000
    _sz = int(info.get("size") or 0)
    if _sz and _sz > TG_MAX_BYTES:
        raise RuntimeError(f"OVERSIZE: {round(_sz / 1073741824, 2)}GB أكبر من حد تلقرام 2GB")

    # resolve download link if not supplied (needs login)
    if not download_url:
        email = os.environ.get("AHMAD_EMAIL"); pw = os.environ.get("AHMAD_PASSWORD")
        if not (email and pw):
            raise RuntimeError("download_url not given and AHMAD_EMAIL/PASSWORD missing")
        ok, msg = a.login(email, pw)
        if not ok:
            raise RuntimeError(f"ahmad login failed: {msg}")
        row = next((r for r in a.list_recent() if r["id"] == str(app_id)), None)
        if not row:
            raise RuntimeError(f"app {app_id} not found in recent listing")
        download_url = row["download_url"]

    work = tempfile.mkdtemp(prefix="app_")
    raw = os.path.join(work, "raw.ipa")
    print("[download] ...")
    _, total, done = a.download(download_url, raw, verify_ipa=True)
    print(f"[download] {done} bytes")

    # MANDATORY dylib injection gate — nothing publishes without it
    out = os.path.join(work, clean_name(info.get("name"), info.get("version", "")))
    dylib = os.environ.get("DYLIB_PATH", "fixipa.dylib")
    injector.main(raw, dylib, out)
    print(f"[inject] -> {os.path.basename(out)}")
    # raw IPA no longer needed — free the disk immediately
    try: os.remove(raw)
    except OSError: pass

    caption = build_caption(info, footer=footer)
    # download the icon and turn it into a small JPEG thumbnail for the document
    # (shown ON the file itself, like a premium post — one cohesive message).
    thumb = None
    if info.get("icon"):
        try:
            icon_path = os.path.join(work, "icon.png")
            a.download(info["icon"], icon_path)
            thumb = os.path.join(work, "thumb.jpg")
            from PIL import Image
            img = Image.open(icon_path)
            # الأيقونات الشفافة: ركّبها على خلفية بيضاء (بدلاً من أسود عند التحويل لـJPEG)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            # أيقونة صغيرة تُرفق بالملف نفسه (الشكل النظيف الفخم — رسالة واحدة، بلا صورة كبيرة)
            img.resize((320, 320), Image.LANCZOS).save(thumb, "JPEG", quality=90)
        except Exception as e:
            print("[thumb] skip:", e)
            thumb = None
    return out, caption, thumb, info

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: main.py <app_id> [download_url]")
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
