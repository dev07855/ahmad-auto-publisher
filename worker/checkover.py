#!/usr/bin/env python3
"""
CheckOver (check0ver.net) client — مصدر التطبيقات (بديل أحمد).
اكتشافات مؤكّدة (سبتمبر 2026):
- تسجيل الدخول: POST /ar/auth/login بحقلي username(=الإيميل)+password، مع CSRF (كوكي XSRF-TOKEN → ترويسة X-XSRF-TOKEN).
- قائمة تطبيقات قسم: GET /ar/iapps?filter[inCategories][0]=<uuid>&page=N  (JSON: response.data[], مرتّبة بالأحدث تحديثاً أولاً).
  كل تطبيق: uuid, name, description, version, size("X MB"), image(أيقونة CDN), downloadURL, isFree ...
- downloadURL: رابط IPA موقّع يعمل *بدون جلسة* (206/PK) — يمرَّر للعامل ليحمّله مباشرة (زي blob أحمد).
- التصنيفات: GET /api/categories.
"""
import urllib.parse
import requests

BASE = "https://check0ver.net"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")


class CheckOver:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"})

    def _xsrf(self):
        tok = self.s.cookies.get("XSRF-TOKEN")
        return urllib.parse.unquote(tok) if tok else ""

    # ---- auth ----
    def login(self, username, password):
        """جلسة Laravel: صفحة تعطي كوكي CSRF ثم POST بحقلي username+password."""
        self.s.get(f"{BASE}/ar", timeout=30)          # يضبط XSRF-TOKEN + _session
        xsrf = self._xsrf()
        if not xsrf:
            return False, "no CSRF cookie"
        r = self.s.post(
            f"{BASE}/ar/auth/login", timeout=30,
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                     "X-XSRF-TOKEN": xsrf, "Referer": f"{BASE}/ar", "Content-Type": "application/json"},
            json={"username": username, "password": password},
        )
        if r.status_code in (200, 201, 204, 302):
            return True, "ok"
        try:
            msg = r.json().get("message", "")
        except Exception:
            msg = r.text[:150]
        return False, f"{r.status_code}: {msg}"

    # ---- listing (needs auth for downloadURL) ----
    def list_apps(self, category_uuid, page=1):
        """تطبيقات قسم (مرتّبة بالأحدث تحديثاً). يرجّع (apps, meta)."""
        r = self.s.get(
            f"{BASE}/ar/iapps", timeout=30,
            params={"filter[inCategories][0]": category_uuid, "page": page},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )
        r.raise_for_status()
        resp = (r.json() or {}).get("response") or {}
        return (resp.get("data") or []), (resp.get("meta") or {})

    def categories(self):
        r = self.s.get(f"{BASE}/api/categories", timeout=30,
                       headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        r.raise_for_status()
        j = r.json() or {}
        data = (j.get("response") or {}).get("data") or j.get("data") or []
        return [{"uuid": c.get("uuid"), "name": c.get("name"),
                 "count": c.get("iappsCount") or c.get("count")} for c in data]

    # ---- download (downloadURL is pre-signed; NO auth needed) ----
    def download(self, url, dest, verify_ipa=False):
        with self.s.get(url, stream=True, timeout=180) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    done += len(chunk)
        if done == 0:
            raise RuntimeError("DEAD_APP: 0-byte file on server")
        if total and done != total:
            raise RuntimeError(f"download truncated: got {done} of {total} bytes")
        if verify_ipa:
            with open(dest, "rb") as f:
                if f.read(4) != b"PK\x03\x04":
                    raise RuntimeError("downloaded file is not an IPA")
        return dest, total, done


def clean_name(name, version):
    import re
    safe = re.sub(r'[^\w .-]', '', name or '', flags=re.UNICODE).strip().replace(' ', '_')
    ver = re.sub(r'[^\w.-]', '', version or '')
    if not safe:
        safe = "app"
    return f"{safe}-{ver}.ipa" if ver else f"{safe}.ipa"


if __name__ == "__main__":
    import os, sys, json
    c = CheckOver()
    ok, msg = c.login(os.environ["CHECKOVER_USER"], os.environ["CHECKOVER_PASS"])
    print("login:", ok, msg)
    if ok:
        cat = sys.argv[1] if len(sys.argv) > 1 else "9c60f563-1983-42f0-8882-a26207bd4aaf"
        apps, meta = c.list_apps(cat, 1)
        print("total:", meta.get("total"), "page apps:", len(apps))
        if apps:
            a = apps[0]
            print(json.dumps({k: a.get(k) for k in ("uuid", "name", "version", "size")}, ensure_ascii=False))
