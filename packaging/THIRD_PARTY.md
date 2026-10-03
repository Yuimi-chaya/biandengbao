# Third-party Components

The desktop distribution includes CPython (PSF license), pywebview (BSD-3-Clause), PyInstaller's bootloader (GPL-2.0-or-later with its distribution exception) and their runtime dependencies. Windows includes Python.NET, clr-loader and CFFI; macOS includes PyObjC bindings to system frameworks. Dependency distribution metadata and available license files are bundled by the packaging hooks. System WebView2 or WebKit is used instead of shipping Chromium.

The bundled phone frontend uses Lucide (ISC), Marked (MIT), DOMPurify (Apache-2.0 or MPL-2.0) and Highlight.js (BSD-3-Clause); license texts remain in web/vendor. The management application icon adapts Lucide's Command geometry under its ISC license. This project is MIT-licensed; third-party components retain their respective licenses.

cloudflared is optional and is not distributed or silently installed by this package.
