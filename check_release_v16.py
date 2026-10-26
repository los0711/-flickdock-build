"""Run independent release checks in disposable offscreen profiles."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root=Path(__file__).resolve().parent
tests=['test_app_settings.py','test_global_hotkeys.py','test_shelf_content.py',
       'test_content_grid.py','test_content_tile_completion.py','test_svg_hidpi.py',
       'test_shake_detector.py','test_drag_probe.py','test_content_resolver.py',
       'test_media_preview.py','test_flickdock_v16.py','test_bundle_policy.py',
       'test_dock_preview_v16.py','test_confirm_clear_v161.py','test_surface_morph_v161.py',
       'test_content_stack_v161.py','test_selection_archive_v161.py','test_drag_artwork_v161.py',
       'test_integration_v161.py','test_file_ownership_v161.py','test_file_release_v161.py',
       'test_async_images_v162.py','test_widget_retirement_v162.py','test_zip_dialog_v162.py',
       'test_surface_continuity_v162.py','test_settings_v163.py','test_capture_files_v163.py',
       'test_capture_integration_v163.py','test_media_missing_v163.py',
       'test_clear_lifecycle_v163.py','test_interactions_v163.py','test_motion_v163.py',
       'test_overview_drop_v163.py','test_sensitivity_v164.py','test_settings_chrome_v164.py',
       'test_intake_v164.py','test_morph_bounds_v164.py','test_intake_entry_v164.py',
       'test_expand_flash_v165.py','test_settings_gesture_v165.py',
       'test_update_client_v170.py','test_update_dialog_v170.py',
       'test_update_inline_v171.py','test_release_manifest_v170.py',
       'test_settings_startup_v171.py','test_account_panel_v180.py',
       'test_platform_startup.py','test_platform_native.py','test_mac_updates.py','test_mac_build.py',
       'test_update_macos.py','test_update_macos_controller.py','test_update_dmg_v185.py',
       'test_macos_native_entry.py','test_shelf_sync_v180.py','test_sync_review_v180.py','test_account_security_v180.py',
       'test_release_macos_v180.py','test_account_actions_v181.py','test_account_settings_v181.py',
       'test_transfer_ui_v181.py','test_lan_transfer_v181.py','test_lan_dialog_v183.py','test_shelf_transfers_v181.py',
       'test_sync_staging_v181.py','test_cloud_progress_v181.py']
logs=root/'release-checks-v185';logs.mkdir(exist_ok=True)

def check(name):
    with tempfile.TemporaryDirectory(prefix='flickdock-release-') as profile:
        env=dict(os.environ,QT_QPA_PLATFORM='offscreen',APPDATA=profile)
        try:
            result=subprocess.run([sys.executable,str(root/name)],env=env,cwd=root.parent,
                                  capture_output=True,text=True,encoding='utf8',errors='replace',timeout=90)
            (logs/(name+'.log')).write_text(result.stdout+result.stderr,encoding='utf8')
            return {'test':name,'exit_code':result.returncode}
        except subprocess.TimeoutExpired:
            return {'test':name,'exit_code':124}

with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(check,tests))
# Timing evidence is collected with no other regression process competing for
# CPU; its purpose is to measure the application's own event-loop work.
results.append(check('test_morph_clock_v164.py'))
results.append(check('test_gesture_entry_v165.py'))
(logs/'results.json').write_text(json.dumps(results,indent=2),encoding='utf8')
for result in results:print(('PASS' if result['exit_code']==0 else 'FAIL')+' '+result['test'])
raise SystemExit(any(item['exit_code'] for item in results))
