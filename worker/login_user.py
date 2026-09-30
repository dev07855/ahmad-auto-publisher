#!/usr/bin/env python3
"""
تسجيل دخول حساب مستخدم تلقرام مرة واحدة → يطبع StringSession.
يُشغّله المالك بنفسه (تفاعلي): يدخل رقمه + الكود، ويطلع «الجلسة» تُحفظ كسرّ.
لا يُخزّن كلمة السر ولا الكود بأي مكان.

التشغيل:  API_ID=... API_HASH=... python3 worker/login_user.py
"""
import os, getpass
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = os.environ.get("API_ID") or input("api_id: ").strip()
api_hash = os.environ.get("API_HASH") or input("api_hash: ").strip()

with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    s = client.session.save()
    me = client.get_me()
    print("\n================= انسخ السطر التالي كامل =================")
    print(s)
    print("=========================================================")
    print(f"\n✅ تم الدخول كـ: {getattr(me,'first_name','')} (@{getattr(me,'username','')})")
    print("أرسل السطر الطويل أعلاه — يُحفظ كسرّ TG_USER_SESSION.")
