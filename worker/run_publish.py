#!/usr/bin/env python3
"""
غلاف عامل النشر (يُنادى من GitHub Actions عبر repository_dispatch).
يحمّل التطبيق مرة، ويحقن + ينشر لكل «مجموعة» (دايلب → قنوات)، ثم يبلّغ العقل بالنتيجة.
Env: APP_ID, DL, FOOTER, GROUPS(json) + أسرار main/telegram + BRAIN_URL, ENQUEUE_SECRET
GROUPS = [{"dylib":"اسم أو فارغ","channels":["@a","-100…"]}, ...]
"""
import os, sys, json, shutil, traceback, requests
import main as worker
import telegram

def notify(path, payload):
    try:
        requests.post(os.environ["BRAIN_URL"].rstrip("/") + path,
                      headers={"x-secret": os.environ["ENQUEUE_SECRET"]},
                      json=payload, timeout=30)
    except Exception as e:
        print("notify failed:", e)

def fetch_dylib(name):
    """اكتب الدايلب المطلوب (بالاسم، أو الفعّال إن فارغ) في مسار الحقن.
    fallback: يبقى الملف المكتوب من السر DYLIB_GZ_B64 إن تعذّر السحب."""
    path = os.environ.get("DYLIB_PATH", "fixipa.dylib")
    try:
        url = os.environ["BRAIN_URL"].rstrip("/") + "/dylib"
        if name:
            url += "?name=" + requests.utils.quote(name)
        r = requests.get(url, headers={"x-secret": os.environ["ENQUEUE_SECRET"]}, timeout=60)
        if r.status_code == 200 and r.content:
            with open(path, "wb") as f:
                f.write(r.content)
            print(f"[dylib] {name or 'الفعّال'} ({len(r.content)} bytes)")
        else:
            print(f"[dylib] لا دايلب ({name or 'الفعّال'}) بالعقل ({r.status_code}) — السر الاحتياطي")
    except Exception as e:
        print("[dylib] فشل السحب، السر الاحتياطي:", e)
    return path

def run():
    app_id = os.environ["APP_ID"]
    dl = os.environ.get("DL") or None
    footer = os.environ.get("FOOTER") or None
    # بيانات التطبيق للمنشور (يمرّرها العقل من الماسح)
    try:
        meta = json.loads(os.environ.get("META") or "{}")
    except Exception:
        meta = {}
    info = {
        "name": os.environ.get("APP_NAME") or meta.get("name") or "",
        "version": os.environ.get("APP_VERSION") or meta.get("version") or "",
        "description": meta.get("description", ""),
        "icon": meta.get("icon", ""),
        "size": meta.get("size", ""),
    }
    # المجموعات (dylib → channels)؛ احتياطي: القناة الرئيسية بالدايلب الفعّال
    try:
        groups = json.loads(os.environ.get("GROUPS") or "[]")
    except Exception:
        groups = []
    if not groups:
        main_ch = [os.environ["TG_CHANNEL"]] if os.environ.get("TG_CHANNEL") else []
        groups = [{"dylib": "", "channels": main_ch}]

    workdir = None
    try:
        raw, caption, thumb, info, workdir = worker.prepare(app_id, dl, info, footer=footer)
        published_any = False
        errors = []
        for g in groups:
            chans = [c for c in (g.get("channels") or []) if c]
            if not chans:
                continue
            dylib_path = fetch_dylib(g.get("dylib") or "")   # دايلب هذه المجموعة
            out = worker.inject_app(raw, info, dylib_path, workdir)
            try:
                cfg = telegram.cfg_from_env()
                cfg["channels"] = chans
                telegram.publish(cfg, out, caption, thumb)
                published_any = True
            except BaseException as e:
                errors.append(str(e)[:120])
                print("group publish failed:", e)
            finally:
                try: os.remove(out)
                except OSError: pass
        if published_any:
            notify("/published", {"app_id": app_id, "name": info.get("name", ""), "version": info.get("version", "")})
            print("PUBLISHED", app_id, info.get("name"), "| errors:", errors)
        else:
            raise RuntimeError("كل المجموعات فشلت: " + ("; ".join(errors) or "لا قنوات"))
    except BaseException as e:  # includes SystemExit — لازم نبلّغ دائماً حتى لا يعلق بالطابور
        traceback.print_exc()
        notify("/failed", {"app_id": app_id, "error": str(e)[:300] or type(e).__name__})
        sys.exit(1)
    finally:
        if workdir and os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)

if __name__ == "__main__":
    run()
