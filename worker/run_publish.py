#!/usr/bin/env python3
"""
غلاف عامل النشر (يُنادى من GitHub Actions عبر repository_dispatch).
يعالج التطبيق، ينشره، ويبلّغ عقل كلاودفلير بالنتيجة (/published أو /failed).
Env: APP_ID, DL, + أسرار main/telegram + BRAIN_URL, ENQUEUE_SECRET
"""
import os, sys, traceback, requests
import main as worker
import telegram

def notify(path, payload):
    try:
        requests.post(os.environ["BRAIN_URL"].rstrip("/") + path,
                      headers={"x-secret": os.environ["ENQUEUE_SECRET"]},
                      json=payload, timeout=30)
    except Exception as e:
        print("notify failed:", e)

def run():
    app_id = os.environ["APP_ID"]
    dl = os.environ.get("DL") or None
    try:
        out, caption, photos, info = worker.process(app_id, dl)
        telegram.publish(telegram.cfg_from_env(), out, caption, photos)
        notify("/published", {"app_id": app_id, "name": info.get("name", ""), "version": info.get("version", "")})
        print("PUBLISHED", app_id, info.get("name"))
    except Exception as e:
        traceback.print_exc()
        notify("/failed", {"app_id": app_id, "error": str(e)[:300]})
        sys.exit(1)

if __name__ == "__main__":
    run()
