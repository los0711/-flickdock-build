"""Observe real native overview/utility handoff, only on disposable CI."""
import json,os,sys,tempfile,time
from pathlib import Path


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS') not in ('Windows','macOS'):
        raise RuntimeError('Native navigation QA requires an ephemeral CI runner')
    os.environ['QT_QPA_PLATFORM']='cocoa' if sys.platform=='darwin' else 'windows'
    output=Path('transfer-navigation-native.json').resolve();trace=[];checks=[]
    with tempfile.TemporaryDirectory(prefix='flickdock-navigation-') as directory:
        os.environ['APPDATA']=directory;os.environ['XDG_CONFIG_HOME']=directory
        from PySide6.QtCore import QTimer,Qt,QMimeData,QUrl
        from PySide6.QtGui import QKeySequence
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        import flickdock
        app=QApplication([]);flickdock.load_font(app)
        manager=flickdock.ShelfManager(app,cocoa_smoke=True);manager.timer.start(20)
        manager.shelf.pinned=False
        started=time.monotonic()
        def sample(event='frame'):
            row=dict(ms=round((time.monotonic()-started)*1000),event=event,
                app_state=app.applicationState().name,shelf_visible=manager.shelf.isVisible(),
                shelf_state=manager.shelf.visible_state,expanded=manager.shelf.expanded,
                transfer_visible=bool(manager.lan_dialog and manager.lan_dialog.isVisible()))
            if sys.platform=='darwin':
                import AppKit
                row['dock_policy']=int(AppKit.NSApplication.sharedApplication().activationPolicy())
            trace.append(row)
        timer=QTimer();timer.setInterval(10);timer.timeout.connect(sample);timer.start()
        def wait(predicate,seconds=5):
            end=time.monotonic()+seconds
            while not predicate() and time.monotonic()<end:app.processEvents();time.sleep(.004)
            assert predicate(),'Native navigation did not settle'
        def spin(seconds):
            end=time.monotonic()+seconds
            while time.monotonic()<end:app.processEvents();time.sleep(.004)
        try:
            for _ in range(2):
                manager.show_overview();wait(lambda:manager.shelf.isVisible() and manager.shelf.expanded)
                manager.show_lan_dialog();wait(lambda:manager.lan_dialog.isVisible())
                manager.shelf.hide_immediately();spin(.05);sample('before-back')
                manager.lan_dialog.back_button.click()
                wait(lambda:not manager.lan_dialog.isVisible());spin(.45);sample('after-back')
                assert manager.shelf.isVisible() and manager.shelf.expanded,'Back closed transfer without a visible overview'
                checks.append('back restores visible unpinned overview')
            manager.show_lan_dialog();wait(lambda:manager.lan_dialog.isVisible());dialog=manager.lan_dialog
            original=Path(directory)/'original.bin';original.write_bytes(b'original native paste fixture')
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(original))]);app.clipboard().setMimeData(mime)
            native=dialog.winId()
            for mode in ('wan','lan','wan'):
                dialog.mode_buttons[mode].click();app.processEvents()
                assert sum(control.isChecked() for control in dialog.mode_buttons.values())==1
                assert dialog.mode_buttons[mode].parentWidget() is dialog.mode_segment
                QTest.keySequence(dialog.panel.drop_zone,QKeySequence(QKeySequence.StandardKey.Paste));app.processEvents()
                assert dialog.panel._queued_paths==[str(original)] and dialog.winId()==native
            checks.append('native segmented modes keep one window and original-file Paste')
            report=dict(ok=True,checks=checks,trace=trace,platform=sys.platform,real_native_windows=True)
        except Exception as error:
            report=dict(ok=False,checks=checks,trace=trace,platform=sys.platform,error=str(error));raise
        finally:
            timer.stop();manager.timer.stop();output.write_text(json.dumps(report,indent=2))
            app.clipboard().clear()
            manager.quit();app.processEvents()
    return 0


if __name__=='__main__':raise SystemExit(main())
