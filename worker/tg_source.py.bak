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
import os, re, sys, html, tempfile, shutil, traceback, struct, zlib, zipfile, glob, asyncio, requests
from telethon import TelegramClient
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

def brain_set_state(**kw):
    """يحدّث مؤشّرات الحالة بالعقل (last_id للجديد، back_id للباكفل)."""
    try:
        requests.post(BRAIN + "/tgsource", headers=HDR,
                      json={k: int(v) for k, v in kw.items() if v is not None}, timeout=30)
    except Exception as e:
        print("[state] set failed:", e)

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


# ---- استخراج أيقونة التطبيق من الـIPA (تظهر كصورة مصغّرة على المنشور) ----
def _paeth(a, b, c):
    p = a + b - c; pa = abs(p - a); pb = abs(p - b); pc = abs(p - c)
    return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)

def _cgbi_to_image(data):
    """يفكّ PNG بصيغة آبل CgBI (أو العادي) → صورة PIL RGB."""
    from PIL import Image
    import io
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return Image.open(io.BytesIO(data)).convert('RGB')
    pos = 8; w = h = None; idat = b''; cgbi = False
    while pos < len(data):
        ln = struct.unpack('>I', data[pos:pos+4])[0]; typ = data[pos+4:pos+8]; chunk = data[pos+8:pos+8+ln]
        if typ == b'CgBI': cgbi = True
        elif typ == b'IHDR': w, h = struct.unpack('>II', chunk[:8])
        elif typ == b'IDAT': idat += chunk
        elif typ == b'IEND': break
        pos += 12 + ln
    if not cgbi:
        return Image.open(io.BytesIO(data)).convert('RGB')
    raw = zlib.decompressobj(-15).decompress(idat)
    bpp = 4; stride = w * bpp; out = bytearray(); prev = bytearray(stride); i = 0
    for _ in range(h):
        f = raw[i]; i += 1; line = bytearray(raw[i:i+stride]); i += stride
        for x in range(stride):
            a = line[x-bpp] if x >= bpp else 0; b = prev[x]; c = prev[x-bpp] if x >= bpp else 0
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4: line[x] = (line[x] + _paeth(a, b, c)) & 255
        prev = line; out += line
    for p in range(0, len(out), 4):        # BGRA→RGBA + فك الضرب المسبق بالألفا
        B, G, R, A = out[p], out[p+1], out[p+2], out[p+3]
        if A: R = min(255, R*255//A); G = min(255, G*255//A); B = min(255, B*255//A)
        out[p], out[p+1], out[p+2] = R, G, B
    return Image.frombytes('RGBA', (w, h), bytes(out)).convert('RGB')

def extract_icon(ipa_path, dest):
    """أكبر AppIcon داخل الـIPA → thumb.jpg بحجم 320 (أو None لو تعذّر)."""
    try:
        with zipfile.ZipFile(ipa_path) as z:
            names = [n for n in z.namelist()
                     if re.search(r'Payload/[^/]+\.app/AppIcon[^/]*\.png$', n, re.I)]
            if not names:
                names = [n for n in z.namelist() if re.search(r'Payload/[^/]+\.app/[^/]*[Ii]con[^/]*\.png$', n)]
            if not names:
                return None
            best = max(names, key=lambda n: z.getinfo(n).file_size)   # الأكبر ≈ الأعلى دقّة
            img = _cgbi_to_image(z.read(best))
        img.thumbnail((320, 320))
        img.save(dest, "JPEG", quality=88)
        return dest
    except Exception as e:
        print("[icon] skip:", str(e)[:80]); return None


def _is_ipa(m):
    if isinstance(m.media, MessageMediaDocument) and m.document:
        fn = next((a.file_name for a in m.document.attributes if isinstance(a, DocumentAttributeFilename)), None)
        return fn if (fn and fn.lower().endswith(".ipa")) else None
    return None


async def _process_one(client, m, kind, cfg_base, groups, reactions, footer):
    """ينزّل + يحقن + ينشر تطبيقاً واحداً فوراً لكل قنواته (حلقة asyncio واحدة)."""
    fn = _is_ipa(m)
    name, version, cap = parse_meta(m.message, fn)
    size = m.document.size or 0
    if size > worker.TG_MAX_BYTES:
        print(f"[skip] {name}: أكبر من حد تلقرام"); return "skip"
    workdir = tempfile.mkdtemp(prefix="tg_")
    try:
        raw = os.path.join(workdir, "raw.ipa")
        print(f"[download] {name} v{version} ({round(size/1048576,1)}MB) [{kind}] ...")
        await client.download_media(m, file=raw)
        thumb = extract_icon(raw, os.path.join(workdir, "thumb.jpg"))   # أيقونة التطبيق
        info = {"name": name, "version": version}
        published_any = False; errors = []
        for g in groups:
            norm = []
            for c in (g.get("channels") or []):
                if isinstance(c, dict):
                    cid = c.get("id"); ft = c.get("footer") if c.get("footer") not in (None, "") else footer
                else:
                    cid, ft = c, footer
                if cid:
                    norm.append((cid, ft))
            if not norm:
                continue
            dylib_path = fetch_dylib(g.get("dylib") or "")
            out = worker.inject_app(raw, info, dylib_path, workdir)   # يحقن + يشيل STRIP_DYLIBS
            try:
                targets = [{"chan": cid, "caption": build_caption(name, version, cap, ft, size=size)}
                           for (cid, ft) in norm]
                cfg = dict(cfg_base); cfg["targets"] = targets; cfg["reactions"] = reactions
                await telegram._publish(cfg, out, targets[0]["caption"], thumb)   # نفس الحلقة
                published_any = True
            except BaseException as e:
                errors.append(str(e)[:120]); print("group publish failed:", e)
            finally:
                try: os.remove(out)
                except OSError: pass
        if published_any:
            brain_published(f"tg{m.id}", name, version)
            print(f"PUBLISHED tg{m.id} {name} | errors: {errors}")
            return "ok"
        raise RuntimeError("كل المجموعات فشلت: " + ("; ".join(errors) or "لا قنوات"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _run():
    st = brain_get()
    if not st.get("enabled", True):
        print("مصدر تلقرام موقوف"); return
    limit = int(st.get("limit", 4) or 4)
    groups = st.get("groups", [])
    reactions = [e.strip() for e in (st.get("reactions") or "").split(",") if e.strip()]
    footer = st.get("footer", "")
    last_id = int(st.get("last_id", 0) or 0)   # مؤشّر الجديد (id > last_id)
    back_id = int(st.get("back_id", 0) or 0)   # مؤشّر الباكفل (id < back_id)؛ 0 = لا باكفل
    min_id = int(st.get("min_id", 0) or 0)     # حد الباكفل (تاريخ 2–3 أشهر)
    if not groups:
        print("لا قنوات مفعّلة — تخطٍّ"); return

    api_id = int(os.environ["TG_USER_API_ID"]); api_hash = os.environ["TG_USER_API_HASH"]
    sess = os.environ["TG_USER_SESSION"]
    cfg_base = telegram.cfg_from_env()

    async with TelegramClient(StringSession(sess), api_id, api_hash) as client:
        # اجمع الجديد (id>last_id) + الباكفل (id<back_id حتى min_id)
        new = []
        async for m in client.iter_messages(CH, min_id=last_id, reverse=True, limit=limit * 4):
            if _is_ipa(m):
                new.append(m)
        new = new[:limit]
        back = []
        room = limit - len(new)
        if room > 0 and back_id and back_id > min_id:
            async for m in client.iter_messages(CH, offset_id=back_id, limit=room * 4):
                if _is_ipa(m) and m.id > min_id:
                    back.append(m)
            back = back[:room]
        if not new and not back:
            print("لا جديد ولا باكفل"); return
        print(f"جديد: {len(new)} | باكفل: {len(back)}")

        done = 0
        # الجديد بالترتيب التصاعدي — ينشر كل واحد فوراً ويقدّم المؤشّر
        for m in sorted(new, key=lambda x: x.id):
            try:
                res = await _process_one(client, m, "new", cfg_base, groups, reactions, footer)
                brain_set_state(last_id=m.id)
                if res == "ok":
                    done += 1
            except BaseException as e:
                traceback.print_exc(); print(f"[fail] tg{m.id}: {str(e)[:200]}")
                print(f"تمّت معالجة {done} تطبيق"); return   # لا نقدّم المؤشّر (يُعاد المرّة الجاية)
        # الباكفل بالترتيب التنازلي (الأحدث أولاً)
        for m in back:
            try:
                res = await _process_one(client, m, "back", cfg_base, groups, reactions, footer)
                brain_set_state(back_id=m.id)
                if res == "ok":
                    done += 1
            except BaseException as e:
                traceback.print_exc(); print(f"[fail back] tg{m.id}: {str(e)[:200]}")
                break
        print(f"تمّت معالجة {done} تطبيق")


def run():
    asyncio.run(_run())


if __name__ == "__main__":
    run()
