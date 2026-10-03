#!/usr/bin/env python3
"""Build unsigned, self-contained native release archives without runtime data."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge.version import VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-dirty", action="store_true", help="Only for local smoke builds")
    args = parser.parse_args()
    if sys.platform not in ("win32", "darwin"):
        parser.error("Build on Windows or macOS; cross-compilation is not supported")
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(ROOT), *arguments], text=True).strip()
    dirty = bool(git("status", "--porcelain"))
    if dirty and not args.allow_dirty:
        parser.error("Commit the scoped source first; release builds require a clean working tree")
    staging = ROOT / ".tmp/native-package"
    staging.mkdir(parents=True, exist_ok=True)
    metadata = staging / "build-info.json"
    metadata.write_text(json.dumps({"version": VERSION, "revision": git("rev-parse", "HEAD"),
        "dirty": dirty, "builtAt": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform + "-" + platform.machine()}, indent=2), encoding="utf-8")
    if sys.platform == "win32":
        png = (ROOT / "assets/manager-icon-256.png").read_bytes()
        icon = staging / "manager.ico"
        icon.write_bytes(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22) + png)
    else:
        iconset = staging / "manager.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = "icon_%dx%d%s.png" % (size, size, "@2x" if scale == 2 else "")
                subprocess.run(["/usr/bin/sips", "-z", str(size * scale), str(size * scale),
                    str(ROOT / "assets/manager-icon.png"), "--out", str(iconset / name)], check=True, stdout=subprocess.DEVNULL)
        icon = staging / "manager.icns"
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(icon)], check=True)
    environment = dict(os.environ, BDB_BUILD_METADATA=str(metadata), BDB_BUILD_ICON=str(icon))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / ".tmp/pyinstaller"),
        str(ROOT / "packaging/manager.spec")], cwd=ROOT, env=environment, check=True)
    architecture = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    name = "Biandengbao-%s-%s-%s" % (VERSION, "Windows" if sys.platform == "win32" else "macOS", architecture)
    releases = ROOT / "release-dist"
    releases.mkdir(exist_ok=True)
    target = releases / (name + ".zip")
    if sys.platform == "win32":
        shutil.make_archive(str(target.with_suffix("")), "zip", ROOT / "dist", "Biandengbao")
    else:
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(ROOT / "dist/Biandengbao.app"), str(target)], check=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(digest + "  " + target.name + "\n", encoding="ascii")
    print(json.dumps({"archive": str(target), "sha256": digest, "bytes": target.stat().st_size, "dirty": dirty}))


if __name__ == "__main__":
    main()
