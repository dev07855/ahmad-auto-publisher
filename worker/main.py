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
import requests
from checkover import clean_name
import inject as injector

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")

def _clean_desc(desc):
    # strip any source references / links (ahmad + checkover, latin + arabic + handles)
    desc = re.sub(r'https?://\S*(ahmad|check0?ver)\S*', '', desc, flags=re.I)
    desc = re.sub(r'(?i)ahmad[\s\-_]*up|ahmad\s*dev|@\w*ahmad\w*', '', desc)
    desc = re.sub(r'(?i)check\s*0?ver|@\w*check0?ver\w*', '', desc)
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

TG_MAX_BYTES = 2_095_000_000  # حد تلقرام للرفع عبر البوت ≈ 2 جيجا

def _download(url, dest, verify_ipa=True):
    """تحميل مباشر لرابط CheckOver الموقّع (بلا دخول) مع فحص الحجم والسلامة."""
    with requests.get(url, stream=True, timeout=180, headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        if total and total > TG_MAX_BYTES:      # تخطٍّ فوري قبل تحميل ملف ضخم
            raise RuntimeError(f"OVERSIZE: {round(total / 1073741824, 2)}GB أكبر من حد تلقرام 2GB")
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk); done += len(chunk)
    if done == 0:
        raise RuntimeError("DEAD_APP: 0-byte file on server")
    if total and done != total:
        raise RuntimeError(f"download truncated: got {done} of {total} bytes")
    if verify_ipa:
        with open(dest, "rb") as f:
            if f.read(4) != b"PK\x03\x04":
                raise RuntimeError("downloaded file is not an IPA")
    return total or done


def prepare(app_id, download_url, info, footer=None):
    """تحميل التطبيق من رابطه الموقّع + بناء الوصف والأيقونة (بلا حقن).
    info = {name, version, description, icon, size?} (من الماسح). يُرجّع (raw, caption, thumb, info, work).
    الـ raw يبقى ليُحقن لكل مجموعة دايلب على حدة؛ حذفه مسؤولية المُنادي."""
    if not download_url or not download_url.startswith("https://check0ver.net/"):
        raise RuntimeError("download_url must be on check0ver.net")
    print(f"[info] {(info.get('name') or '').strip()} v{info.get('version')}")

    work = tempfile.mkdtemp(prefix="app_")
    raw = os.path.join(work, "raw.ipa")
    print("[download] ...")
    total = _download(download_url, raw, verify_ipa=True)
    print(f"[download] {total} bytes")

    info = dict(info)
    info["size"] = total                            # حجم فعلي بالبايت (للوصف)
    caption = build_caption(info, footer=footer)

    thumb = None
    if info.get("icon"):
        try:
            icon_path = os.path.join(work, "icon.png")
            ir = requests.get(info["icon"], timeout=30, headers={"User-Agent": UA})
            ir.raise_for_status()
            open(icon_path, "wb").write(ir.content)
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
            img.resize((320, 320), Image.LANCZOS).save(thumb, "JPEG", quality=90)
        except Exception as e:
            print("[thumb] skip:", e)
            thumb = None
    return raw, caption, thumb, info, work


def inject_app(raw_ipa, info, dylib_path, work):
    """حقن دايلب محدّد وإخراج IPA جاهز للنشر (بوابة إلزامية: لا نشر بلا حقن)."""
    out = os.path.join(work, clean_name(info.get("name"), info.get("version", "")))
    injector.main(raw_ipa, dylib_path, out)
    print(f"[inject] {os.path.basename(dylib_path)} -> {os.path.basename(out)}")
    return out


def process(app_id, download_url, info, footer=None):
    """مسار مبسّط (CLI/اختبار): تحميل + حقن بالدايلب الافتراضي + إرجاع الملف."""
    raw, caption, thumb, info2, work = prepare(app_id, download_url, info, footer=footer)
    out = inject_app(raw, info2, os.environ.get("DYLIB_PATH", "fixipa.dylib"), work)
    try: os.remove(raw)
    except OSError: pass
    return out, caption, thumb, info2

if __name__ == "__main__":
    # اختبار يدوي: main.py <download_url> <name> [version]
    if len(sys.argv) < 3:
        raise SystemExit("usage: main.py <download_url> <name> [version]")
    dl = sys.argv[1]; nm = sys.argv[2]; ver = sys.argv[3] if len(sys.argv) > 3 else ""
    out, caption, thumb, info = process(nm, dl, {"name": nm, "version": ver, "description": ""})
    print("=== caption ===\n" + caption + "\n=== file ===", out)
