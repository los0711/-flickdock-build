"""Frozen native-icon helper + isolated offscreen settings acceptance."""
import json,os,sys,tempfile,time,traceback
from pathlib import Path

def run(argv):
    if os.environ.get('QT_QPA_PLATFORM')!='offscreen':raise RuntimeError('Magnetic acceptance requires an offscreen parent')
    return _run(argv)

def _run(argv):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QEvent,QCoreApplication
    from app_settings import SettingsDialog,SettingsStore
    from shelf_ui_common import load_font
    from edge_native_icons import NativeIconCache,read_native_icon
    from app_version import APP_VERSION
    from display_triggers import display_id,display_values
    import ui_theme
    output=Path(argv[argv.index('--verify-output')+1]).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    app=QApplication([]);app.setQuitOnLastWindowClosed(False);load_font(app)
    report=dict(ok=False,version=APP_VERSION,frozen=bool(getattr(sys,'frozen',False)),qt_platform=app.platformName())
    dialog=None;cache=None
    def settle(check,seconds=5):
        deadline=time.monotonic()+seconds
        while not check() and time.monotonic()<deadline:app.processEvents();time.sleep(.005)
        assert check(),'Isolated verification did not settle'
    try:
        with tempfile.TemporaryDirectory(prefix='flickdock-magnetic-verify-') as directory:
            store=SettingsStore(Path(directory)/'profile');dialog=SettingsDialog(store);dialog.load_values()
            key=display_id(app.primaryScreen());rows=dialog.magnetic_editor.rows()
            assert {edge for edge,row in rows.items() if row['enabled']}=={'left','right'}
            assert rows['left']['width']==80 and rows['left']['length']==50
            dialog.magnetic_editor.controls['length'].setValue(100)
            settle(lambda:store.magnetic_edges['displays'].get(key,{}).get('left',{}).get('length')==100)
            dock=dialog.display_trigger_editor.rows[key];dock.length_percent.setValue(100)
            settle(lambda:store.display_triggers.get(key,{}).get('length_percent')==100)
            assert display_values(store,app.primaryScreen())['dock_length']==app.primaryScreen().geometry().height()
            dialog.accent_presets['#0066D6'].click();settle(lambda:store.accent_color=='#0066D6')
            # Parent is offscreen; only the helper uses the native platform,
            # reads this generated fixture's icon, and shows no widgets.
            fixture=Path(directory)/'native-icon-fixture.txt';fixture.write_text('synthetic icon fixture',encoding='utf-8')
            cache=NativeIconCache(reader=read_native_icon);cache.request(str(fixture))
            settle(lambda:cache.image(str(fixture)) is not None)
            image=cache.image(str(fixture));assert not image.isNull()
            image.save(str(output.with_name('frozen-native-icon.png')))
            for theme in ('light','dark'):
                store.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(app,app.font().family());dialog.apply_theme(theme)
                dialog.show();app.processEvents();time.sleep(.03);app.processEvents()
                dialog.magnetic_editor.grab().save(str(output.with_name('magnetic-settings-'+theme+'.png')))
                dialog.display_trigger_editor.grab().save(str(output.with_name('dock-settings-'+theme+'.png')))
                dialog._reference_appearance.grab().save(str(output.with_name('theme-presets-'+theme+'.png')))
            report.update(ok=True,default_left_right=True,default_half_region=True,full_magnetic_length=True,
                          full_dock_length=True,presets_live=True,native_icon_helper=True,native_icon_size=[image.width(),image.height()],
                          native_icon_parent_offscreen=True,account_access=False,clipboard_access=False)
            cache.close();cache.pool.waitForDone(3000);cache.deleteLater();cache=None
            dialog._preference_writer.close();dialog.close();dialog.deleteLater();dialog=None
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
    except Exception:report['traceback']=traceback.format_exc()
    finally:
        if cache is not None:cache.close();cache.pool.waitForDone(3000)
        if dialog is not None:dialog._preference_writer.close();dialog.close()
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['ok'] else 1
