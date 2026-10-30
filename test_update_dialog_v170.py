"""Offscreen update UI flow with a signal-only client; no network/installer."""
import os
from pathlib import Path
import sys
import tempfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = sys.argv[1] if len(sys.argv)>1 else '1.25'

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from update_dialog import UpdateDialog


class FakeClient(QObject):
    checking = Signal(bool)
    update_available = Signal(dict, bool)
    up_to_date = Signal(bool)
    failed = Signal(str, bool)
    progress = Signal(int, int)
    verified_file = Signal(str, dict, bool)
    cancelled = Signal(bool)
    def __init__(self):
        super().__init__()
        self.check_calls = []
        self.download_calls = []
        self.cancel_calls = 0
    def check(self, manual=False):
        self.check_calls.append(manual)
        self.checking.emit(manual)
        return True
    def download(self, manifest=None, manual=True):
        self.download_calls.append((manifest, manual))
        return True
    def cancel(self):
        self.cancel_calls += 1
        self.cancelled.emit(True)


app = QApplication([])
assert QFontDatabase.addApplicationFont(str(Path(__file__).with_name('fonts')/'HarmonyOS_Sans_SC_Regular.ttf'))>=0
app.setFont(QFont('HarmonyOS Sans SC', 11))


def flush():
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


manifest = {'version':'1.7.1', 'published_at':'2026-10-24T09:00:00+08:00',
    'notes':'修复拖入后的动画\n改善设置与更新体验\n<a href="https://example.invalid">说明作为纯文本</a>',
    'installer':{'url':'https://example.invalid/setup.exe', 'size':4096, 'sha256':'0'*64}}

with tempfile.TemporaryDirectory(prefix='flickdock-update-ui-v170-') as temporary:
    client = FakeClient()
    dialog = UpdateDialog('1.7.0')
    dialog.bind_client(client)
    dialog.bind_client(client)
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not dialog.windowFlags() & Qt.WindowType.WindowCloseButtonHint
    assert not dialog.isVisible() and not dialog.isModal()
    assert dialog.current_version=='1.7.0' and dialog.state=='idle'
    assert dialog.font().family()=='HarmonyOS Sans SC'
    installed, downloads, checks, dismissals, skipped = [], [], [], [], []
    dialog.request_install.connect(lambda: installed.append(dialog.verified_path))
    dialog.request_download.connect(lambda: downloads.append(dialog.latest_version))
    dialog.request_check.connect(lambda: checks.append(True))
    dialog.dismissed.connect(lambda: dismissals.append(True))
    dialog.skipped.connect(skipped.append)

    # Root controls automatic notifications: receiving any background result
    # updates presentation but never opens or activates this dialog.
    client.checking.emit(False)
    client.up_to_date.emit(False)
    assert dialog.state=='latest' and not dialog.isVisible()
    client.checking.emit(False)
    client.failed.emit('离线时无法检查更新，请稍后重试。', False)
    assert dialog.state=='error' and not dialog.isVisible()
    client.checking.emit(False)
    client.update_available.emit(manifest, False)
    assert dialog.state=='available' and not dialog.isVisible() and not installed
    assert dialog.release_notes.toPlainText()==manifest['notes']
    assert dialog.current_version_label.text()=='1.7.0'
    assert dialog.latest_version_label.text()=='1.7.1'
    print('PASS automatic latest/error/available signals never show UI or install; versions and release notes are plain text')

    dialog.set_state('idle')
    dialog.show(); flush()
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    assert dialog.state=='checking' and len(client.check_calls)==len(checks)==1
    assert client.check_calls[-1] is True and not dialog.primary_button.isEnabled()
    client.update_available.emit(manifest, True)
    assert dialog.state=='available' and dialog.primary_button.text()=='立即下载并更新'
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    assert dialog.state=='downloading' and len(client.download_calls)==len(downloads)==1
    assert client.download_calls[-1]==(manifest, True) and not installed
    client.progress.emit(1024, 4096)
    assert dialog.progress_bar.value()==25 and '25%' in dialog.progress_label.text()
    client.progress.emit(2048, 0)
    assert dialog.progress_bar.maximum()==0 and '2 KB' in dialog.progress_label.text()
    installer = Path(temporary)/'verified-test-only.exe'
    installer.write_bytes(b'This is inert test data, never executed.')
    client.verified_file.emit(str(installer), manifest, True)
    assert dialog.state=='ready' and dialog.verified_path==str(installer)
    assert dialog.progress_bar.value()==100 and not installed
    assert dialog.primary_button.text()=='重启并安装'
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    assert installed==[str(installer)] and not dialog.primary_button.isEnabled()
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    assert len(installed)==1
    dialog.show_error('测试安装被接收方拒绝。', operation='install')
    assert dialog.primary_button.text()=='重试安装'
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    assert len(installed)==2
    print('PASS actual Check/Download/Install clicks, known/unknown progress and verified ready state; installation requires explicit user action')

    client.checking.emit(True)
    client.update_available.emit(manifest, True)
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    client.failed.emit('连接中断，请重试。', True)
    assert dialog.state=='error' and dialog.primary_button.text()=='重试下载'
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    assert len(client.download_calls)==3
    QTest.mouseClick(dialog.secondary_button, Qt.MouseButton.LeftButton)
    assert dialog.state=='cancelled' and client.cancel_calls==1
    client.progress.emit(4096, 4096)
    client.verified_file.emit(str(installer), manifest, True)
    assert dialog.state=='cancelled' and not dialog.verified_path and len(installed)==2
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    assert dialog.state=='checking' and len(client.check_calls)==2
    QTest.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
    flush()
    assert not dialog.isVisible() and client.cancel_calls==2 and len(dismissals)==1
    print('PASS retry download, Cancel, ignored late progress/verified signals and close during checking cancels once')

    dialog.show(); flush()
    client.checking.emit(True)
    client.update_available.emit(manifest, True)
    QTest.mouseClick(dialog.skip_button, Qt.MouseButton.LeftButton)
    flush()
    assert skipped==['1.7.1'] and not dialog.isVisible() and len(dismissals)==2
    dialog.show(); flush()
    client.checking.emit(True)
    client.up_to_date.emit(True)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    flush()
    assert not dialog.isVisible() and len(dismissals)==3
    print('PASS Skip identifies the offered version; Later/close/Esc only dismiss and do not install')

    render_dir = os.getenv('FLICKDOCK_UPDATE_RENDER_DIR')
    if render_dir: Path(render_dir).mkdir(parents=True, exist_ok=True)
    for theme in ('light', 'dark'):
        dialog.apply_theme(theme)
        for state in ('idle', 'checking', 'available', 'latest', 'error', 'downloading', 'ready', 'cancelled'):
            dialog.set_state(state, manifest=manifest, error='无法连接更新服务，请稍后重试。',
                received_bytes=1024, total_bytes=4096, verified_path=str(installer))
            dialog.resize(500, 520)
            dialog.show(); flush()
            assert dialog.rect().contains(dialog.primary_button.geometry()), (theme, state, dialog.size())
            assert dialog.rect().contains(dialog.secondary_button.geometry()), (theme, state)
            assert dialog.title_bar.rect().contains(dialog.close_button.geometry()), (theme, state)
            if dialog.release_notes.isVisible():
                assert dialog.rect().contains(dialog.release_notes.geometry()), (theme, state)
            assert dialog.close_button.dark==(theme=='dark')
            if render_dir and state in ('available', 'downloading', 'ready'):
                dialog.grab().save(str(Path(render_dir)/f'update-{theme}-{state}-{os.environ["QT_SCALE_FACTOR"]}.png'))
        dialog.set_state('latest')
        dialog.reject(); flush()
    print('PASS light/dark layouts, footer visibility, Harmony typography and centered close target at DPI', os.environ['QT_SCALE_FACTOR'])

    newer = FakeClient()
    dialog.bind_client(newer)
    dialog.set_state('idle')
    before = dialog.state
    client.update_available.emit(manifest, True)
    assert dialog.state==before, 'Old client remained connected after rebinding'
    dialog.show(); flush()
    QTest.mouseClick(dialog.primary_button, Qt.MouseButton.LeftButton)
    assert newer.check_calls==[True] and len(client.check_calls)==2
    dialog.close_button.click(); flush()
    dialog.bind_client(None)
    dialog.deleteLater(); flush()
    print('PASS bind/rebind has no duplicate requests or old-client updates; dialog cleanup performs no network or execution')

    # Exercise the exact production client signal bridge too. An empty
    # endpoint must fail locally before any network request can be made.
    from update_client import UpdateClient
    class NoNetwork:
        calls = 0
        def get(self, request):
            self.calls += 1
            raise AssertionError('This UI test must not make any network request')
    network = NoNetwork()
    real = UpdateClient('1.7.0', Path(temporary)/'unused-updates', endpoint='', network=network)
    production_dialog = UpdateDialog('1.7.0')
    production_dialog.bind_client(real)
    assert real.check(manual=False)
    assert production_dialog.state=='error' and not production_dialog.isVisible()
    production_dialog.show(); flush()
    QTest.mouseClick(production_dialog.primary_button, Qt.MouseButton.LeftButton)
    assert production_dialog.state=='error' and production_dialog.isVisible()
    assert network.calls==0 and not real.busy
    production_dialog.reject(); production_dialog.bind_client(None)
    real.close(); production_dialog.deleteLater(); flush()
    print('PASS real UpdateClient binding: an unconfigured endpoint fails locally with no request, automatic popup or installation')

print('UPDATE_DIALOG_V170_OK')
