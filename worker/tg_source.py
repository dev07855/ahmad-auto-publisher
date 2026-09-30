#!/usr/bin/env python3
"""
قارئ مصدر تلقرام (@AbodSyripa → 3BodSy):
  يقرأ الرسائل الجديدة، ينزّل ملف الـIPA المرفق، يشيل بصمة المصدر (3BodSyPatch.dylib)،
  يحقن دايلب المالك، وينشر بقنواته. يُنادى من GitHub Actions بالكرون.

Env:
  TG_USER_API_ID, TG_USER_API_HASH, TG_USER_SESSION   # حساب القراءة (userbot)
  SOURCE_CHANNEL                                       # @AbodSyripa
  BRAIN_URL, ENQUEUE_SECRET                            # العقل (أهداف/حالة/تأكيد)
  TG_API_ID, TG_API_HASH, TG_BOT_TOKEN, TG_CHANNEL     # نشر البوت
  DYLIB_PATH                                           # دايلب احتياطي
  STRIP_DYLIBS = 3BodSyPatch.dylib                     # بصمة المصدر (تُشال)
"""
import os, re, sys, html, tempfile, shutil, traceback, requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename
import main as worker
import telegram

BRAIN = os.environ["BRAIN_URL"].rstrip("/")
SECRET = os.environ["ENQUEUE_SECRET"]
CH = os.environ.get("SOURCE_CHANNEL", "AbodSyripa")
HDR = {"x-secret": SECRET}


def brain_get():
    return requests.get(BRAIN + "/tgsource", headers=HDR, timeout=30).json()

def brain_set_last(mid):
    try:
        requests.post(BRAIN + "/tgsource", headers=HDR, json={"last_id": int(mid)}, timeout=30)
    except Exception as e:
        print("[state] set last failed:", e)

def brain_published(app_id, name, version):
    try:
        requests.post(BRAIN + "/published", headers=HDR,
                      json={"app_id": app_id, "name": name, "version": version}, timeout=30)
    except Exception as e:
        print("[brain] published failed:", e)

def brain_log(kind, msg):
    # نستخدم /failed فقط للفشل الحقيقي؛ للسجل العام لا يوجد endpoint، نكتفي بالطباعة
    print(f"[{kind}] {msg}")

def fetch_dylib(name):
    """اكتب دايلب المجموعة (بالاسم، أو الفعّال إن فارغ) بمسار الحقن."""
    path = os.environ.get("DYLIB_PATH", "fixipa.dylib")
    try:
        url = BRAIN + "/dylib" + ("?name=" + requests.utils.quote(name) if name else "")
        r = requests.get(url, headers=HDR, timeout=60)
        if r.status_code == 200 and r.content:
            with open(path, "wb") as f:
                f.write(r.content)
            print(f"[dylib] {name or 'الفعّال'} ({len(r.content)} bytes)")
    except Exception as e:
        print("[dylib] fallback:", e)
    return path


# ---- استخراج اسم/إصدار/مميزات من الملف والتعليق ----
def parse_meta(caption, filename):
    cap = caption or ""
    # الاسم الأساسي من اسم الملف: نشيل ' 3BodSy' واللاحقة .ipa
    name = re.sub(r'\.ipa$', '', filename or "", flags=re.I)
    name = re.sub(r'\s*3?\s*bodsy.*$', '', name, flags=re.I).strip()
    # الإصدار من التعليق: أول V<رقم>
    mver = re.search(r'\bV\s*([0-9][0-9.]*)', cap)
    version = mver.group(1) if mver else ""
    return name.strip(), version.strip(), cap


# أسطر دعائية/بصمة نحذفها كاملة (المصدر) — لا نبقّي شيئاً يخصّهم
_DROP_LINE = re.compile(
    r'(?i)(3\s*bodsy|bodsy|syripa|plussy|t\.me|https?://|@\w+|telegram|'
    r'premium\s*features?\s*activated|من\s*المتجر|بشكل\s*مباشر|direct\s*(link|download)|'
    r'download.*store|store🔥|قناة|تابعنا|اشترك|الشات|chat)')

def clean_desc(cap, name=""):
    """يحوّل تعليق المصدر إلى أسطر مميزات نظيفة — بلا اسمهم/روابطهم/دعايتهم/اسم التطبيق المكرر."""
    out = []
    nlow = (name or "").lower().strip()
    for raw in (cap or "").splitlines():
        t = raw.strip()
        if not t:
            continue
        if _DROP_LINE.search(t):                      # سطر يخصّهم/دعاية → احذف
            continue
        t = re.sub(r'(?i)^\s*application\s+', '', t)   # بادئة Application
        t = re.sub(r'\bV[0-9][0-9.]*\b', '', t)        # أرقام الإصدار
        t = re.sub(r'^[\-\*•▪◾●·►▶‣∙:\s]+', '', t)     # علامات القوائم البادئة
        t = re.sub(r'[\s\-•]+$', '', t).strip()        # زوائد لاحقة
        # أسقط السطر لو صار فاضي، أو مجرد رموز/إيموجي، أو اسم التطبيق (أو جزء منه)
        letters = re.sub(r'[^\w؀-ۿ]', '', t).lower()
        if not letters:
            continue
        nl = re.sub(r'[^\w؀-ۿ]', '', nlow).lower()
        if nl and (letters in nl or nl in letters):    # سطر = اسم التطبيق أو مختصره → احذف
            continue
        out.append(t)
    return "\n".join(out)


def build_caption(name, version, cap, footer, size=0):
    info = {"name": name, "version": version, "description": clean_desc(cap, name), "size": size}
    return worker.build_caption(info, footer=footer)


def publish_app(app, cfg_base, groups, reactions, footer):
    """حقن + نشر تطبيق نُزّل مسبقاً (بلا جلسة telethon — نتفادى تعارض حلقات asyncio)."""
    name, version, cap, size = app["name"], app["version"], app["cap"], app["size"]
    raw, workdir = app["raw"], app["workdir"]
    thumb = None  # يمكن لاحقاً استخراج الأيقونة من الـIPA
    info = {"name": name, "version": version}
    published_any = False; errors = []
    for g in groups:
        chans = g.get("channels") or []
        # كل قناة: {id, footer} — أو معرّف خام (توافق قديم)
        norm = []
        for c in chans:
            if isinstance(c, dict):
                cid = c.get("id")
                ft = c.get("footer") if c.get("footer") not in (None, "") else footer
            else:
                cid, ft = c, footer
            if cid:
                norm.append((cid, ft))
        if not norm:
            continue
        dylib_path = fetch_dylib(g.get("dylib") or "")
        out = worker.inject_app(raw, info, dylib_path, workdir)   # يحقن + يشيل STRIP_DYLIBS
        try:
            # نص خاص لكل قناة (نفس الملف، تعليق مختلف)
            targets = [{"chan": cid, "caption": build_caption(name, version, cap, ft, size=size)}
                       for (cid, ft) in norm]
            cfg = dict(cfg_base); cfg["targets"] = targets; cfg["reactions"] = reactions
            telegram.publish(cfg, out, targets[0]["caption"], thumb)
            published_any = True
        except BaseException as e:
            errors.append(str(e)[:120]); print("group publish failed:", e)
        finally:
            try: os.remove(out)
            except OSError: pass

    if published_any:
        brain_published(f"tg{app['id']}", name, version)
        print(f"PUBLISHED tg{app['id']} {name} | errors: {errors}")
        return "ok"
    raise RuntimeError("كل المجموعات فشلت: " + ("; ".join(errors) or "لا قنوات"))


def run():
    st = brain_get()
    if not st.get("enabled", True):
        print("مصدر تلقرام موقوف"); return
    last_id = int(st.get("last_id", 0) or 0)
    limit = int(st.get("limit", 4) or 4)
    groups = st.get("groups", [])
    reactions = [e.strip() for e in (st.get("reactions") or "").split(",") if e.strip()]
    footer = st.get("footer", "")
    if not groups:
        print("لا قنوات مفعّلة — تخطٍّ"); return

    api_id = int(os.environ["TG_USER_API_ID"]); api_hash = os.environ["TG_USER_API_HASH"]
    sess = os.environ["TG_USER_SESSION"]
    cfg_base = telegram.cfg_from_env()

    # ── المرحلة 1: قراءة + تحميل (داخل جلسة telethon فقط) ──
    items = []
    with TelegramClient(StringSession(sess), api_id, api_hash) as client:
        msgs = []
        for m in client.iter_messages(CH, min_id=last_id, limit=60):
            if isinstance(m.media, MessageMediaDocument) and m.document:
                fn = next((a.file_name for a in m.document.attributes if isinstance(a, DocumentAttributeFilename)), None)
                if fn and fn.lower().endswith(".ipa"):
                    msgs.append(m)
        msgs.sort(key=lambda x: x.id)              # الأقدم أولاً
        if not msgs:
            print("لا جديد"); return
        print(f"جديد: {len(msgs)} تطبيق (نعالج حتى {limit})")
        for m in msgs[:limit]:
            name, version, cap = parse_meta(m.message, next(
                (a.file_name for a in m.document.attributes if isinstance(a, DocumentAttributeFilename)), None))
            size = m.document.size or 0
            if size > worker.TG_MAX_BYTES:
                items.append({"id": m.id, "oversize": True, "name": name}); continue
            workdir = tempfile.mkdtemp(prefix="tg_")
            raw = os.path.join(workdir, "raw.ipa")
            print(f"[download] {name} v{version} ({round(size/1048576,1)}MB) ...")
            client.download_media(m, file=raw)
            items.append({"id": m.id, "name": name, "version": version, "cap": cap,
                          "size": size, "raw": raw, "workdir": workdir})

    # ── المرحلة 2: حقن + نشر (خارج جلسة telethon) بالترتيب ──
    done = 0
    for it in items:
        if it.get("oversize"):
            print(f"[skip] {it['name']}: أكبر من حد تلقرام")
            brain_set_last(it["id"]); continue
        try:
            res = publish_app(it, cfg_base, groups, reactions, footer)
            brain_set_last(it["id"])
            if res == "ok":
                done += 1
        except BaseException as e:
            traceback.print_exc()
            print(f"[fail] tg{it['id']}: {str(e)[:200]}")
            break   # نوقف عند أول فشل حتى لا نتخطّى تطبيقاً (يُعاد المرّة الجاية)
        finally:
            shutil.rmtree(it["workdir"], ignore_errors=True)
    print(f"تمّت معالجة {done} تطبيق")


if __name__ == "__main__":
    run()
