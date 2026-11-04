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

Media decoding uses Windows-installed codecs through Windows Media Foundation. The distribution does not include the Qt FFmpeg media plugin or FFmpeg codec DLLs.

Wide-area file transfer uses the unmodified Node.js 24.12.0 runtime, node-datachannel 0.33.6 and libdatachannel 0.24.6. Node's complete notices are bundled at `p2p/runtime/Node-LICENSE.txt`. The native addon and libdatachannel are provided under MPL 2.0; corresponding source is available at https://github.com/murat-dogan/node-datachannel/tree/v0.33.6 and https://github.com/paullouisageneau/libdatachannel/tree/v0.24.6. Native transport dependencies and their license texts are retained in `licenses/WebRTC-LICENSES.txt`. These libraries are separate replaceable runtime files; FlickDock does not modify their source.

Publisher signature verification uses cryptography 50.0.2 (Apache-2.0 / BSD-3-Clause). Complete licenses are included in licenses/Release-Crypto-LICENSES.txt.

- FFmpeg 9.0.1 shared libraries supplied by Qt for Windows: LGPL 2.1 or later, built without GPL/nonfree components. Libraries remain separate and replaceable in `_internal/PySide6`. See licenses/FFmpeg-LICENSE.txt. Exact upstream source: https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz ; build configuration is recorded in PACKAGE_AUDIT.md. macOS retains Qt’s Darwin backend.


视频解析使用 yt-dlp 2026.10.26（Unlicense）与 yt-dlp-ejs 0.8.0，复用应用现有 Node 24.12.0 运行时，不远程执行附加组件。推荐依赖的许可证汇总于 licenses/Video-LICENSES.txt。

Windows 下载与音视频合并工具为独立目录下的 FFmpeg 9.0.2（7adfbe40dc）BtbN LGPL 构建，来源：https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-31-12-59 。原始源码：https://github.com/FFmpeg/FFmpeg/tree/7adfbe40dc ，构建配置由该上游项目公开。构建时校验固定归档 SHA256；原版 LGPL-3.0 和 GPL-3.0 许可一并提供。该独立进程不替换 Qt 播放器的 FFmpeg 动态库。macOS 视频工具由 imageio-ffmpeg 0.6.0 的平台轮子提供，独立 FFmpeg 可执行文件遵循其 GPL 许可；BSD 包装器源码及二进制构建来源：https://github.com/imageio/imageio-ffmpeg 。
