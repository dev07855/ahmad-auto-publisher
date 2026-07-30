#!/usr/bin/env python3
"""
الماسح: يسجّل دخول أحمد، يقرأ الأقسام الأربعة بالترتيب (فوق→تحت)، ويرسلها لعقل كلاودفلير.
كل تطبيق يُوسم بقسمه. العقل يقرر الجديد (منع تكرار بالإصدار) ويعبّي طابور كل قسم.
يعمل بجدول GitHub Actions كل بضع دقائق.

Env: AHMAD_EMAIL, AHMAD_PASSWORD, BRAIN_URL, ENQUEUE_SECRET, SECTION_LIMIT(optional)
"""
import os, sys, requests
from ahmad import Ahmad, BASE

# الأقسام تُقرأ ديناميكياً من العقل (يديرها المالك من البوت)؛ وإن تعذّر، احتياطي ثابت.
FALLBACK_SECTIONS = [
    ("updates", "/last-app-update"),
    ("games",   "/category/6"),
    ("design",  "/category/9"),
    ("modded",  "/category/7"),
]

def get_config():
    """الأقسام + عدد صفحات المسح من العقل (يتحكم فيهما المالك من البوت)."""
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

def scan_section(a, path, limit, pages):
    """يمسح صفحات القسم 1..pages (النمط ?page=N)، ويقف عند أول صفحة فاضية."""
    apps, rank = [], 0
    for pg in range(1, pages + 1):
        sep = '&' if '?' in path else '?'
        url = BASE + path + (f"{sep}page={pg}" if pg > 1 else "")
        try:
            r = a.s.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"  صفحة {pg} فشلت: {e}")
            break
        rows = a.parse_listing(r.text, limit)
        if not rows:
            break                       # صفحة فاضية = انتهت صفحات القسم
        for row in rows:
            item = {"id": row["id"], "download_url": row["download_url"], "rank": rank}
            rank += 1
            try:
                info = a.app_info(row["id"])
                item["name"] = info.get("name", "")
                item["version"] = info.get("version", "")
            except Exception:
                pass
            apps.append(item)
    return apps

def main():
    a = Ahmad()
    ok, msg = a.login(os.environ["AHMAD_EMAIL"], os.environ["AHMAD_PASSWORD"])
    if not ok:
        print("login failed:", msg); sys.exit(1)
    sections, pages_cfg = get_config()                 # الأقسام + الصفحات من العقل
    limit = int(os.environ.get("SCAN_LIMIT") or os.environ.get("SECTION_LIMIT") or "60")
    pages = pages_cfg or int(os.environ.get("SCAN_PAGES") or "3")   # من البوت، وإلا env، وإلا 3
    total = 0
    for section, path in sections:
        try:
            apps = scan_section(a, path, limit, pages)
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
