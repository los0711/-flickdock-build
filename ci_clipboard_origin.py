"""Native clipboard provenance checks. Refuses to run on a user's desktop."""
import argparse,json,os,subprocess,sys,tempfile,time
from pathlib import Path


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS') not in ('Windows','macOS'):
        raise RuntimeError('Native clipboard QA requires an ephemeral CI runner')
    parser=argparse.ArgumentParser();parser.add_argument('--editor',type=Path);parser.add_argument('--output',type=Path,default=Path('clipboard-origin-native.json'))
    args=parser.parse_args()
    os.environ['QT_QPA_PLATFORM']='cocoa' if sys.platform=='darwin' else 'windows'
    from PySide6.QtCore import QTimer,QMimeData,QUrl,Qt
    from PySide6.QtGui import QKeySequence,QImage,QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication,QLineEdit
    app=QApplication([]);app.setQuitOnLastWindowClosed(False)
    if args.editor:
        # A separate desktop process: edits do not share objects or callbacks
        # with the receiver. This is an external-app fixture, not Notepad/Safari.
        edit=QLineEdit();edit.resize(400,60);edit.show();seen=[-1]
        def command():
            path=args.editor/'command.json'
            try:value=json.loads(path.read_text())
            except (OSError,ValueError):return
            if value['id']==seen[0]:return
            seen[0]=value['id'];action=value['action'];edit.setFocus()
            if action=='type':edit.clear();QTest.keyClicks(edit,'ordinary external input 123')
            elif action in ('shortcut','context'):
                edit.setText(value['text']);edit.selectAll()
                if action=='shortcut':QTest.keySequence(edit,QKeySequence(QKeySequence.StandardKey.Copy))
                else:
                    menu=edit.createStandardContextMenu()
                    copies=[action for action in menu.actions() if 'copy' in action.text().replace('&','').lower()]
                    assert len(copies)==1,'Standard editor Copy action missing'
                    copies[0].trigger();menu.deleteLater()
            elif action=='quit':app.quit()
            (args.editor/'ack.json').write_text(json.dumps({'id':seen[0]}))
        timer=QTimer();timer.timeout.connect(command);timer.start(20)
        return app.exec()

    from clipboard_sync import ClipboardSync,native_sequence,SYNC_MARKER
    from test_clipboard_sync_v1819 import Clipboard,Events,Account,Hub
    from verify_clipboard import assert_native_paste
    import clipboard_audit
    checks=[]
    def wait(predicate,seconds=10):
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:app.processEvents();time.sleep(.003)
        assert predicate(),'Native clipboard operation timed out'
    def settle():
        end=time.monotonic()+.35
        while time.monotonic()<end:app.processEvents();time.sleep(.003)
    with tempfile.TemporaryDirectory(prefix='flickdock-origin-native-') as directory:
        base=Path(directory);hub=Hub();peer=Clipboard();clips=[app.clipboard(),peer];clients=[]
        clipboard_audit.start(base)
        clipboard_audit.set_text(clips[0],'synthetic initial clipboard',module='ci_clipboard_origin',function='main',trigger='fixture',origin='internal')
        for index,clip in enumerate(clips):
            events=Events();hub.events.append(events)
            clients.append(ClipboardSync(Account(hub.api(str(index)),str(index)),events,base/str(index),clipboard=clip))
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--editor',str(base)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        command_id=0
        def command(action,**values):
            nonlocal command_id
            command_id+=1;pending=base/'command.tmp'
            pending.write_text(json.dumps(dict(id=command_id,action=action,**values)));pending.replace(base/'command.json')
            def acknowledged():
                if child.poll() is not None:raise AssertionError('External editor exited: '+child.stderr.read().decode('utf8',errors='replace'))
                try:return json.loads((base/'ack.json').read_text())['id']==command_id
                except (OSError,ValueError):return False
            wait(acknowledged)
        def puts():return sum(method=='PUT' for method,_ in hub.calls)
        try:
            wait(lambda:all(not client._fetching for client in clients))
            before=native_sequence();command('type');settle()
            assert native_sequence()==before and puts()==0,'Typing changed clipboard or uploaded content'
            checks.append('separate native editor typing preserves clipboard')
            for action in ('shortcut','context'):
                command(action,text='synthetic '+action+' copy')
                wait(lambda:peer.mimeData().text()=='synthetic '+action+' copy');settle()
                assert puts()==len(checks),'One native Copy must create one upload'
                assert_native_paste('text','synthetic '+action+' copy')
                checks.append('native '+action+' copy uploads once')
            before=puts();peer.text('synthetic cloud receive');wait(lambda:clips[0].text()=='synthetic cloud receive');settle()
            assert puts()==before+1 and clips[0].mimeData().hasFormat(SYNC_MARKER)
            checks.append('native cloud receive never echoes')
            sequence=native_sequence();before=puts();command('type');settle()
            assert native_sequence()==sequence and puts()==before
            checks.append('typing while cloud payload is active never uploads')
            source=base/'original.bin';source.write_bytes(b'synthetic original file'*4096)
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);before=puts();peer.setMimeData(mime)
            wait(lambda:clips[0].mimeData().hasUrls());settle()
            copied=Path(clips[0].mimeData().urls()[0].toLocalFile())
            assert copied.read_bytes()==source.read_bytes() and puts()==before+1
            assert_native_paste('files',str(copied));checks.append('native original file receive preserves bytes without echo')
            image=QImage(64,48,QImage.Format.Format_RGB32);image.fill(QColor('blue'));mime=QMimeData();mime.setImageData(image)
            before=puts();peer.setMimeData(mime);wait(lambda:clips[0].mimeData().hasImage());settle()
            assert puts()==before+1;assert_native_paste('image');checks.append('native image receive never echoes')
            args.output.write_text(json.dumps(dict(ok=True,platform=sys.platform,qt=app.platformName(),checks=checks,
                real_os_clipboard=True,external_application='separate Qt editor fixture',physical_cross_device=False),indent=2))
        finally:
            if child.poll() is None:child.terminate()
            try:child.wait(5)
            except subprocess.TimeoutExpired:child.kill();child.wait(5)
            child.stderr.close()
            for client in clients:client.close()
            wait(lambda:all(not client._jobs for client in clients));clipboard_audit.stop()
            if (base/'diagnostics'/'clipboard-writes.jsonl').exists():
                args.output.with_suffix('.trace.jsonl').write_bytes((base/'diagnostics'/'clipboard-writes.jsonl').read_bytes())
    return 0


if __name__=='__main__':raise SystemExit(main())
