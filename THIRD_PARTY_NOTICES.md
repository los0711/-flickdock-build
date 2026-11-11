# Third-party components

- Simple Icons Apple mark: CC0-1.0, https://github.com/simple-icons/simple-icons/blob/develop/icons/apple.svg and https://github.com/simple-icons/simple-icons/blob/develop/LICENSE.md. Apple is a trademark of Apple Inc.; used only to identify a discovered Mac.

FlickDock uses HarmonyOS Sans Fonts. / 本软件使用 HarmonyOS Sans 字体。

FlickDock uses unmodified Python, Qt for Python (PySide6/Shiboken), Qt and HarmonyOS Sans SC. The application icon and content-category icons are original SVG artwork. The LAN transfer, file import and cloud status glyphs use the Lucide icons noted below.

- Python 3.12: Python Software Foundation license; see licenses/Python-LICENSE.txt and https://www.python.org/downloads/source/.
- PySide6/Shiboken and Qt 6.12: used under LGPL version 3. The libraries are dynamically loaded from _internal and may be replaced by compatible builds for modification or debugging. The application uses Qt Core, GUI, Widgets, Network, SVG and Multimedia with Qt’s FFmpeg backend and native video rendering on Windows. See licenses/LGPL-3.0.txt and licenses/GPL-3.0.txt. Upstream source: https://code.qt.io/cgit/pyside/pyside-setup.git/, https://code.qt.io/cgit/qt/qtbase.git/, https://code.qt.io/cgit/qt/qtsvg.git/ and https://code.qt.io/cgit/qt/qtmultimedia.git/.
- HarmonyOS Sans SC Regular: Copyright 2021 Huawei Device Co., Ltd. The original, unmodified Regular font is bundled with this application under the HarmonyOS Sans Fonts License Agreement. Qt synthesizes display weights; the font file is not modified. See licenses/HarmonyOS-Sans-LICENSE.txt and https://github.com/openharmony/resources/blob/master/LICENSE_Fonts. The font is not distributed as standalone font software. Source and checksum are recorded in font_manifest.json.
- PyInstaller bootloader: GPL with bootloader exception; see licenses/PyInstaller-COPYING.txt and https://github.com/pyinstaller/pyinstaller.
- Lucide `monitor-smartphone`, `file-up` and `cloud` icons: Copyright (c) 2026 Lucide Icons and Contributors, ISC License. Their SVG paths are embedded for crisp rendering at any display scale. See licenses/Lucide-LICENSE.txt, https://lucide.dev/icons/monitor-smartphone, https://lucide.dev/icons/file-up and https://lucide.dev/icons/cloud.

The application loads its bundled HarmonyOS Sans SC family; Apple font files are not included.


Cloud account credentials use keyring 25.7.0 with the native Windows Credential Manager or macOS Keychain backend. keyring, jaraco dependencies, more-itertools and pywin32-ctypes retain their original licenses in licenses/Cloud-Client-LICENSES.txt. CPython's native scrypt implementation requires OpenSSL libcrypto under the Apache License 2.0; see licenses/OpenSSL-LICENSE.txt. No credential service secrets are distributed with the client.

Media previews use the Qt FFmpeg plugin on Windows and Qt's Darwin backend on macOS. Qt multimedia libraries remain separate and replaceable.

Manual remote file transfer uses node-datachannel 0.33.6 and libdatachannel 0.24.6 under MPL 2.0, with their original dependencies and licenses in `licenses/WebRTC-LICENSES.txt` and `p2p/node_modules`. Source: https://github.com/murat-dogan/node-datachannel/tree/v0.33.6 and https://github.com/paullouisageneau/libdatachannel/tree/v0.24.6.

The unmodified Node.js 24.12.0 runtime runs the WebRTC bridge and video JavaScript challenges. Complete notices are bundled at `p2p/runtime/Node-LICENSE.txt`.

Publisher signature verification uses cryptography 50.0.2 (Apache-2.0 / BSD-3-Clause). Complete licenses are included in licenses/Release-Crypto-LICENSES.txt.

- FFmpeg 9.0.1 shared libraries supplied by Qt for Windows: LGPL 2.1 or later, built without GPL/nonfree components. Libraries remain separate and replaceable in `_internal/PySide6`. See licenses/FFmpeg-LICENSE.txt. Exact upstream source: https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz ; build configuration is recorded in PACKAGE_AUDIT.md. macOS retains Qt’s Darwin backend.


视频解析使用 yt-dlp 2026.10.26（Unlicense）与 yt-dlp-ejs 0.8.0，复用应用现有 Node 24.12.0 运行时，不远程执行附加组件。推荐依赖的许可证汇总于 licenses/Video-LICENSES.txt。

下载与音视频合并使用 imageio-ffmpeg 0.6.0 平台轮子提供的独立 FFmpeg 7.1 可执行文件（GPL）。首次下载时按固定归档和可执行文件 SHA256 校验后存入应用数据目录，不随安装包重复分发，也不替换 Qt 播放器的动态库。二进制、对应源码与构建说明：https://github.com/imageio/imageio-ffmpeg 。原版 GPL-3.0 许可保留于 licenses/Video-LICENSES.txt。

Relay SOCKS proxy connections use socks 2.8.10 (MIT), ip-address and smart-buffer (MIT). Original notices are bundled with each module in `p2p/node_modules`. Source: https://github.com/JoshGlazebrook/socks . HTTP CONNECT uses the existing Node.js standard library.

## Translation language detection and flags

ELDC 0.4.0 (https://github.com/nitotm/eldc), Apache-2.0; see licenses/ELDC-LICENSE.txt.
Flag-icons 7.6.0 (https://github.com/lipis/flag-icons), MIT; see licenses/Flag-Icons-LICENSE.txt.
Source-language detection runs locally; flags are bundled static resources.

Notice fusion uses circular-cap Bézier connections following the Paper.js v0.12.18 Meta Balls example (MIT), originally by SATO Hiroyuki; rendered with existing Qt paths without embedding Paper.js. Source: https://github.com/paperjs/paper.js/blob/v0.12.18/examples/Paperjs.org/MetaBalls.html ; license: licenses/Paper-Notice-Motion-LICENSE.txt.
