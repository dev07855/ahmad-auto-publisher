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
import os, re, sys, html, json, random, time, tempfile, shutil, traceback, struct, zlib, zipfile, glob, asyncio, requests
from datetime import datetime, timezone, timedelta
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

def brain_enabled():
    """فحص سريع: هل النشر لا زال مفعّلاً؟ (لاحترام زر الإيقاف فوراً حتى وسط الجولة)."""
    try:
        return bool(requests.get(BRAIN + "/tgsource", headers=HDR, timeout=15).json().get("enabled", True))
    except Exception:
        return True   # عند تعذّر الفحص لا نوقف (الأمان: نكمل)

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

def brain_alert(msg):
    """تنبيه فوري للمالك عبر المخ (تخطّي/فشل تطبيق، معالم)."""
    try:
        requests.post(BRAIN + "/alert", headers=HDR, json={"msg": msg}, timeout=30)
    except Exception as e:
        print("[alert] failed:", e)

def brain_stats(payload):
    """يرفع تحليلات القناة اليومية للمخ (مشاهدات/تفاعلات/منشورات)."""
    try:
        requests.post(BRAIN + "/stats", headers=HDR, json=payload, timeout=30)
    except Exception as e:
        print("[stats] post failed:", e)

def brain_backfill_done():
    """يبلّغ المخ بانتهاء السحب التدريجي (يوقفه ويرسل تنبيهاً مرة واحدة)."""
    try:
        requests.post(BRAIN + "/tgsource", headers=HDR, json={"backfill_done": 1}, timeout=30)
    except Exception as e:
        print("[backfill] done post failed:", e)


async def collect_stats(client, ident):
    """يمسح منشورات اليوم بالقناة (بتوقيت السعودية) ويجمع المشاهدات والتفاعلات ويرفعها للمخ."""
    ksa = timezone(timedelta(hours=3))
    day_start = datetime.now(ksa).replace(hour=0, minute=0, second=0, microsecond=0)
    day_str = day_start.strftime("%Y-%m-%d")
    start_ts = day_start.timestamp()
    # تطبيع المعرّف: @username كما هو، والرقم (-100…) يُحوّل int ليحلّه تيليثون
    s = str(ident).strip()
    if s and not s.startswith("@"):
        try: ident = int(s)
        except ValueError: ident = s
    ent = await client.get_entity(ident)
    views = reactions = posts = 0
    async for m in client.iter_messages(ent, limit=400):
        if not m.date:
            continue
        if m.date.timestamp() < start_ts:      # أقدم من بداية اليوم → وقف
            break
        posts += 1
        views += (m.views or 0)
        try:
            if m.reactions and m.reactions.results:
                reactions += sum((r.count or 0) for r in m.reactions.results)
        except Exception:
            pass
    brain_stats({"day": day_str, "views": views, "reactions": reactions, "posts": posts})
    print(f"[stats] {day_str} مشاهدات={views} تفاعلات={reactions} منشورات={posts}")

PER_APP_TIMEOUT = 480   # مهلة كل تطبيق (ث): بعدها نتخطّاه فوراً بلا تعليق

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
def _extract_version(cap, filename):
    """يستخرج الإصدار من صيغ بلاتانتس المتعدّدة بدقّة: التعليق (Updated to/Version) ثم اسم الملف (_vX.Y.Z_)."""
    cap = cap or ""; fn = filename or ""
    # 1) صيغة صريحة بالتعليق: Updated to / Version / الإصدار: X.Y[.Z]
    mm = re.search(r'(?:Updated\s*to|Version|Ver|الإصدار|الاصدار)\s*[:：]?\s*v?([0-9]+(?:\.[0-9]+)+)', cap, re.I)
    if mm:
        return mm.group(1)
    # 2) vX.Y.Z مسبوقة بفاصل/بداية (بالتعليق ثم اسم الملف) — يلتقط الرقم كاملاً
    for src in (cap, fn):
        mm = re.search(r'(?:^|[_\-. (\[])v([0-9]+(?:\.[0-9]+)+)', src, re.I)
        if mm:
            return mm.group(1)
    # 3) رقم إصدار كامل (X.Y.Z فأكثر) محاط بفواصل باسم الملف
    mm = re.search(r'(?:^|[_\-. ])([0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)*)(?=[_\-. ]|$)', fn)
    return mm.group(1) if mm else ""


def parse_meta(caption, filename):
    cap = caption or ""
    # الاسم الأساسي من اسم الملف: نشيل ' 3BodSy' واللاحقة .ipa
    name = re.sub(r'\.ipa$', '', filename or "", flags=re.I)
    name = re.sub(r'\s*3?\s*bodsy.*$', '', name, flags=re.I).strip()
    version = _extract_version(cap, filename)
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


# ---- التعريب الذكي (جيمناي) — بأمانة تامة: يعرّب المذكور فقط، ما يخترع ولا يزيد ولا يعدّل ----
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                 "gemini-3.5-flash", "gemini-flash-latest"]
GROQ_MODEL = "openai/gpt-oss-120b"   # بديل جروك مجاني بحدّ يومي عالٍ عند نفاد جيمناي

# الخاتمة = فوتر كل قناة الحالي (من إعدادات البوت)


class TransientError(Exception):
    """عطل مؤقّت (تعريب/شبكة/مخ) — نوقف الدفعة بلا تقديم المؤشّر ونعيد لاحقاً، لا نخسر التطبيق."""
    pass


def _sanitize_src(src):
    """ينظّف نص المصدر من بصمة الناشر قبل التعريب: معرّفات @، روابط، وأسطر البرandة."""
    out = []
    for ln in (src or "").splitlines():
        t = re.sub(r'@\w+', '', ln)
        t = re.sub(r'https?://\S+|t\.me/\S+', '', t)
        if re.search(r'(?i)blatant|bodsy|syripa|ipaomtk|check0?ver|t\.me|telegram|قناة|تابعنا|اشترك|'
                     r'(?:modded|modified|patched|cracked|signed|released)\s*by', t):
            continue
        t = t.strip()
        if t:
            out.append(t)
    return "\n".join(out)


def _build_prompt(name, cap):
    src = _sanitize_src(cap or name or "")
    return (
        "أنت كاتب محتوى عربي فاخر لقناة تطبيقات آيفون.\n"
        "هذه معلومات تطبيق كما وردت من المصدر:\n---\n" + src + "\n---\n"
        "المطلوب بأمانة تامة وبدون أي اختراع أو مبالغة أو تعديل:\n"
        "1) name: اسم التطبيق النظيف المختصر بالإنجليزي فقط — بدون رقم الإصدار وبدون "
        "كلمات مثل Unlocked/Patched/Premium/Mod/blatant وبدون شرطات سفلية أو رموز.\n"
        "2) desc: وصف عربي فاخر قصير جداً (سطر إلى سطرين) لوظيفة التطبيق، مبني على المعلومات "
        "المذكورة فقط لا غير.\n"
        "3) features: عرّب للعربية المميزات/التغييرات المذكورة في نص المصدر بأسلوب جذاب ومهذّب. "
        "يجب أن تكون features مصفوفة JSON من جُمل عربية، كل ميزة عنصر نصّي مستقل (وليست نصاً واحداً). "
        "ممنوع تماماً اختراع أي ميزة غير مذكورة، وممنوع الزيادة من عندك، وممنوع تعديل أو تضخيم "
        "أي ميزة. إذا لم يذكر المصدر مميزات واضحة فاكتب من 2 إلى 3 نقاط واقعية موجزة تصف وظيفة "
        "التطبيق الأساسية فقط بلا مبالغة.\n"
        "⛔ ممنوع منعاً باتاً ذكر أو ترجمة أي اسم ناشر/مصدر/مطوّر معدِّل، أو أي معرّف يبدأ بـ@، أو أي رابط، "
        "أو عبارة مثل «تم التعديل/التطوير بواسطة فلان» — تجاهلها نهائياً ولا تُدرجها بأي حقل.\n"
        "ممنوع أي كلمة إنجليزية في المخرجات عدا حقل name. أعِد JSON فقط بالحقول: name, desc, features."
    )


def _valid(o):
    if not (isinstance(o, dict) and o.get("name") and o.get("desc")):
        return None
    f = o.get("features")
    if isinstance(f, list):
        o["features"] = [str(x).strip() for x in f if str(x).strip()]
    elif isinstance(f, str) and f.strip():
        parts = [p.strip(" \u2022\u00b7-*\t") for p in re.split(r"[\n\r]+", f) if p.strip()]
        o["features"] = parts if len(parts) >= 2 else [f.strip()]
    else:
        o["features"] = []
    return o


def _try_gemini(prompt):
    """يجرّب جيمناي؛ يرجّع dict أو None (None = نفد حدّه/فشل، ننتقل لجروك)."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7}}
    for m in GEMINI_MODELS:
        try:
            r = None
            for _rl in range(3):                      # عند 429 ننتظر ويتجدد الحد اللحظي ثم نعيد
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % m,
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body, timeout=45)
                if r.status_code != 429:
                    break
                if _rl < 2:
                    print("[gemini] 429 — ننتظر 30ث ونعيد"); time.sleep(30)
            if r.status_code == 429:                  # نفد حدّ جيمناي، ننتقل لجروك
                print("[gemini] 429 مستمر → بديل جروك"); return None
            if r.status_code in (500, 503):
                continue
            parts = r.json()["candidates"][0]["content"]["parts"]
            txt = next((p["text"] for p in parts if str(p.get("text", "")).strip().startswith("{")),
                       parts[-1].get("text", ""))
            o = _valid(json.loads(txt))
            if o:
                return o
        except Exception as e:
            print("[gemini] %s: %s" % (m, str(e)[:90])); continue
    return None


def _try_groq(prompt):
    """بديل مجاني: جروك (llama-3.3-70b). يرجّع dict أو None."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return None
    body = {"model": GROQ_MODEL, "temperature": 0.7,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    for attempt in range(2):
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                              json=body, timeout=45)
            if r.status_code == 429:
                time.sleep(20); continue
            if r.status_code >= 500:
                continue
            txt = r.json()["choices"][0]["message"]["content"]
            o = _valid(json.loads(txt))
            if o:
                return o
        except Exception as e:
            print("[groq] %s" % str(e)[:90]); continue
    return None


def _gemini_localize(name, cap):
    """يعرّب بأمانة: جيمناي أولاً، وإذا نفد حدّه → جروك. يرمي TransientError لو فشل الاثنان."""
    prompt = _build_prompt(name, cap)
    o = _try_gemini(prompt)
    if o:
        return o
    o = _try_groq(prompt)
    if o:
        print("[localize] عُرّب عبر جروك (جيمناي غير متاح)"); return o
    raise TransientError("تعذّر التعريب عبر جيمناي وجروك — سنعيد تلقائياً بالجولة القادمة")


CAPTION_LIMIT = 1000   # حدّ تلقرام للتعليق 1024 حرف مرئي — نبقى دونه بأمان

def _vlen(s):
    """طول مرئي تقريبي (بلا وسوم HTML) — تلقرام يحسب النص الظاهر فقط."""
    return len(re.sub(r"<[^>]+>", "", s))

def clean_app_filename(app_name):
    """اسم ملف نظيف = اسم التطبيق فقط (بلا إصدار ولا زوائد)، مع إزالة رموز الملفات الممنوعة."""
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', str(app_name or '')).strip()
    safe = re.sub(r'\s+', ' ', safe)
    return (safe[:80] or "app") + ".ipa"


def _format_caption(o, version, footer=""):
    """يبني نص المنشور العربي من ردّ جيمناي ضمن حدّ تلقرام، وينهي بفوتر القناة الحالي."""
    esc = html.escape                                   # يمنع رفض تلقرام لأي < أو > أو &
    title = "✨ " + esc(str(o["name"]).strip())
    desc = str(o["desc"]).strip()
    if len(desc) > 400:                                 # وصف طويل جداً → قصّه بأمان
        desc = desc[:400].rstrip() + "…"
    desc = esc(desc)
    tail_ver = "📱 الإصدار: " + esc(version or "—")
    foot = esc(str(footer).strip()) if footer and str(footer).strip() else ""
    # الأجزاء الثابتة (عنوان + وصف + عنوان المميزات + الإصدار + الفوتر) لها الأولوية
    base = [title, "", desc, "", "🔹 المميزات:", "", tail_ver]
    if foot:
        base += ["", foot]
    budget = CAPTION_LIMIT - _vlen("\n".join(base))
    feats, used = [], 0
    _feats = o.get("features") or []
    if isinstance(_feats, str):                         # حماية: لو رجع نصاً بدل قائمة لا تقسّمه حروفاً
        _feats = [_feats]
    for f in _feats[:6]:
        f = str(f).strip().lstrip("•-*·").strip()
        if not f:
            continue
        raw = "• " + f                                  # القياس على النص الخام (الطول المرئي)
        if used + len(raw) + 1 > budget:
            break
        feats.append("• " + esc(f)); used += len(raw) + 1
    parts = [title, "", desc, "", "🔹 المميزات:"]
    if feats:
        parts.append("\n".join(feats))
    parts += ["", tail_ver]
    if foot:
        parts += ["", foot]
    return "\n".join(parts)


def build_caption(name, version, cap, footer, size=0):
    """(توافق) يعرّب ثم يبني المنشور بفوتر القناة. يرمي TransientError لو تعذّر التعريب."""
    return _format_caption(_gemini_localize(name, cap), version, footer)


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
        brain_alert(f"⚠️ <b>تطبيق كبير وتخطّيناه</b>\nالتطبيق: {name}\nالسبب: أكبر من حد تلقرام (٢ جيجا).")
        print(f"[skip] {name}: أكبر من حد تلقرام"); return "skip"
    # عرّب أولاً قبل التنزيل — لو تعذّر التعريب نوقف فوراً بلا تنزيل ولا ننشر شيئاً ناقصاً
    o = _gemini_localize(name, cap)                 # تعريب مرة وحدة (يرمي TransientError عند العطل)
    clean_fname = clean_app_filename(o.get("name") or name)   # اسم الملف = اسم التطبيق النظيف فقط
    workdir = tempfile.mkdtemp(prefix="tg_")
    try:
        raw = os.path.join(workdir, "raw.ipa")
        print(f"[download] {o.get('name')} v{version} ({round(size/1048576,1)}MB) [{kind}] ...")
        await client.download_media(m, file=raw)
        thumb = extract_icon(raw, os.path.join(workdir, "thumb.jpg"))   # أيقونة التطبيق
        info = {"name": o.get("name") or name, "version": version}
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
            # اسم الملف الظاهر بتلقرام = اسم التطبيق النظيف فقط
            newout = os.path.join(workdir, clean_fname)
            if out != newout:
                try: os.replace(out, newout); out = newout
                except OSError: pass
            try:
                targets = [{"chan": cid, "caption": _format_caption(o, version, ft)} for (cid, ft) in norm]
                cfg = dict(cfg_base); cfg["targets"] = targets; cfg["reactions"] = reactions
                await telegram._publish(cfg, out, targets[0]["caption"], thumb)   # نفس الحلقة
                published_any = True
            except BaseException as e:
                errors.append(str(e)[:120]); print("group publish failed:", e)
            finally:
                try: os.remove(out)
                except OSError: pass
        if published_any:
            brain_published(f"tg{m.id}", o.get("name") or name, version)
            print(f"PUBLISHED tg{m.id} {o.get('name') or name} | errors: {errors}")
            return "ok"
        raise RuntimeError("كل المجموعات فشلت: " + ("; ".join(errors) or "لا قنوات"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _run():
    st = brain_get()
    # مفتاح التعريب يصل من المخ (سرّ الووركر) — نحقنه بالبيئة بلا طباعة
    gk = (st.get("gemini_key") or "").strip()
    if gk and not os.environ.get("GEMINI_API_KEY"):
        os.environ["GEMINI_API_KEY"] = gk
    qk = (st.get("groq_key") or "").strip()
    if qk and not os.environ.get("GROQ_API_KEY"):
        os.environ["GROQ_API_KEY"] = qk
    enabled = bool(st.get("enabled", True))
    limit = int(st.get("limit", 4) or 4)
    groups = st.get("groups", [])
    reactions = [e.strip() for e in (st.get("reactions") or "").split(",") if e.strip()]
    footer = st.get("footer", "")
    last_id = int(st.get("last_id", 0) or 0)     # مؤشّر الجديد (id > last_id)
    back_id = int(st.get("back_id", 0) or 0)     # مؤشّر الباكفل (يمشي للأسفل)
    backfill_on = bool(st.get("backfill"))       # السحب التدريجي مفعّل؟
    back_days = int(st.get("back_days", 90) or 90)  # نافذة السحب (أيام) — افتراضي 3 شهور

    # قناة الوجهة لجمع التحليلات (أول قناة بالمجموعات أو المتغيّر)
    stats_ident = None
    for g in groups:
        for c in (g.get("channels") or []):
            stats_ident = (c.get("id") if isinstance(c, dict) else c)
            if stats_ident:
                break
        if stats_ident:
            break
    stats_ident = stats_ident or os.environ.get("TG_CHANNEL") or None

    api_id = int(os.environ["TG_USER_API_ID"]); api_hash = os.environ["TG_USER_API_HASH"]
    sess = os.environ["TG_USER_SESSION"]
    cfg_base = telegram.cfg_from_env()

    async with TelegramClient(StringSession(sess), api_id, api_hash) as client:
        # 📊 تحليلات القناة (مشاهدات/تفاعلات/منشورات اليوم) — كل ساعة، وتعمل حتى لو النشر موقوف
        if stats_ident and st.get("stats_due", True):
            try:
                await collect_stats(client, stats_ident)
            except Exception as e:
                print("[stats] fail:", str(e)[:120])
                brain_alert("⚠️ <b>تعذّر جمع تحليلات اليوم</b>\nالسبب: " + str(e)[:140])

        if not enabled:
            print("مصدر تلقرام موقوف (جُمعت التحليلات فقط)"); return
        if not groups:
            print("لا قنوات مفعّلة — تخطٍّ"); return

        # بدء السحب التدريجي: نقطة الانطلاق = آخر ما عالجناه (نمشي منها للأسفل)
        if backfill_on and not back_id:
            back_id = last_id
            brain_set_state(back_id=back_id)
        cutoff = int(time.time()) - back_days * 86400   # حدّ آخر back_days يوم

        # اجمع الجديد (id>last_id)
        new = []
        async for m in client.iter_messages(CH, min_id=last_id, reverse=True, limit=limit * 4):
            if _is_ipa(m):
                new.append(m)
        new = new[:limit]

        # الباكفل بالتاريخ: انزل من back_id، خذ اللي داخل النافذة، ووقف عند أقدم منها
        back = []; reached_end = False; more_in_window = False
        room = limit - len(new)
        if backfill_on and room > 0 and back_id:
            collected = []
            async for m in client.iter_messages(CH, offset_id=back_id, limit=room * 8):
                if m.date and m.date.timestamp() < cutoff:
                    reached_end = True; break          # وصلنا حدّ الـback_days → خلصنا
                if _is_ipa(m):
                    collected.append(m)
            more_in_window = len(collected) > room
            back = collected[:room]

        if not new and not back:
            if backfill_on and reached_end and not more_in_window:
                brain_backfill_done()
            print("لا جديد ولا باكفل"); return
        print(f"جديد: {len(new)} | باكفل: {len(back)}")

        done = 0
        # الجديد بالترتيب التصاعدي — ينشر كل واحد فوراً ويقدّم المؤشّر
        for m in sorted(new, key=lambda x: x.id):
            if not brain_enabled():           # احترام زر الإيقاف فوراً حتى وسط الجولة
                print("أُوقف النشر وسط الجولة — توقّف"); return
            try:
                res = await asyncio.wait_for(
                    _process_one(client, m, "new", cfg_base, groups, reactions, footer),
                    timeout=PER_APP_TIMEOUT)
                brain_set_state(last_id=m.id)
                if res == "ok":
                    done += 1
            except asyncio.TimeoutError:
                brain_set_state(last_id=m.id)   # تخطَّ فوراً (لا تعليق ولا إعادة)
                brain_alert(f"⚠️ <b>تطبيق تأخّر وتخطّيناه</b>\nالسبب: تجاوز المهلة ({PER_APP_TIMEOUT//60} دقيقة) — غالباً كبير أو الشبكة بطيئة.\n(المصدر: رسالة {m.id})")
                print(f"[timeout] tg{m.id} skipped")
            except TransientError as e:   # عطل مؤقّت → أوقف بلا تقديم المؤشّر (نعيد لاحقاً، ما نخسر التطبيق)
                brain_alert(f"⛔️ <b>توقّفت الدفعة مؤقّتاً</b>\nالسبب: {str(e)[:170]}\nلن نخسر أي تطبيق — سنعيد المحاولة تلقائياً بالجولة القادمة.")
                print(f"[transient] paused at tg{m.id}: {str(e)[:150]}"); return
            except BaseException as e:
                brain_set_state(last_id=m.id)   # تخطَّ فوراً، لا نعلّق ولا نعيد نفس التطبيق
                brain_alert(f"⚠️ <b>تطبيق فشل وتخطّيناه</b>\nالسبب: {str(e)[:150]}\n(المصدر: رسالة {m.id})")
                traceback.print_exc(); print(f"[fail] tg{m.id} skipped: {str(e)[:200]}")
        # الباكفل بالترتيب التنازلي (الأحدث أولاً)
        for m in back:
            if not brain_enabled():           # احترام زر الإيقاف فوراً حتى وسط الجولة
                print("أُوقف النشر وسط الباكفل — توقّف"); return
            try:
                res = await asyncio.wait_for(
                    _process_one(client, m, "back", cfg_base, groups, reactions, footer),
                    timeout=PER_APP_TIMEOUT)
                brain_set_state(back_id=m.id)
                if res == "ok":
                    done += 1
            except asyncio.TimeoutError:
                brain_set_state(back_id=m.id)
                brain_alert(f"⚠️ <b>تطبيق قديم تأخّر وتخطّيناه</b>\nالسبب: تجاوز المهلة ({PER_APP_TIMEOUT//60} دقيقة).\n(المصدر: رسالة {m.id})")
                print(f"[timeout back] tg{m.id} skipped")
            except TransientError as e:   # عطل مؤقّت → أوقف بلا تقديم مؤشّر الباكفل
                brain_alert(f"⛔️ <b>توقّف الباكفل مؤقّتاً</b>\nالسبب: {str(e)[:170]}\nلن نخسر أي تطبيق — سنعيد المحاولة تلقائياً بالجولة القادمة.")
                print(f"[transient back] paused at tg{m.id}: {str(e)[:150]}"); return
            except BaseException as e:
                brain_set_state(back_id=m.id)
                brain_alert(f"⚠️ <b>تطبيق قديم فشل وتخطّيناه</b>\nالسبب: {str(e)[:150]}\n(المصدر: رسالة {m.id})")
                traceback.print_exc(); print(f"[fail back] tg{m.id} skipped: {str(e)[:200]}")
        # وصلنا حدّ آخر back_days يوم بلا متبقٍّ داخل النافذة → السحب التدريجي خلص
        if backfill_on and reached_end and not more_in_window:
            brain_backfill_done()
        print(f"تمّت معالجة {done} تطبيق")


def run():
    # درع عام: أي خطأ غير متوقّع بكامل التشغيل (دخول تلقرام/الجلسة/المخ/الشبكة) → تنبيه فوري للمالك
    try:
        asyncio.run(_run())
    except BaseException as e:
        try:
            brain_alert(f"🚨 <b>عطل عام في القارئ</b>\nالسبب: {str(e)[:200]}\nتوقّفت هذه الجولة — لم يُنشر شيء ناقص، وسنعيد تلقائياً بالجولة القادمة.")
        except Exception:
            pass
        traceback.print_exc()
        raise


if __name__ == "__main__":
    run()
