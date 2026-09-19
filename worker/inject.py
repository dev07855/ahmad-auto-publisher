#!/usr/bin/env python3
# Cross-platform IPA dylib injector (runs identically on Linux/GitHub Actions).
# Method mirrors the user's own output: dylib at .app root + LC_LOAD_WEAK_DYLIB
# @executable_path/<name>. Proven on device (arm64e dylib into arm64 apps, re-signed by ESign).
import sys, os, zipfile, shutil, plistlib, tempfile
import lief


def find_app(payload):
    for n in os.listdir(payload):
        if n.endswith(".app"):
            return os.path.join(payload, n)
    raise RuntimeError("no .app found inside Payload")


def main(ipa_in, dylib, ipa_out):
    work = tempfile.mkdtemp(prefix="inj_")
    try:
        with zipfile.ZipFile(ipa_in) as z:
            z.extractall(work)
        payload = os.path.join(work, "Payload")
        if not os.path.isdir(payload):
            raise RuntimeError("IPA has no Payload folder (not a valid app archive)")
        app = find_app(payload)

        plist_path = os.path.join(app, "Info.plist")
        with open(plist_path, "rb") as f:
            info = plistlib.load(f)
        exe = info.get("CFBundleExecutable")
        if not exe:
            raise RuntimeError("Info.plist has no CFBundleExecutable")
        exe_path = os.path.join(app, exe)
        if not os.path.isfile(exe_path):
            raise RuntimeError(f"main executable '{exe}' not found in app bundle")

        # دايلبات البرandة (المصدر) اللي نشيلها قبل حقن دايلبنا — يبقى الهاك والفريمويركات
        strip = set(x.strip() for x in os.environ.get(
            "STRIP_DYLIBS", "Check0verPlus.dylib").split(",") if x.strip())

        # 1) copy our dylib to .app root, named after its install-id basename
        dyl_name = "ThamerScreen.dylib"
        dst = os.path.join(app, dyl_name)
        shutil.copy(dylib, dst)
        os.chmod(dst, 0o644)

        # 2) بكل شريحة: احذف أوامر تحميل دايلبات البرandة، ثم أضف أمر تحميل دايلبنا (weak)
        load_path = f"@executable_path/{dyl_name}"
        binary = lief.MachO.parse(exe_path)
        slices = [binary.at(i) for i in range(binary.size)] if hasattr(binary, "size") else [binary]
        added = False; removed = 0
        for b in slices:
            for lib in list(b.libraries):                       # نسخة للتكرار الآمن أثناء الحذف
                if lib.name.split('/')[-1] in strip:
                    b.remove(lib); removed += 1
            if load_path not in [c.name for c in b.libraries]:
                b.add(lief.MachO.DylibCommand.weak_lib(load_path))
                added = True
        binary.write(exe_path)
        os.chmod(exe_path, 0o755)

        # احذف ملفات دايلبات البرandة نفسها من جذر التطبيق
        for name in strip:
            p = os.path.join(app, name)
            if os.path.isfile(p):
                os.remove(p)
                print(f"[strip] removed {name}")

        # 3) repackage (preserve tree; symlinks are rare in IPAs and re-signing handles the rest)
        if os.path.exists(ipa_out):
            os.remove(ipa_out)
        # ضغط سريع (level=1): محتوى IPA مضغوط أصلاً، فالمستوى العالي يضيّع وقتاً بلا فائدة تُذكر
        with zipfile.ZipFile(ipa_out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for root, _, files in os.walk(payload):
                for fn in files:
                    fp = os.path.join(root, fn)
                    z.write(fp, os.path.relpath(fp, work))
        print(f"OK stripped={removed} injected={added} exe={exe} -> {ipa_out}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: inject.py <in.ipa> <dylib> <out.ipa>")
    main(sys.argv[1], sys.argv[2], sys.argv[3])
