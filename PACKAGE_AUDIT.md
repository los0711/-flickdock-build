# FlickDock 1.6.0 bundle audit

The 1.6 bundle uses Qt Core/Gui/Widgets/Network/SVG/Multimedia with Windows Media Foundation. Only the original HarmonyOS Sans SC Regular font is included. Qt synthesizes display weights; font bytes are unchanged.

`FlickDock.spec` runs `bundle_policy.filter_bundle` after PyInstaller analysis. This applies the same policy to **both** binary and data TOCs:

1. Keep the Windows/offscreen/minimal platform plugins, Windows widget style, JPEG/GIF/TIFF/WebP/ICO/SVG image formats, SVG icon engine, certificate/Schannel TLS, Windows network information and native Windows multimedia plugin.
2. Remove unused Qt plugin entry points, QML/resources/translations and unused font weights.
3. Read PE direct imports, delay imports and export forwarders with pefile. Keep the dependency closure of Python extension/runtime roots and selected runtime-loaded Qt plugins. A large DLL is never removed if a retained binary imports it.
4. Do not bundle Poppler's unrelated `icuuc.dll`; Qt's unversioned ICU API uses Windows system ICU. This also makes Poppler's `icudt78.dll` unreachable.
5. Exclude only the optional `_hashlib` accelerator; include CPython's builtin `_md5`, `_sha1`, `_sha2`, `_sha3` and `_blake2` implementations. If an actual runtime SSL extension imports OpenSSL, dependency closure still retains its libraries.
6. Select `QT_MEDIA_BACKEND=windows` before Qt loads. Do not force a software OpenGL mode when its optional 20 MB fallback is absent.

A static audit of the existing 1.5 distribution (not a 1.6 build) found **177,732,295 bytes -> 77,066,090 bytes**, removing **100,666,205 bytes (56.6%)**. It did not yet include the new native multimedia module. The 1.6 analysis additionally avoids the optional OpenSSL hash accelerator when SSL is not needed.

Largest confirmed unreachable or unused components in that audit:

| Component | Bytes | Evidence |
| --- | ---: | --- |
| Poppler `icudt78.dll` | 33,126,192 | No retained import references it |
| Software OpenGL `opengl32sw.dll` | 20,639,544 | No retained import references it |
| Medium and Bold fonts | 16,386,140 | Single unmodified Regular font supplies the UI |
| Qt Quick/QML/QML Models | 13,164,456 | Their loading plugin entry points are excluded |
| Qt PDF | 4,623,672 | PDF image plugin excluded; no retained import references it |
| Qt OpenGL | 1,989,944 | No retained import references it |
| OpenSSL `libssl` | 1,336,624 | No retained SSL/TLS plugin imports it |

`libcrypto` was correctly retained in the old distribution audit because `_hashlib.pyd` imported it; this is why it was not blindly deleted. The final build writes its actual totals and dependency reasons to `package-audit-analysis.json`. Final installed and compressed setup sizes must be measured from that build.

Static audit command (read-only):

`python bundle_policy.py dist/FlickDock --output package-audit.json`

The required native media plugin is reported as missing when auditing the old 1.5 distribution; the 1.6 spec explicitly analyses QtMultimedia and requires it in the build. This audit does not install the application, create shortcuts, register uninstall entries or modify user settings.

## Release gates

- Source manifest: `package_source.collect_sources()` must resolve every required file, including `image_preview.py` and `offscreen_verify.py`; its local Python import closure includes new helpers without sweeping user media or old upgrade scripts into the archive.
- Build-time report: `required_dynamic_roots_missing`, `required_plugin_paths_missing` and `unresolved_non_system_imports` must all be empty. The spec checks Qt DLLs, canonical Qt plugin destinations, Schannel/certificate TLS, Windows native media, SVG and the supported image codecs before collecting the bundle.
- Frozen runtime: run `dist/FlickDock/FlickDock.exe --verify-media PATH_TO_LOCAL_VIDEO --verify-output verify-result.json`. This application entry point forces offscreen mode before creating the manager and checks muted media cover/seek/play/pause/stop, SVG at fractional DPR, native TLS and builtin SHA. Use the actual local fixture for this release; source archives do not include user media.
- Optional source native-media test: `python test_media_native.py --media PATH_TO_LOCAL_VIDEO`. `FLICKDOCK_TEST_MEDIA` is also accepted. A missing fixture reports explicit SKIP; a supplied fixture must pass.
- Installer compilation follows the frozen gate. Verify Inno's compiled version is 1.6.0 and measure the actual setup size; the script enables current-user uninstall registration, always provides Start Menu app/uninstall entries, and checks the desktop shortcut task by default. These static/compile gates do not install or modify the user's registry.

PE review of `windowsmediaplugin.dll` confirms dependencies on Qt Core/Gui/Multimedia plus Windows Media Foundation, EVR, D3D9/DXVA and system runtimes. Qt Multimedia also imports Qt Network. Schannel and the native network-information plugin depend on Qt Core/Network and Windows cryptography/network APIs. None of these native entry points requires Qt Quick, Qt QML or the FFmpeg backend.


## 1.8.0 account security dependency

The native scrypt password KDF now requires _hashlib and libcrypto. They are intentionally retained and their license is included; removing them would break account authentication. Runtime Qt network/media trimming and the single unchanged HarmonyOS font remain. keyring uses only native platform credential backends, not a bundled browser or database server.
