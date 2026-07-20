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
    def _hidden_fields(self):
        """Grab ALL hidden inputs from the sign-in form (the site ships two dynamic
        ones: `token` = CSRF, and `login` = a per-page signed value). Both are required.
        Supports single/double quotes and HTML-unescapes the values."""
        r = self.s.get(f"{BASE}/sign-in", timeout=30)
        r.raise_for_status()
        # narrow to the sign-in form when possible, so unrelated hidden inputs are ignored
        m = re.search(r'<form[^>]*login-member.*?</form>', r.text, re.I | re.S) \
            or re.search(r'<form[^>]*id=["\']loginMembers["\'].*?</form>', r.text, re.I | re.S)
        scope = m.group(0) if m else r.text
        fields = {}
        for tag in re.finditer(r'<input[^>]*type=["\']hidden["\'][^>]*>', scope, re.I):
            t = tag.group(0)
            nm = re.search(r'name=["\']([^"\']+)["\']', t)
            vl = re.search(r'value=["\']([^"\']*)["\']', t)
            if nm:
                fields[nm.group(1)] = html.unescape(vl.group(1)) if vl else ""
        return fields

    def login(self, email, password):
        data = self._hidden_fields()          # includes token + login (correct values)
        if "token" not in data:
            return False, "sign-in form / CSRF token not found"
        data["email"] = email
        data["password"] = password
        r = self.s.post(f"{BASE}/users/login-member", data=data, timeout=30,
                        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/sign-in"})
        # verify: a gated page must both contain download links AND not bounce to sign-in
        chk = self.s.get(f"{BASE}/last-app-update", timeout=30)
        ok = ("download/link/" in chk.text) and ("/sign-in" not in chk.url)
        return ok, re.sub(r'<[^>]+>', ' ', (r.text or ""))[:200].strip()

    # ---- listing ----
    def list_recent(self, limit=None):
        """Return recently-updated apps in page order (top=newest): [{id, download_url}]."""
        r = self.s.get(f"{BASE}/last-app-update", timeout=30)
        r.raise_for_status()
        return self.parse_listing(r.text, limit)

    @staticmethod
    def parse_listing(page_html, limit=None):
        """Pair each app with the download link that belongs to ITS card only.

        Card layout in page order is: [ID, ID, DL, DL] per app (top = newest). The
        download link for app N is the first DL that appears AFTER app N's detail link
        AND BEFORE the next distinct app's detail link. If a card has no download link
        (e.g. a restricted item), that app is SKIPPED — never paired with a neighbour's
        link (which would publish the wrong IPA under this app's name).
        """
        out, seen = [], set()
        # base64 / base64url token — must allow + / - _ and =, not just alnum
        dl_iter = [(m.start(), m.group(1))
                   for m in re.finditer(r'download/link/([A-Za-z0-9+/=_-]+)', page_html)]
        details = list(re.finditer(r'ref\?app=(\d+)', page_html))
        for idx, dm in enumerate(details):
            app_id = dm.group(1)
            if app_id in seen:
                continue
            start = dm.start()
            # boundary = position of the next DIFFERENT app id (card end)
            end = len(page_html)
            for nxt in details[idx + 1:]:
                if nxt.group(1) != app_id:
                    end = nxt.start()
                    break
            blob = next((b for (i, b) in dl_iter if start < i < end), None)
            seen.add(app_id)
            if not blob:
                continue  # no link inside this card → skip, do NOT borrow a neighbour's
            out.append({"id": app_id, "download_url": f"{BASE}/download/link/{blob}"})
            if limit and len(out) >= limit:
                break
        return out

    # ---- public metadata (no auth) ----
    def app_info(self, app_id):
        r = self.s.get(f"{BASE}/api/app-info/{app_id}", timeout=30)
        r.raise_for_status()
        j = r.json()
        if not j.get("success") or not j.get("app"):
            raise RuntimeError(f"app-info failed for {app_id}: {str(j)[:200]}")
        return j["app"]

    # ---- public download (no auth) ----
    def download(self, url, dest, verify_ipa=False):
        with self.s.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    done += len(chunk)
        # empty file on Ahmad's side (0 bytes) = permanently broken app → mark DEAD (skip, no retry)
        if done == 0:
            raise RuntimeError("DEAD_APP: 0-byte file on server (broken app)")
        # integrity: truncated download?
        if total and done != total:
            raise RuntimeError(f"download truncated: got {done} of {total} bytes ({url[:60]})")
        # sanity: an IPA must be a real ZIP (PK\x03\x04). If the session expired the
        # server returns an HTML sign-in page with HTTP 200 — catch that here.
        if verify_ipa:
            with open(dest, "rb") as f:
                if f.read(4) != b"PK\x03\x04":
                    raise RuntimeError("downloaded file is not an IPA (bad session or dead link?)")
        return dest, total, done


def clean_name(name, version):
    safe = re.sub(r'[^\w .-]', '', name or '', flags=re.UNICODE).strip().replace(' ', '_')
    ver = re.sub(r'[^\w.-]', '', version or '')
    if not safe:
        safe = "app"
    return f"{safe}-{ver}.ipa" if ver else f"{safe}.ipa"


if __name__ == "__main__":
    import sys
    a = Ahmad()
    # quick self-test of public endpoints (no login needed)
    info = a.app_info(sys.argv[1] if len(sys.argv) > 1 else "4538")
    print(json.dumps({k: info[k] for k in ("id","name","version","size") if k in info}, ensure_ascii=False))
