# Third-party components

FlickDock uses HarmonyOS Sans Fonts. / 本软件使用 HarmonyOS Sans 字体。

FlickDock uses unmodified Python, Qt for Python (PySide6/Shiboken), Qt and HarmonyOS Sans SC. The application icon and content-category icons are original SVG artwork. The LAN transfer, file import and cloud status glyphs use the Lucide icons noted below.

- Python 3.12: Python Software Foundation license; see licenses/Python-LICENSE.txt and https://www.python.org/downloads/source/.
- PySide6/Shiboken and Qt 6.12: used under LGPL version 3. The libraries are dynamically loaded from _internal and may be replaced by compatible builds for modification or debugging. The application uses Qt Core, GUI, Widgets, Network, SVG and Multimedia with the native Windows media backend. See licenses/LGPL-3.0.txt and licenses/GPL-3.0.txt. Upstream source: https://code.qt.io/cgit/pyside/pyside-setup.git/, https://code.qt.io/cgit/qt/qtbase.git/, https://code.qt.io/cgit/qt/qtsvg.git/ and https://code.qt.io/cgit/qt/qtmultimedia.git/.
- HarmonyOS Sans SC Regular: Copyright 2021 Huawei Device Co., Ltd. The original, unmodified Regular font is bundled with this application under the HarmonyOS Sans Fonts License Agreement. Qt synthesizes display weights; the font file is not modified. See licenses/HarmonyOS-Sans-LICENSE.txt and https://github.com/openharmony/resources/blob/master/LICENSE_Fonts. The font is not distributed as standalone font software. Source and checksum are recorded in font_manifest.json.
- PyInstaller bootloader: GPL with bootloader exception; see licenses/PyInstaller-COPYING.txt and https://github.com/pyinstaller/pyinstaller.
- Lucide `monitor-smartphone`, `file-up` and `cloud` icons: Copyright (c) 2026 Lucide Icons and Contributors, ISC License. Their SVG paths are embedded for crisp rendering at any display scale. See licenses/Lucide-LICENSE.txt, https://lucide.dev/icons/monitor-smartphone, https://lucide.dev/icons/file-up and https://lucide.dev/icons/cloud.

The application loads its bundled HarmonyOS Sans SC family; Apple font files are not included.

Cloud account credentials use keyring 25.7.0 with the native Windows Credential Manager or macOS Keychain backend. keyring, jaraco dependencies, more-itertools and pywin32-ctypes retain their original licenses in licenses/Cloud-Client-LICENSES.txt. CPython's native scrypt implementation requires OpenSSL libcrypto under the Apache License 2.0; see licenses/OpenSSL-LICENSE.txt. No credential service secrets are distributed with the client.

Media decoding uses Windows-installed codecs through Windows Media Foundation. The distribution does not include the Qt FFmpeg media plugin or FFmpeg codec DLLs.
