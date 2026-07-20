#!/usr/bin/env python3
"""
الماسح: يسجّل دخول أحمد، يقرأ "تم تحديثها مؤخراً" بالترتيب، ويرسلها لعقل كلاودفلير.
العقل يقرر الجديد ويعبّي الطابور (منع تكرار + قائمة سوداء).
يعمل بجدول GitHub Actions كل بضع دقائق.

Env: AHMAD_EMAIL, AHMAD_PASSWORD, BRAIN_URL, ENQUEUE_SECRET, SCAN_LIMIT(optional)
"""
import os, sys, requests
from ahmad import Ahmad

def main():
    a = Ahmad()
    ok, msg = a.login(os.environ["AHMAD_EMAIL"], os.environ["AHMAD_PASSWORD"])
    if not ok:
        print("login failed:", msg); sys.exit(1)
    limit = int(os.environ.get("SCAN_LIMIT", "60"))
    rows = a.list_recent(limit=limit)
    # enrich with name/version from public app-info (best-effort)
    apps = []
    for rank, r in enumerate(rows):
        item = {"id": r["id"], "download_url": r["download_url"], "rank": rank}
        try:
            info = a.app_info(r["id"])
            item["name"] = info.get("name", "")
            item["version"] = info.get("version", "")
        except Exception:
            pass
        apps.append(item)
    resp = requests.post(os.environ["BRAIN_URL"].rstrip("/") + "/enqueue",
                         headers={"x-secret": os.environ["ENQUEUE_SECRET"]},
                         json={"apps": apps}, timeout=60)
    print("enqueue:", resp.status_code, resp.text[:200])

if __name__ == "__main__":
    main()
