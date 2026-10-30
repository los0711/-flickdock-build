"""Validate a built release and write latest.json only after anonymous download.

This tool does not build, upload, tag, install, or call a hosting provider API.
Run it after uploading the immutable installer. See PUBLISHING.md.
"""
from __future__ import annotations

import argparse
import ast
import ctypes
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


class ReleaseError(ValueError):
    """An unverified release must not become the public update descriptor."""


VERSION_RE=re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
HASH_RE=re.compile(r'[0-9a-f]{64}\Z')
RUNTIME_SOURCES=(
    'flickdock.py','app_settings.py','async_images.py','capture_files.py',
    'confirm_clear.py','content_grid.py','content_resolver.py','content_stack.py',
    'dock_geometry.py','dock_preview.py','drag_artwork.py','drag_probe.py',
    'drop_routing.py','drop_bridge.py','flickdock_icons.py','flickdock_motion.py','global_hotkeys.py',
    'image_preview.py','instance_guard.py','media_preview.py','offscreen_verify.py',
    'runtime_media.py','selection_archive.py','shake_detector.py','shelf_content.py',
    'surface_morph.py','ui_theme.py','widget_retirement.py','item_transitions.py','ui_transitions.py','app_version.py',
    'native_ui_common.py','reference_widgets.py','pointer_paste.py','settings_edge_hint.py','cloud_connectivity.py','clipboard_sync.py','clipboard_preview.py','verify_clipboard.py','transfer_priority.py','small_upload_lane.py','runtime_cleanup.py','window_chrome.py','intake_notice.py','display_triggers.py','shelf_intake_limit.py',
    'update_client.py','update_download.py','update_signatures.py','update_dialog.py','update_controller.py','update_notice.py','release_history.py','network_environment.py','network_diagnostics.py','ci_mac_update_runtime.py','ci_utility_windows.py',
    'platform_startup.py','platform_native.py','mac_updates.py','mac_entry.py','runtime_macos.py','mac_permissions.py','mac_permissions_ui.py',
    'account_store.py','cloud_api.py','cloud_config.py','cloud_events.py','account_service.py','account_panel.py',
    'shelf_sync.py','shared_clipboard_files.py','sync_files.py','sync_adapter.py','verify_cloud.py','shortcut_preview.py',
    'lan_transfer.py','lan_panel.py','transfer_visuals.py','owned_files.py','verify_transfers.py',
    'wan_transfer.py','webrtc_stream.py','nearby_transport.py','verify_wan.py','windows_network.py',
)
MAINTAINED_TESTS=(
    'test_display_groups_v1821.py','test_transfer_layout_v1821.py','test_interaction_scope_v1821.py','test_shared_clipboard_files_v1821.py','test_download_ranges_v1821.py','test_remote_handoff_v1821.py',
    'test_mac_permissions_v1821.py','test_shelf_live_v1821.py','test_release_notes_v1821.py','test_clipboard_fast_v1821.py','test_clipboard_preview_v1821.py','test_utility_windows_v1821.py','test_update_resume_v1821.py','test_update_signatures_v1821.py',
    'test_small_upload_lane.py','test_feedback_v1820.py',
    'test_pointer_paste_v1819.py','test_presentation_v1819.py','test_clipboard_sync_v1819.py','test_wan_account_v1819.py','test_webrtc_close_v1819.py',
    'test_feedback_v1818.py','test_nearby_transport_v1818.py','test_wan_signaling_v1818.py','test_cloud_failover_v1818.py','test_windows_network_v1818.py',
    'test_app_settings.py','test_global_hotkeys.py','test_shelf_content.py',
    'test_content_grid.py','test_content_tile_completion.py','test_svg_hidpi.py',
    'test_shake_detector.py','test_drag_probe.py','test_content_resolver.py',
    'test_media_preview.py','test_flickdock_v16.py','test_bundle_policy.py',
    'test_dock_preview_v16.py','test_confirm_clear_v161.py','test_surface_morph_v161.py',
    'test_content_stack_v161.py','test_selection_archive_v161.py','test_drag_artwork_v161.py',
    'test_integration_v161.py','test_file_ownership_v161.py','test_file_release_v161.py',
    'test_async_images_v162.py','test_widget_retirement_v162.py','test_zip_dialog_v162.py',
    'test_surface_continuity_v162.py','test_settings_v163.py','test_capture_files_v163.py',
    'test_capture_integration_v163.py','test_media_missing_v163.py','test_clear_lifecycle_v163.py',
    'test_interactions_v163.py','test_motion_v163.py','test_overview_drop_v163.py',
    'test_sensitivity_v164.py','test_settings_chrome_v164.py','test_intake_v164.py',
    'test_morph_bounds_v164.py','test_intake_entry_v164.py','test_expand_flash_v165.py',
    'test_settings_gesture_v165.py','test_morph_clock_v164.py','test_gesture_entry_v165.py',
    'test_release_manifest_v170.py','test_update_client_v170.py',
    'test_update_dialog_v170.py','test_update_inline_v171.py','test_update_notice_v1813.py','test_settings_startup_v171.py',
    'test_platform_startup.py','test_platform_native.py','test_mac_updates.py','test_mac_build.py',
    'test_update_macos.py','test_update_macos_controller.py','test_update_dmg_v185.py','test_macos_native_entry.py','test_release_macos_v180.py',
    'test_account_panel_v180.py','test_account_security_v180.py','test_shelf_sync_v180.py','test_sync_review_v180.py','test_cloud_client_v180.py',
    'test_account_actions_v181.py','test_account_settings_v181.py','test_account_rules_v1811.py','test_transfer_ui_v181.py',
    'test_lan_transfer_v181.py','test_lan_dialog_v183.py','test_shelf_transfers_v181.py','test_sync_staging_v181.py','test_cloud_progress_v181.py',
    'test_cloud_api_stream_v183.py','test_sync_directions.py','test_sync_timeout_recovery.py',
    'test_sync_receive_priority.py','test_cloud_download_fast_path_v189.py','test_window_layer.py',
    'test_mac_window_order_v1810.py','test_mac_cocoa_spaces.py','test_win_window_order_v1810.py',
    'test_html_native_lan.py','test_html_native_dialogs.py','test_html_native_workspace.py',
    'test_hig_accessibility.py','test_account_profile_layout_v1814.py','test_lan_hig_layout_v1814.py',
    'test_ui_feedback_v1815.py','test_directory_sync_v1815.py','test_feedback_v1816.py','test_stream_upload_v1816.py',
    'test_shelf_raise_v1810.py','test_intake_first_frame_v1810.py',
    'test_sync_remote_commit_wakeup.py','test_cloud_file_status_v1810.py',
    'test_trigger_reliability_v1810.py','test_event_stream_wiring_v1810.py',
    'test_remote_transfer_actions_v1812.py','test_wan_transfer_v1816.py',
)
SOURCE_FILES=RUNTIME_SOURCES+MAINTAINED_TESTS+(
    '.github/workflows/native-clipboard.yml',
    'FlickDock.spec','FlickDock.iss','version_info.txt','ChineseSimplified.isl','windows-network.ps1',
    'flickdock.ico','THIRD_PARTY_NOTICES.md','font_manifest.json','bundle_policy.py',
    'release_manifest.py','release_macos.py','release_signing.py','package_source.py','PUBLISHING.md','README.md','AGENTS.md','UI_REFERENCE_IMPLEMENTATION.md','HIG_DESIGN.md','.gitignore','.gitattributes',
    'check_release_v16.py','release_dpr_v165.py','PACKAGE_AUDIT.md',
    'MOTION_V161.md','MOTION_V162.md','MOTION_V163.md','MOTION_V164.md','MOTION_V165.md',
    'bench_morph_v161.py','bench_cold_expand_v162.py','bench_clear_confirm_v161.py',
    'bench_cloud_latency_v183.py','bench_cloud_events_v183.py','bench_cloud_transfer_v188.py',
    'bench_lan_throughput_v183.py',
    'fonts/HarmonyOS_Sans_SC_Regular.ttf',
    'assets/ui/qtbase_zh_CN.qm',
    'licenses/HarmonyOS-Sans-LICENSE.txt','licenses/Python-LICENSE.txt',
    'licenses/LGPL-3.0.txt','licenses/GPL-3.0.txt','licenses/PyInstaller-COPYING.txt',
    'licenses/OpenSSL-LICENSE.txt','licenses/Cloud-Client-LICENSES.txt','licenses/Lucide-LICENSE.txt','licenses/WebRTC-LICENSES.txt','licenses/Release-Crypto-LICENSES.txt',
    'FlickDock-mac.spec','build_macos.py','MAC_BUILD.md','CLOUD_SYNC.md','FEATURE_V180.md','FEATURE_V181.md','FEATURE_V183.md',
    'cloud_remote_probe.py','cloud_feature_probe.py','cloud/package.json','cloud/package-lock.json','cloud/tsconfig.json',
    'cloud/wrangler.jsonc','cloud/README.md','cloud/.gitignore',
    'cloud/migrations/0001_schema.sql','cloud/migrations/0002_retention.sql','cloud/migrations/0003_transfers_password.sql',
    'cloud/migrations/0004_instant_uploads.sql','cloud/migrations/0005_account_names.sql','cloud/migrations/0006_cloud_retention.sql','cloud/migrations/0007_clipboard_upload_refs.sql','cloud/src/index.ts','cloud/src/hash-state.ts','cloud/src/wan.ts','cloud/src/relay-budget.ts','cloud/src/clipboard.ts','cloud/src/shared-file.ts','cloud/src/intake.ts',
    'cloud/src/wasm.d.ts','cloud/src/vendor/hash-wasm-sha256-4.12.0.wasm',
    'cloud/src/vendor/hash-wasm-sha256-4.12.0.json','cloud/src/vendor/hash-wasm-4.12.0-LICENSE.txt',
    'cloud/test/integration.mjs','cloud/test/shared-file.mjs','cloud/test/hash-state.mjs','cloud/test/upload-cancel.mjs','cloud/test/wan-codes.mjs','cloud/test/wan-connect.mjs','cloud/test/wan-account.mjs','cloud/test/account-names.mjs','cloud/test/qa-server.mjs','cloud/test/clipboard.mjs','cloud/test/clipboard-expiry.mjs',
    '.github/workflows/macos-build.yml','.github/workflows/native-clipboard.yml','ci_installer_close.py','ci_assets.py','p2p_assets.py',
    'p2p/bridge.mjs','p2p/turn_tls.mjs','p2p/test-turn-tls.mjs','p2p/package.json','p2p/package-lock.json','p2p/node-shasums.json',
    'website/wrangler.jsonc','website/worker.mjs','website/package.json','website/package-lock.json',
    'website/README.md','website/CHINA_DEPLOYMENT.md','website/test/check-site.mjs','website/test/check-gateway.mjs',
    'website/scripts/build-retiehe.mjs',
    'website/public/.assetsignore','website/public/index.html',
    'website/public/styles.css','website/public/site.js',
    'website/public/assets/flickdock.svg','website/public/assets/control-nearby.svg',
    'website/public/assets/control-chevron.svg','website/public/assets/control-check.svg',
    'website/public/assets/content-video.svg','website/public/assets/content-text.svg',
    'website/public/assets/content-pdf.svg','website/public/assets/content-image.svg',
    'website/public/assets/content-archive.svg',
)
OPTIONAL_SOURCE_FILES=(
    'LICENSE','LICENSE.txt','LICENSE.md',
)
ICON_FILES=tuple('assets/icons/'+name+'.svg' for name in (
    'flickdock','content-archive','content-audio','content-code','content-document',
    'content-folder','content-font','content-generic','content-image','content-link',
    'content-pdf','content-presentation','content-sheet','content-text','content-video',
    'control-arrow','control-audio','control-check','control-chevron-down','control-cloud',
    'control-chevron-up','control-chevron','control-close','control-collapse',
    'control-copy','control-dock','control-expand','control-file','control-file-up','control-folder',
    'control-image','control-more','control-nearby','control-pause','control-pin-filled','control-pin','control-receive',
    'control-grid','control-list','control-search','control-user',
    'control-play','control-plus','control-shelf','control-stop','control-temporary',
    'control-text','control-trash','control-video','control-volume-off','control-volume',
    'format-7z','format-doc','format-docx','format-flac','format-md','format-mkv',
    'format-mov','format-mp3','format-mp4','format-pdf','format-ppt','format-pptx',
    'format-rar','format-svg','format-svgz','format-txt','format-wav','format-webm',
    'format-xls','format-xlsx','format-zip',
))
REQUIREMENTS=b'PySide6==6.12.0\nPyInstaller==6.22.3\npefile==2024.8.26\nkeyring==25.7.0\ncryptography==50.0.2\n'
SECRET_PATTERNS=(
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:ghp|github_pat|gitee_pat)_[A-Za-z0-9_]{20,}\b'),
    re.compile(rb'https?://[^\s/:]+:[^\s/@]+@'),
)


def version_tuple(value: str) -> tuple[int,int,int]:
    if not isinstance(value,str) or not VERSION_RE.fullmatch(value):
        raise ReleaseError('Version must use canonical X.Y.Z without leading zeroes')
    return tuple(map(int,value.split('.')))


def sha256_file(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path):
    try:return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError) as exc:raise ReleaseError(f'Cannot read required JSON: {path.name}') from exc


def _safe_file(root: Path,name: str) -> Path:
    path=root/name
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ReleaseError(f'Missing or unsafe required release source: {name}')
    for parent in path.parents:
        if parent==root:break
        if parent.is_symlink() or getattr(parent,'is_junction',lambda:False)():
            raise ReleaseError(f'Symlink/junction in release source: {name}')
    return path


def installer_product_version(path: Path) -> str:
    """Read the PE product version without launching the installer."""
    if sys.platform!='win32':
        raise ReleaseError('Installer PE version verification requires the Windows release host')
    from ctypes import wintypes
    class FixedInfo(ctypes.Structure):
        _fields_=[(name,wintypes.DWORD) for name in (
            'signature','structure','file_ms','file_ls','product_ms','product_ls',
            'flags_mask','flags','os','type','subtype','date_ms','date_ls')]
    api=ctypes.WinDLL('version',use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype=wintypes.DWORD
    api.GetFileVersionInfoW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p]
    api.GetFileVersionInfoW.restype=wintypes.BOOL
    api.VerQueryValueW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype=wintypes.BOOL
    ignored=wintypes.DWORD();size=api.GetFileVersionInfoSizeW(str(path),ctypes.byref(ignored))
    if not size:raise ReleaseError('Installer is missing a readable PE version resource')
    data=ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path),0,size,data):raise ReleaseError('Cannot read installer PE version')
    pointer=ctypes.c_void_p();length=wintypes.UINT()
    if not api.VerQueryValueW(data,'\\',ctypes.byref(pointer),ctypes.byref(length)) or length.value<ctypes.sizeof(FixedInfo):
        raise ReleaseError('Installer is missing its fixed PE product version')
    info=ctypes.cast(pointer,ctypes.POINTER(FixedInfo)).contents
    if info.signature!=0xFEEF04BD or info.product_ls&0xffff:
        raise ReleaseError('Installer PE version is invalid or has an unsupported fourth version component')
    return '.'.join(map(str,(info.product_ms>>16,info.product_ms&0xffff,info.product_ls>>16)))


def validate_versions(root: Path,installer: Path,version: str) -> None:
    version_tuple(version)
    main=_safe_file(root,'flickdock.py').read_text(encoding='utf-8-sig')
    tree=ast.parse(main,filename='flickdock.py')
    values=[node.value.value for node in tree.body if isinstance(node,ast.Assign)
            and any(isinstance(target,ast.Name) and target.id=='APP_VERSION' for target in node.targets)
            and isinstance(node.value,ast.Constant)]
    central=_safe_file(root,'app_version.py').read_text(encoding='utf-8-sig')
    central_tree=ast.parse(central,filename='app_version.py')
    central_values=[node.value.value for node in central_tree.body if isinstance(node,ast.Assign)
                    and any(isinstance(target,ast.Name) and target.id=='APP_VERSION' for target in node.targets)
                    and isinstance(node.value,ast.Constant)]
    imports_version=any(isinstance(node,ast.ImportFrom) and node.module=='app_version'
                        and any(alias.name=='APP_VERSION' and alias.asname in (None,'APP_VERSION') for alias in node.names)
                        for node in tree.body)
    iss=_safe_file(root,'FlickDock.iss').read_text(encoding='utf-8-sig')
    inno=re.findall(r'^\s*#define\s+AppVersion\s+"([^"]+)"',iss,re.M)
    resource=_safe_file(root,'version_info.txt').read_text(encoding='utf-8-sig')
    strings=re.findall(r"StringStruct\(['\"](?:FileVersion|ProductVersion)['\"],\s*['\"]([^'\"]+)",resource)
    fixed=re.findall(r'(?:filevers|prodvers)\s*=\s*\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)',resource)
    if values or not imports_version or central_values!=[version] or inno!=[version] or len(strings)!=2 or any(value!=version for value in strings):
        raise ReleaseError('Imported app_version.APP_VERSION, Inno AppVersion and PE version strings must match the release version')
    expected=version_tuple(version)+(0,)
    if len(fixed)!=2 or any(tuple(map(int,value))!=expected for value in fixed):
        raise ReleaseError('PE fixed file/product versions do not match the release version')
    if installer_product_version(installer)!=version:raise ReleaseError('Built installer PE version does not match the release version')


def validate_verification(path: Path,installer: Path,version: str,sha256: str,size: int) -> dict:
    report=_json(path)
    if not isinstance(report,dict) or report.get('version')!=version:
        raise ReleaseError('Verification report must identify this exact release version')
    checks=report.get('checks')
    if not isinstance(checks,list) or not checks or any(not isinstance(row,dict) or type(row.get('exit_code')) is not int or row['exit_code']!=0 for row in checks):
        raise ReleaseError('Verification report must contain successful release checks')
    frozen=report.get('frozen',{})
    if not isinstance(frozen,dict) or frozen.get('ok') is not True or frozen.get('frozen') is not True:
        raise ReleaseError('Verification report must include a passing frozen-program check')
    setup=report.get('setup',{})
    if (not isinstance(setup,dict) or setup.get('name')!=installer.name or setup.get('bytes')!=size
            or str(setup.get('sha256','')).lower()!=sha256):
        raise ReleaseError('Verification report does not attest these exact installer bytes')
    return dict(checks=len(checks),report_sha256=sha256_file(path))


def collect_source_files(root: Path) -> list[tuple[str,bytes]]:
    names=list(SOURCE_FILES+ICON_FILES)
    names.extend(name for name in OPTIONAL_SOURCE_FILES if (root/name).is_file())
    sources=[]
    for name in sorted(set(names)):
        path=_safe_file(root,name);data=path.read_bytes()
        if path.suffix.lower() not in ('.ttf','.ico') and any(pattern.search(data) for pattern in SECRET_PATTERNS):
            raise ReleaseError(f'Credential/private-key pattern in selected release source: {name}')
        sources.append((name,data))
    selected={name for name,_ in sources}
    # Local runtime and test dependencies require an explicit allowlist edit.
    # Never silently sweep a source directory into a release archive.
    for name,data in sources:
        if not name.endswith('.py'):continue
        tree=ast.parse(data.decode('utf-8-sig'),filename=name)
        for node in ast.walk(tree):
            modules=([alias.name.split('.')[0] for alias in node.names] if isinstance(node,ast.Import)
                     else [node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module else [])
            for module in modules:
                local=module+'.py'
                if (root/local).is_file() and local not in selected:
                    raise ReleaseError(f'Local import is missing from the explicit source allowlist: {local}')
    font=_json(_safe_file(root,'font_manifest.json'))
    entries=font.get('fonts') if isinstance(font,dict) else None
    if not isinstance(entries,list) or not entries:raise ReleaseError('A verified bundled-font manifest is required')
    for entry in entries:
        path=_safe_file(root,entry['file'])
        if entry['file'] not in selected or path.stat().st_size!=entry['bytes'] or sha256_file(path)!=entry['sha256']:
            raise ReleaseError('Bundled font differs from its reviewed font manifest')
    sources.append(('requirements.txt',REQUIREMENTS))
    return sorted(sources)


def source_archive(root: Path,version: str) -> tuple[bytes,bytes]:
    sources=collect_source_files(root)
    inventory=dict(schema_version=1,product='FlickDock',version=version,
                   files=[dict(path=name,size=len(data),sha256=hashlib.sha256(data).hexdigest()) for name,data in sources])
    manifest=(json.dumps(inventory,ensure_ascii=False,indent=2)+'\n').encode('utf8')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sources+[('SOURCE_MANIFEST.json',manifest)]:
            info=zipfile.ZipInfo('FlickDock-'+version+'/'+name,date_time=(1980,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=3;info.external_attr=0o100644<<16
            archive.writestr(info,data)
    return buffer.getvalue(),manifest


def validate_url(url: str,*,allow_redirect_signatures=False) -> str:
    if not isinstance(url,str) or not url or any(ord(ch)<33 for ch in url):raise ReleaseError('Invalid public download URL')
    try:parts=urlsplit(url);port=parts.port
    except ValueError as exc:raise ReleaseError('Invalid public download URL') from exc
    host=(parts.hostname or '').lower()
    if parts.scheme!='https' or not host or parts.username is not None or parts.password is not None or parts.fragment:
        raise ReleaseError('Public URLs must use HTTPS without credentials or fragments')
    if host=='localhost' or host.endswith(('.localhost','.local','.internal')):raise ReleaseError('Download URL is not public')
    try:address=ipaddress.ip_address(host)
    except ValueError:address=None
    if address is not None and not address.is_global:raise ReleaseError('Download URL is not a public address')
    credentials={'token','access_token','private_token','password','secret','key','api_key','auth','authorization'}
    signatures={'signature','sig','x-amz-signature','x-amz-credential'}
    query=parse_qsl(parts.query,keep_blank_values=True)
    keys={key.lower() for key,_ in query}
    fields=dict(query)
    # Anonymous Gitee attachment redirects use "token" for their CDN signature.
    # This exception is never enabled for the stable URL published in latest.json.
    attachment_signature=(allow_redirect_signatures and host=='foruda.gitee.com'
                          and parts.path.startswith('/attach_file/')
                          and len(query)==len(fields) and set(fields)<={'token','ts','attname'}
                          and all(fields.get(key,'').strip() for key in ('token','ts')))
    credential_keys=keys&credentials
    if ((credential_keys and not (credential_keys=={'token'} and attachment_signature))
            or (keys&signatures and not allow_redirect_signatures)):
        raise ReleaseError('Credential or temporary signing parameters cannot be published in update URLs')
    if port not in (None,443):raise ReleaseError('Public release URLs must use the HTTPS default port')
    return host


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def _public_response(url: str,allowed_hosts: set[str],opener=None):
    opener=opener or build_opener(_NoRedirect())
    current=url
    for hop in range(6):
        # Approved raw/attachment CDNs may return short-lived signatures on
        # anonymous redirects. Stable published URLs cannot contain them.
        host=validate_url(current,allow_redirect_signatures=hop>0)
        if host not in allowed_hosts:raise ReleaseError(f'Redirect/download host is not approved: {host}')
        request=Request(current,headers={'User-Agent':'FlickDock-Release/1','Accept-Encoding':'identity'})
        try:
            try:response=opener.open(request,timeout=30)
            except HTTPError as error:
                if error.code not in (301,302,303,307,308):raise
                response=error
        except (OSError,URLError) as exc:raise ReleaseError('Anonymous public download request failed') from exc
        status=response.getcode()
        if status in (301,302,303,307,308):
            location=response.headers.get('Location');response.close()
            if not location:raise ReleaseError('Download redirect has no Location')
            current=urljoin(current,location);continue
        if status!=200:
            response.close();raise ReleaseError(f'Public download returned HTTP {status}')
        return response,current
    raise ReleaseError('Too many public download redirects')


def verify_public_file(url: str,sha256: str,size: int,allowed_hosts: set[str],*,opener=None,pe=True) -> dict:
    response,final_url=_public_response(url,allowed_hosts,opener)
    digest=hashlib.sha256();count=0;first=b''
    try:
        content_type=response.headers.get('Content-Type','').lower().split(';',1)[0]
        if content_type.startswith('text/') or content_type in ('application/json','application/xhtml+xml'):
            raise ReleaseError('Public download is an HTML/login/text response instead of a release file')
        declared=response.headers.get('Content-Length')
        if declared is not None and (not declared.isdecimal() or int(declared)!=size):
            raise ReleaseError('Public download Content-Length does not match the verified file')
        while True:
            chunk=response.read(1024*1024)
            if not chunk:break
            if not first:first=chunk[:2]
            count+=len(chunk)
            if count>size:raise ReleaseError('Public download is larger than the verified installer')
            digest.update(chunk)
    finally:response.close()
    if count!=size or digest.hexdigest()!=sha256:raise ReleaseError('Public download size/SHA256 does not match the verified local file')
    if pe and first!=b'MZ':raise ReleaseError('Public installer is not a Windows PE executable')
    return dict(url=url,final_url=final_url,size=count,sha256=digest.hexdigest())


def read_public_manifest(url: str,allowed_hosts: set[str],*,opener=None) -> dict:
    response,_=_public_response(url,allowed_hosts,opener)
    try:data=response.read(1024*1024+1)
    finally:response.close()
    if len(data)>1024*1024:raise ReleaseError('Previous update manifest is too large')
    try:value=json.loads(data.decode('utf-8-sig'))
    except (ValueError,UnicodeError) as exc:raise ReleaseError('Previous URL did not return a JSON manifest') from exc
    return validate_previous(value)


def validate_previous(value: dict) -> dict:
    if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version']!=1:
        raise ReleaseError('Previous manifest has an unsupported schema')
    version_tuple(value.get('version'))
    installer=value.get('installer',{})
    if not isinstance(installer,dict) or not HASH_RE.fullmatch(str(installer.get('sha256',''))) or type(installer.get('size')) is not int or installer['size']<=0:
        raise ReleaseError('Previous manifest has invalid installer metadata')
    validate_url(installer.get('url'))
    filename=installer.get('filename')
    if filename is not None and (not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.exe',filename)):
        raise ReleaseError('Previous manifest has an unsafe installer filename')
    if not isinstance(value.get('notes'),str):raise ReleaseError('Previous manifest needs text release notes')
    try:
        published=value['published_at']
        stamp=datetime.fromisoformat(published.replace('Z','+00:00')) if isinstance(published,str) else None
        if stamp is None or stamp.utcoffset()!=timezone.utc.utcoffset(stamp):raise ValueError
    except (KeyError,ValueError,TypeError):raise ReleaseError('Previous manifest needs a UTC published_at timestamp') from None
    return value


def _write_atomic(path: Path,data: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,name=tempfile.mkstemp(prefix='.'+path.name+'-',suffix='.tmp',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def create_release(*,root: Path,installer: Path,version: str,notes: str,download_url: str,
                   verification: Path,output: Path,artifacts: Path,previous: dict|None=None,
                   initial_release=False,allowed_hosts: set[str]|None=None,opener=None,signer=None) -> dict:
    if installer.is_symlink():raise ReleaseError('Installer cannot be a symlink')
    root=root.resolve();installer=installer.resolve();version_tuple(version)
    if artifacts.resolve().is_relative_to(output.parent.resolve()):
        raise ReleaseError('Private source artifacts cannot be placed in the public update-index directory')
    if not installer.is_file() or installer.is_symlink() or installer.suffix.lower()!='.exe' or installer.stat().st_size<=2:
        raise ReleaseError('A built, nonempty .exe installer is required')
    if installer.name!=f'FlickDock-Setup-{version}.exe':raise ReleaseError('Installer filename must identify the exact release version')
    with installer.open('rb') as stream:
        if stream.read(2)!=b'MZ':raise ReleaseError('Built installer is not a Windows PE executable')
    if not isinstance(notes,str) or not notes.strip() or len(notes)>50000:raise ReleaseError('Nonempty release notes are required')
    host=validate_url(download_url)
    approved={host} if allowed_hosts is None else {entry.lower() for entry in allowed_hosts}
    baselines=[]
    if previous is not None:baselines.append(validate_previous(previous))
    # A stale caller-supplied baseline cannot bypass a newer local index.
    if output.is_file():baselines.append(validate_previous(_json(output)))
    if not baselines and not initial_release:raise ReleaseError('Previous public manifest is required; first publication needs --initial-release')
    if any(version_tuple(version)<version_tuple(value['version']) for value in baselines):
        raise ReleaseError('Release version downgrade is forbidden')
    validate_versions(root,installer,version)
    size=installer.stat().st_size;digest=sha256_file(installer)
    qa=validate_verification(verification,installer,version,digest,size)
    source_bytes,inventory=source_archive(root,version)
    source_name=f'FlickDock-{version}-source.zip'
    source=dict(filename=source_name,size=len(source_bytes),sha256=hashlib.sha256(source_bytes).hexdigest())
    existing_source=artifacts/source_name
    if existing_source.is_file() and sha256_file(existing_source)!=source['sha256']:
        raise ReleaseError('An existing version cannot acquire different private source bytes; publish a newer version')
    metadata=dict(filename=installer.name,url=download_url,sha256=digest,size=size)
    manifest=dict(schema_version=1,version=version,
                  published_at=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),
                  notes=notes.strip(),installer=metadata)
    from release_history import release_entries,manifest_notes
    history=release_entries(version,notes)
    if history:
        manifest['releases']=history
        manifest['notes']=manifest_notes(version,notes)
    from update_client import validate_manifest,MAX_MANIFEST_BYTES
    try:
        if signer is not None:
            from update_signatures import sign_manifest
            manifest=sign_manifest(manifest,signer)
        elif version_tuple(version)>=(1,8,21) and host in ('gitee.com','flickdock-site.roger000416.workers.dev'):
            raise ValueError('A publisher signature is required for this official release')
        validate_manifest(manifest,approved,platform='win32')
        if len((json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode('utf8'))>MAX_MANIFEST_BYTES:
            raise ValueError('Update descriptor exceeds the desktop reader limit')
    except (TypeError,ValueError) as error:
        raise ReleaseError('Windows release manifest is not accepted by the shipped updater') from error
    for previous in baselines:
        if version!=previous['version']:continue
        old=previous['installer']
        if any(old.get(key)!=metadata[key] for key in ('url','sha256','size')):
            raise ReleaseError('An existing version cannot be repackaged or moved; publish a newer version')
        if previous.get('notes')!=manifest['notes']:
            raise ReleaseError('An existing version has immutable release notes; publish a newer version')
        manifest=previous
    public=verify_public_file(download_url,digest,size,approved,opener=opener)
    # The mutable local inputs may have changed during a slow public download.
    # Reject rather than pairing a verified file with later source/build bytes.
    if installer.stat().st_size!=size or sha256_file(installer)!=digest:
        raise ReleaseError('Installer changed during public verification')
    if source_archive(root,version)[0]!=source_bytes:raise ReleaseError('Source changed during public verification')
    inventory_name=f'FlickDock-{version}-source-manifest.json'
    sums=f'{digest}  {installer.name}\n{source["sha256"]}  {source_name}\n'
    record=dict(schema_version=1,version=version,verification=qa,public_download=public,installer=metadata,source=source)
    _write_atomic(artifacts/source_name,source_bytes)
    _write_atomic(artifacts/inventory_name,inventory)
    _write_atomic(artifacts/f'FlickDock-{version}-SHA256SUMS.txt',sums.encode('utf8'))
    _write_atomic(artifacts/f'FlickDock-{version}-release-record.json',(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode('utf8'))
    # latest.json is the commit point and is always written last.
    _write_atomic(output,(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode('utf8'))
    return manifest


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--installer',type=Path,required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--notes-file',type=Path,required=True)
    parser.add_argument('--download-url',required=True)
    parser.add_argument('--verification',type=Path,required=True)
    parser.add_argument('--output',type=Path,help='Default: ROOT/updates/latest.json')
    parser.add_argument('--artifacts',type=Path,required=True)
    parser.add_argument('--previous-manifest',type=Path)
    parser.add_argument('--previous-url')
    parser.add_argument('--initial-release',action='store_true')
    parser.add_argument('--sign',action='store_true',help='Sign using the publisher credential in the OS vault')
    parser.add_argument('--allow-host',action='append',default=[])
    args=parser.parse_args(argv)
    try:
        if args.previous_manifest and args.previous_url:raise ReleaseError('Choose one previous-manifest input')
        approved={validate_url(args.download_url),*map(str.lower,args.allow_host)}
        if args.previous_url:approved.add(validate_url(args.previous_url))
        previous=(validate_previous(_json(args.previous_manifest)) if args.previous_manifest else
                  read_public_manifest(args.previous_url,approved) if args.previous_url else None)
        manifest=create_release(root=args.root,installer=args.installer,version=args.version,
                                notes=args.notes_file.read_text(encoding='utf-8-sig'),download_url=args.download_url,
                                verification=args.verification,output=args.output or args.root/'updates/latest.json',
                                artifacts=args.artifacts,previous=previous,initial_release=args.initial_release,
                                allowed_hosts=approved,signer=__import__('release_signing').publisher_signer() if args.sign else None)
    except (ReleaseError,OSError,SyntaxError) as exc:
        print('RELEASE_BLOCKED: '+str(exc),file=sys.stderr);return 1
    print(json.dumps(dict(version=manifest['version'],manifest=str(args.output or args.root/'updates/latest.json'),
                          public_installer_verified=True),ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
