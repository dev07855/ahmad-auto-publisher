#!/usr/bin/env python3
"""
غلاف عامل النشر (يُنادى من GitHub Actions عبر repository_dispatch).
يعالج التطبيق، ينشره، ويبلّغ عقل كلاودفلير بالنتيجة (/published أو /failed).
Env: APP_ID, DL, FOOTER(optional) + أسرار main/telegram + BRAIN_URL, ENQUEUE_SECRET
"""
import os, sys, shutil, traceback, requests
import main as worker
import telegram

def notify(path, payload):
    try:
        requests.post(os.environ["BRAIN_URL"].rstrip("/") + path,
                      headers={"x-secret": os.environ["ENQUEUE_SECRET"]},
                      json=payload, timeout=30)
    except Exception as e:
        print("notify failed:", e)

def fetch_active_dylib():
    """اسحب الدايلب الفعّال (اللي اختاره المالك من البوت) من العقل.
    fallback: إن تعذّر أو ما فيه فعّال، يبقى الملف المكتوب من السر DYLIB_GZ_B64."""
    try:
        r = requests.get(os.environ["BRAIN_URL"].rstrip("/") + "/dylib",
                         headers={"x-secret": os.environ["ENQUEUE_SECRET"]}, timeout=60)
        if r.status_code == 200 and r.content:
            path = os.environ.get("DYLIB_PATH", "fixipa.dylib")
            with open(path, "wb") as f:
                f.write(r.content)
            print(f"[dylib] الفعّال من العقل ({len(r.content)} bytes)")
        else:
            print(f"[dylib] العقل بلا دايلب فعّال ({r.status_code}) — استخدام السر الاحتياطي")
    except Exception as e:
        print("[dylib] فشل سحب العقل، استخدام السر الاحتياطي:", e)

def run():
    app_id = os.environ["APP_ID"]
    dl = os.environ.get("DL") or None
    footer = os.environ.get("FOOTER") or None  # per-run footer from the brain (falls back to env)
    channels = [c.strip() for c in (os.environ.get("CHANNELS") or "").split(",") if c.strip()]
    workdir = None
    try:
        fetch_active_dylib()   # الدايلب الفعّال من العقل قبل المعالجة (يتخطّى السر لو موجود)
        out, caption, thumb, info = worker.process(app_id, dl, footer=footer)
        workdir = os.path.dirname(out)
        cfg = telegram.cfg_from_env()
        cfg["channels"] = channels   # قنوات النشر (فارغة = القناة الرئيسية TG_CHANNEL)
        telegram.publish(cfg, out, caption, thumb)
        notify("/published", {"app_id": app_id, "name": info.get("name", ""), "version": info.get("version", "")})
        print("PUBLISHED", app_id, info.get("name"))
    except BaseException as e:  # includes SystemExit — MUST always report so nothing sticks in the queue
        traceback.print_exc()
        notify("/failed", {"app_id": app_id, "error": str(e)[:300] or type(e).__name__})
        sys.exit(1)
    finally:
        if workdir and os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)

if __name__ == "__main__":
    run()
