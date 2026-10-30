"""Real native utility-window checks, exclusively on disposable CI runners."""
import json,os,sys,tempfile,time
from pathlib import Path
from unittest.mock import patch


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS') not in ('Windows','macOS'):
        raise RuntimeError('This native UI probe is restricted to an ephemeral CI runner')
    os.environ['QT_QPA_PLATFORM']='cocoa' if sys.platform=='darwin' else 'windows'
    from PySide6.QtCore import QObject,Signal,QPoint,QRect,Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import app_settings
    from lan_panel import LanDialog
    from window_chrome import dock_window_state
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    def wait(predicate):
        until=time.monotonic()+5
        while not predicate() and time.monotonic()<until:app.processEvents();time.sleep(.01)
        assert predicate(),'Native window state did not settle'
    class Transport(QObject):
        peers_changed=Signal(list);offer_received=Signal(dict);progress=Signal(dict)
        completed=Signal(dict);status_changed=Signal(str)
        def __init__(self):super().__init__();self._transfers={}
        def peers(self):return []
    report=dict(ok=False,platform=sys.platform,qt_platform=app.platformName(),windows=[])
    with tempfile.TemporaryDirectory(prefix='flickdock-native-utility-') as directory,patch.object(app_settings,'_startup_runtime_allowed',return_value=False):
        store=app_settings.SettingsStore(Path(directory));service=Transport()
        settings=app_settings.SettingsDialog(store);transfer=LanDialog(service,store)
        for window in (settings,transfer):
            window.chrome.fit_screen();window.show();wait(window.isVisible)
            initial=window.size();area=window.screen().availableGeometry()
            window.resize(max(window.minimumWidth(),min(initial.width()-60,area.width()-40)),max(window.minimumHeight(),initial.height()-30))
            app.processEvents();changed=window.size();window.chrome.save_size()
            assert window.chrome.path.is_file()
            if sys.platform=='win32':
                buttons=window.chrome.buttons
                QTest.mouseClick(buttons['max'],Qt.LeftButton,pos=QPoint(1,1));wait(window.isMaximized)
                assert area.adjusted(-2,-2,2,2).contains(window.frameGeometry()),(area,window.frameGeometry())
                QTest.mouseClick(buttons['max'],Qt.LeftButton,pos=QPoint(buttons['max'].width()-2,buttons['max'].height()-2));wait(lambda:not window.isMaximized())
                assert window.size()==changed
                QTest.mouseClick(buttons['min'],Qt.LeftButton,pos=QPoint(1,1));wait(window.isMinimized)
                window.showNormal();wait(lambda:not window.isMinimized())
                import ctypes
                style=ctypes.windll.user32.GetWindowLongW(int(window.winId()),-16)
                report.setdefault('styles',[]).append(dict(thickframe=bool(style&0x40000),minimize=bool(style&0x20000),maximize=bool(style&0x10000)))
                assert all(report['styles'][-1].values()),'Native sizing/min/max capabilities were lost'
            else:
                import AppKit
                assert AppKit.NSApplication.sharedApplication().activationPolicy()==AppKit.NSApplicationActivationPolicyRegular
                window.showMinimized();wait(window.isMinimized)
                assert AppKit.NSApplication.sharedApplication().activationPolicy()==AppKit.NSApplicationActivationPolicyRegular
                window.showNormal();wait(lambda:not window.isMinimized())
            window.close();wait(lambda:not window.isVisible())
            report['windows'].append(dict(title=window.windowTitle(),resize=True,minimize=True,close=True))
        if sys.platform=='darwin':
            import AppKit
            assert AppKit.NSApplication.sharedApplication().activationPolicy()==AppKit.NSApplicationActivationPolicyAccessory
            report['dock_restored_to_menu_bar']=True
        report['ok']=True
        for window in (settings,transfer):window.deleteLater()
        app.processEvents()
    print(json.dumps(report),flush=True)
    if '--output' in sys.argv:Path(sys.argv[sys.argv.index('--output')+1]).write_text(json.dumps(report,indent=2),encoding='utf8')
    return 0


if __name__=='__main__':raise SystemExit(main())
