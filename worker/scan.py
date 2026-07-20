#!/usr/bin/env python3
"""
الماسح: يسجّل دخول أحمد، يقرأ الأقسام الأربعة بالترتيب (فوق→تحت)، ويرسلها لعقل كلاودفلير.
كل تطبيق يُوسم بقسمه. العقل يقرر الجديد (منع تكرار بالإصدار) ويعبّي طابور كل قسم.
يعمل بجدول GitHub Actions كل بضع دقائق.

Env: AHMAD_EMAIL, AHMAD_PASSWORD, BRAIN_URL, ENQUEUE_SECRET, SECTION_LIMIT(optional)
"""
import os, sys, requests
from ahmad import Ahmad, BASE

# الأقسام الأربعة: (المفتاح, مسار الصفحة). التحديثات = صفحة مرتّبة بالأحدث؛ البقية صفحات أقسام.
SECTIONS = [
    ("updates", "/last-app-update"),
    ("games",   "/category/6"),
    ("design",  "/category/9"),
    ("modded",  "/category/7"),
]

def scan_section(a, path, limit):
    r = a.s.get(BASE + path, timeout=30)
    r.raise_for_status()
    rows = a.parse_listing(r.text, limit)
    apps = []
    for rank, row in enumerate(rows):
        item = {"id": row["id"], "download_url": row["download_url"], "rank": rank}
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
    limit = int(os.environ.get("SECTION_LIMIT", "40"))
    total = 0
    for section, path in SECTIONS:
        try:
            apps = scan_section(a, path, limit)
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
