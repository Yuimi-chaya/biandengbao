# Native-only builds: build macOS on macOS and Windows on Windows.
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata, collect_data_files

root = Path(SPECPATH).parent
metadata = Path(os.environ["BDB_BUILD_METADATA"])
icon = os.environ["BDB_BUILD_ICON"]
datas = [(str(root / "web"), "web"), (str(root / "manager-web"), "manager-web"),
         (str(root / "tools/configure-autostart.ps1"), "tools"),
         (str(root / "LICENSE"), "."), (str(root / "docs/MANAGER.md"), "docs"),
         (str(root / "docs/AUTOSTART.md"), "docs"),
         (str(root / "docs/STARTUP-MIGRATION.md"), "docs"),
         (str(root / "packaging/THIRD_PARTY.md"), "."),
         (str(metadata), ".")]
datas += copy_metadata("pywebview", recursive=True)
datas += collect_data_files("webview")
datas += copy_metadata("pyinstaller")
for candidate in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
    if candidate.is_file():
        datas.append((str(candidate), "python-license"))
        break
platform_imports = ["webview.platforms.winforms", "webview.platforms.edgechromium"] if sys.platform == "win32" else ["webview.platforms.cocoa"]
a = Analysis([str(root / "manager.py")], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=platform_imports, hookspath=[], runtime_hooks=[],
             excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3"], noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Biandengbao",
          debug=False, strip=False, upx=False, console=False, icon=icon,
          target_arch=None, codesign_identity=None, entitlements_file=None)
if sys.platform == "win32":
    cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Biandengbao-CLI",
              debug=False, strip=False, upx=False, console=True, icon=icon)
    coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="Biandengbao")
else:
    coll = COLLECT(gui, a.binaries, a.datas, strip=False, upx=False, name="Biandengbao")
    app = BUNDLE(coll, name="Biandengbao.app", icon=icon, bundle_identifier="com.biandengbao.manager",
                 info_plist={"CFBundleName": "便蹬宝", "CFBundleDisplayName": "便蹬宝",
                             "CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "0.2.0",
                             "NSHighResolutionCapable": True,
                             "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}})
