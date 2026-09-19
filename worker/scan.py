#!/usr/bin/env python3
"""
الماسح: يسجّل دخول CheckOver، يقرأ الأقسام (تصنيفات) بالترتيب (الأحدث تحديثاً أولاً)،
ويرسلها لعقل كلاودفلير. كل تطبيق يحمل رابط تحميله الموقّع + بياناته الكاملة (meta) للمنشور.
يعمل بجدول GitHub Actions كل بضع دقائق.

Env: CHECKOVER_USER, CHECKOVER_PASS, BRAIN_URL, ENQUEUE_SECRET, SCAN_PAGES(optional)
الأقسام تُقرأ من العقل: path = uuid التصنيف بـCheckOver.
"""
import os, sys, requests
from checkover import CheckOver

# احتياطي إن تعذّرت قراءة الأقسام من العقل (key, category_uuid)
FALLBACK_SECTIONS = [
    ("games",  "9c60f563-1983-42f0-8882-a26207bd4aaf"),  # ألعاب
    ("apps",   "9c60f57f-b2be-49b8-be17-aa0231a3ec50"),  # تطبيقات
    ("design", "9c65babe-44ec-41f4-b452-98e8f4649479"),  # تصميم
    ("paid",   "9c65bb1e-afb0-427b-8811-547ae30dd6a7"),  # مدفوعة
]

def get_config():
    """الأقسام (key, category_uuid) + عدد صفحات المسح من العقل."""
    try:
        r = requests.get(os.environ["BRAIN_URL"].rstrip("/") + "/sections",
                         headers={"x-secret": os.environ["ENQUEUE_SECRET"]}, timeout=30)
        r.raise_for_status()
        j = r.json()
        secs = [(s["key"], s["path"]) for s in j.get("sections", []) if s.get("path")]
        pages = int(j.get("pages") or 0) or None
        return (secs or FALLBACK_SECTIONS), pages
    except Exception as e:
        print("get_config failed, using fallback:", e)
        return FALLBACK_SECTIONS, None

def scan_section(c, category_uuid, pages):
    """يمسح صفحات القسم 1..pages (الأحدث تحديثاً أولاً)، يوقف عند صفحة فاضية."""
    apps, rank = [], 0
    for pg in range(1, pages + 1):
        try:
            rows, meta = c.list_apps(category_uuid, pg)
        except Exception as e:
            print(f"  صفحة {pg} فشلت: {e}")
            break
        if not rows:
            break
        for a in rows:
            uuid = a.get("uuid"); du = a.get("downloadURL")
            if not uuid or not du:
                continue  # بلا رابط (مجاني بلا ملف؟) → تجاهل
            apps.append({
                "id": uuid,
                "name": a.get("name", ""),
                "version": a.get("version", ""),
                "download_url": du,
                "rank": rank,
                "meta": {
                    "description": a.get("description", ""),
                    "icon": a.get("image", ""),
                    "size": a.get("size", ""),
                    "bundle": a.get("bundle", ""),
                },
            })
            rank += 1
        last = meta.get("last_page")
        if last and pg >= last:
            break
    return apps

def main():
    c = CheckOver()
    ok, msg = c.login(os.environ["CHECKOVER_USER"], os.environ["CHECKOVER_PASS"])
    if not ok:
        print("checkover login failed:", msg); sys.exit(1)
    sections, pages_cfg = get_config()
    pages = pages_cfg or int(os.environ.get("SCAN_PAGES") or "3")
    total = 0
    for section, cat_uuid in sections:
        try:
            apps = scan_section(c, cat_uuid, pages)
        except Exception as e:
            print(f"scan {section} failed: {e}"); continue
        resp = requests.post(os.environ["BRAIN_URL"].rstrip("/") + "/enqueue",
                             headers={"x-secret": os.environ["ENQUEUE_SECRET"]},
                             json={"section": section, "apps": apps}, timeout=60)
        print(f"enqueue[{section}]:", resp.status_code, resp.text[:150])
        resp.raise_for_status()
        total += 1
    if total == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
