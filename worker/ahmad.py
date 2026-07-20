#!/usr/bin/env python3
"""
Ahmad-up.com client.
- login(): programmatic session login (email+password+CSRF token)
- list_recent(): read "تم تحديثها مؤخراً" in order → [{id, download_url}]
- app_info(id): public metadata (no auth) → name, version, desc, icon, size...
- download(url, dest): public IPA download (no auth)
"""
import re, os, html, json
import requests

BASE = "https://ahmad-up.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

class Ahmad:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"})

    # ---- auth ----
    def _csrf(self):
        r = self.s.get(f"{BASE}/sign-in", timeout=30)
        r.raise_for_status()
        m = re.search(r'name="token"[^>]*value="([^"]+)"', r.text) or \
            re.search(r'value="([^"]+)"[^>]*name="token"', r.text) or \
            re.search(r'id="token"[^>]*value="([^"]+)"', r.text)
        return m.group(1) if m else ""

    def login(self, email, password):
        token = self._csrf()
        data = {"email": email, "password": password, "token": token, "login": "1"}
        r = self.s.post(f"{BASE}/users/login-member", data=data, timeout=30,
                        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/sign-in"})
        # verify by loading a gated page and checking we are NOT bounced to sign-in
        chk = self.s.get(f"{BASE}/last-app-update", timeout=30)
        ok = "download/link" in chk.text
        return ok, (r.text or "")[:200]

    # ---- listing ----
    def list_recent(self, limit=None):
        """Return recently-updated apps in page order (top=newest): [{id, download_url}]."""
        r = self.s.get(f"{BASE}/last-app-update", timeout=30)
        r.raise_for_status()
        return self.parse_listing(r.text, limit)

    @staticmethod
    def parse_listing(page_html, limit=None):
        """Pair each app with the FIRST download link that appears AFTER its detail link.
        Card layout in the HTML is: [ID, ID, DL, DL] per app, in page order (top=newest).
        """
        out, seen = [], set()
        detail_iter = list(re.finditer(r'ref\?app=(\d+)', page_html))
        dl_iter = [(m.start(), m.group(1)) for m in re.finditer(r'download/link/([A-Za-z0-9=]+)', page_html)]
        for dm in detail_iter:
            app_id = dm.group(1)
            if app_id in seen:
                continue
            pos = dm.start()
            # first download link occurring after this app's detail link
            nxt = next((b for (i, b) in dl_iter if i > pos), None)
            if not nxt:
                continue
            seen.add(app_id)
            out.append({"id": app_id, "download_url": f"{BASE}/download/link/{nxt}"})
            if limit and len(out) >= limit:
                break
        return out

    # ---- public metadata (no auth) ----
    def app_info(self, app_id):
        r = self.s.get(f"{BASE}/api/app-info/{app_id}", timeout=30)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(f"app-info failed for {app_id}: {j}")
        return j["app"]

    # ---- public download (no auth) ----
    def download(self, url, dest, progress=False):
        with self.s.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    done += len(chunk)
            return dest, total, done


def clean_name(name, version):
    safe = re.sub(r'[^\w؀-ۿ .-]', '', name).strip().replace(' ', '_')
    return f"{safe}-{version}.ipa" if version else f"{safe}.ipa"


if __name__ == "__main__":
    import sys
    a = Ahmad()
    # quick self-test of public endpoints (no login needed)
    info = a.app_info(sys.argv[1] if len(sys.argv) > 1 else "4538")
    print(json.dumps({k: info[k] for k in ("id","name","version","size") if k in info}, ensure_ascii=False))
