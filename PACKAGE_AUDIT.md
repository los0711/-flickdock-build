# FlickDock 1.8.26 Windows bundle audit

The bundle uses PySide6 / Qt 6.12, Qt FFmpeg 9.0.1, Qt Network/Schannel, OpenSSL and the existing native WebRTC runtime. Only the original HarmonyOS Sans SC Regular font is included; platform fonts remain the UI default.

`FlickDock.spec` runs `bundle_policy.filter_bundle` after PyInstaller analysis, applying the same policy to both binary and data TOCs:

1. Retain the Windows/offscreen/minimal platform plugins, Windows style, required image formats, SVG icon engine, certificate/Schannel TLS, network information and FFmpeg media plugin.
2. Remove unused plugin entry points, QML/resources/translations, unused font weights and the obsolete Windows Media Foundation plugin.
3. Inspect PE direct imports, delayed imports and export forwarders. Keep the complete dependency closure of Python extensions, runtime roots and selected plugins. Never trim a library solely because of its size.
4. Use Windows system ICU rather than shadowing its unversioned API with Poppler ICU.
5. Retain `_hashlib` and its OpenSSL dependencies for authenticated account derivation and integrity checking.
6. Set `QT_MEDIA_BACKEND=ffmpeg` before Qt loads on Windows. macOS retains the Darwin backend. Avoid forcing the optional software OpenGL fallback.

Video playback uses QVideoWidget, retaining Qt6MultimediaWidgets and its native dependencies. CPU frame conversion is limited to posters, explicit paused seeks and source release. Each source owns a new player and video sink, with generation fences rejecting stale callbacks. Hide/removal clears the media source and outputs so file handles can be released.

The FFmpeg PE dependency closure retains avcodec-63, avformat-63, avutil-61, swresample-7 and swscale-10. These shared libraries remain replaceable in `_internal/PySide6`. The installed avutil reports FFmpeg 9.0.1 and LGPL version 2.1 or later. Exact source is available at https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz ; license text is bundled as `licenses/FFmpeg-LICENSE.txt`.

FFmpeg configuration reported by the actual distributed DLL:

```
--prefix=/c/FFmpeg-n9.0.1/build/msvc/installed --disable-programs --disable-doc --disable-debug --enable-network --disable-lzma --enable-pic --disable-vulkan --disable-v4l2-m2m --disable-decoder=truemotion1 --disable-avdevice --disable-avfilter --enable-zlib --extra-cflags='-IC:/zlib-1.3.1/build/amd64' --extra-ldflags='-LIBPATH:C:/zlib-1.3.1/build/amd64' --toolchain=msvc --enable-shared --disable-static
```

Generated analysis and frozen audits list the selected destinations, hashes and unresolved imports. Release verification checks that the FFmpeg plugin loads inside the frozen bundle, decodes a real MP4, presents a native video surface, seeks to a clicked timeline position, advances playback while muted, releases the file on close and rejects replay after removal. Source-level tests additionally exercise source-switch fences and coalesced drag seeks. Offscreen verification does not measure on-screen GPU smoothness on every device.
