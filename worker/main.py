#!/usr/bin/env python3
"""نواة عامل النشر — الأدوات المستخدمة فعلياً من القارئ tg_source.

المصدر الوحيد هو قناة تلقرام (يقرأها tg_source عبر userbot، ينزّل الملف المرفق،
يحقن الدايلب، وينشر). لا مصادر ويب (تشيك أوفر / أحمد-أب) — أُزيلت بالكامل.
هذا الملف يوفّر فقط: حدّ حجم تلقرام + اسم ملف نظيف + الحقن.
"""
import os, re
import inject as injector

TG_MAX_BYTES = 2_095_000_000  # حد تلقرام للرفع عبر البوت ≈ 2 جيجا


def clean_name(name, version):
    """اسم ملف آمن من اسم التطبيق (+ الإصدار إن وُجد)."""
    safe = re.sub(r'[^\w .-]', '', name or '', flags=re.UNICODE).strip().replace(' ', '_')
    ver = re.sub(r'[^\w.-]', '', version or '')
    if not safe:
        safe = "app"
    return f"{safe}-{ver}.ipa" if ver else f"{safe}.ipa"


def inject_app(raw_ipa, info, dylib_path, work):
    """حقن دايلب محدّد وإخراج IPA جاهز للنشر (بوابة إلزامية: لا نشر بلا حقن)."""
    out = os.path.join(work, clean_name(info.get("name"), info.get("version", "")))
    injector.main(raw_ipa, dylib_path, out)
    print(f"[inject] {os.path.basename(dylib_path)} -> {os.path.basename(out)}")
    return out
