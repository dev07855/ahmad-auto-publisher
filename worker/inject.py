#!/usr/bin/env python3
# Cross-platform IPA dylib injector (runs identically on Linux/GitHub Actions).
# Method mirrors the user's own output: dylib at .app root + LC_LOAD_DYLIB @executable_path/<name> (weak)
import sys, os, zipfile, shutil, plistlib, tempfile, subprocess
import lief

def find_app(payload):
    for n in os.listdir(payload):
        if n.endswith(".app"):
            return os.path.join(payload, n)
    raise SystemExit("no .app found")

def main(ipa_in, dylib, ipa_out):
    work = tempfile.mkdtemp(prefix="inj_")
    with zipfile.ZipFile(ipa_in) as z: z.extractall(work)
    app = find_app(os.path.join(work, "Payload"))
    with open(os.path.join(app, "Info.plist"), "rb") as f:
        info = plistlib.load(f)
    exe = info["CFBundleExecutable"]
    exe_path = os.path.join(app, exe)

    # 1) copy dylib to .app root, named after its install-id basename
    dyl_name = "ThamerScreen.dylib"
    shutil.copy(dylib, os.path.join(app, dyl_name))

    # 2) add load command via LIEF (weak, @executable_path)
    load_path = f"@executable_path/{dyl_name}"
    bin = lief.MachO.parse(exe_path)
    # handle fat: take arm64/arm64e slice(s)
    binaries = bin if hasattr(bin, "__len__") else [bin]
    added=False
    for b in ([bin.at(i) for i in range(bin.size)] if hasattr(bin,'size') else [bin]):
        names=[c.name for c in b.libraries]
        if load_path not in names:
            cmd = lief.MachO.DylibCommand.weak_lib(load_path)  # LC_LOAD_WEAK_DYLIB (matches user's method)
            b.add(cmd)
            added=True
    bin.write(exe_path)
    os.chmod(exe_path, 0o755)

    # 3) repackage
    if os.path.exists(ipa_out): os.remove(ipa_out)
    base=os.path.join(work)
    with zipfile.ZipFile(ipa_out,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for root,_,files in os.walk(os.path.join(work,"Payload")):
            for fn in files:
                fp=os.path.join(root,fn)
                z.write(fp, os.path.relpath(fp, work))
    shutil.rmtree(work, ignore_errors=True)
    print(f"OK injected={added} exe={exe} -> {ipa_out}")

if __name__=="__main__":
    main(*sys.argv[1:4])
