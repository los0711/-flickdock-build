"""HOSTED-ONLY external Qt clipboard consumer; current exact T1 eventFilter."""
from __future__ import annotations
import argparse,hashlib,sys
from types import SimpleNamespace
from probe_contract import checked_source,encode_frame,path_fingerprint,result,safe_error,scope_record,text_fingerprint

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--scope',required=True);parser.add_argument('--nonce',required=True)
    parser.add_argument('--source-root',required=True);parser.add_argument('--phase',required=True,choices=('legacy-inspect','legacy-control','legacy-filtered'))
    args=parser.parse_args(argv);scope,workspace,record=scope_record(args.scope,args.nonce,role='child')
    source=checked_source(args.source_root,workspace);sys.path.insert(0,str(source))
    # Guard above is deliberately before imports that can initialize native Qt.
    from PySide6.QtCore import QEvent,Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication,QDialog,QLineEdit
    import lan_panel
    app=QApplication([])
    if app.platformName()!='windows':raise RuntimeError('NATIVE_WINDOWS_PLATFORM_REQUIRED')
    value=None;dialog=None;router=None;callback_errors=[]
    sys.excepthook=lambda kind,cause,tb:callback_errors.append(safe_error(cause.with_traceback(tb)))
    try:
        clip=app.clipboard()
        if args.phase=='legacy-inspect':
            mime=clip.mimeData();formats=list(mime.formats()) if mime is not None else []
            has=bool(mime is not None and mime.hasUrls());urls=mime.urls() if has else []
            paths=[url.toLocalFile() for url in urls if url.isLocalFile() and url.toLocalFile()]
            value=result(qt_formats=formats,qt_owns_clipboard=clip.ownsClipboard(),has_urls=has,
                         url_count=len(urls),local_flags=[url.isLocalFile() for url in urls],paths=paths)
        else:
            queues=[];events=[];dialog=QDialog();dialog.panel=SimpleNamespace(queue_paths=lambda paths:queues.append(list(paths)))
            field=QLineEdit(dialog);field.setGeometry(16,16,400,36);dialog.resize(440,88)
            baseline='Lft[replace]Rgt';field.setText(baseline);field.setSelection(3,9)
            if args.phase=='legacy-filtered':
                class RecordingRouter(lan_panel._TransferDropRouter):
                    def eventFilter(self,watched,event):
                        kind=event.type();consumed=super().eventFilter(watched,event)
                        if watched is field and kind in (QEvent.Type.ShortcutOverride,QEvent.Type.KeyPress):
                            events.append(dict(kind=kind.name,consumed=bool(consumed),accepted=event.isAccepted()))
                        return consumed
                router=RecordingRouter(dialog)
            dialog.show();field.setFocus();app.processEvents()
            QTest.keyClick(field,Qt.Key.Key_V,Qt.KeyboardModifier.ControlModifier);app.processEvents()
            pasted=field.text();cursor=field.cursorPosition();selection=(field.selectionStart(),len(field.selectedText()))
            undo_available=field.isUndoAvailable()
            QTest.keyClick(field,Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier);app.processEvents()
            value=result(input_hash=text_fingerprint(pasted),input_unchanged=pasted==baseline,
                         undo_available=undo_available,undo_restored=field.text()==baseline,
                         cursor=cursor,selection=selection,queue_dispatch_count=len(queues),queued_paths=queues,
                         key_events=events,qt_owns_clipboard=clip.ownsClipboard())
        if callback_errors:value=result('error',code='QT_CALLBACK_ERROR',diagnostic=callback_errors)
    except Exception as cause:value=result('error',code=type(cause).__name__,diagnostic=safe_error(cause))
    finally:
        if router is not None:app.removeEventFilter(router)
        if dialog is not None:dialog.hide();dialog.deleteLater();app.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        # Consumer must not clear, replace or flush another process's clipboard value.
    sys.stdout.buffer.write(encode_frame(value));sys.stdout.buffer.flush();return 0
if __name__=='__main__':raise SystemExit(main())
