"""Deterministic offscreen soak, isolated data and clipboard; no native hooks.

This exercises lifecycle and compositor frames, not display/GPU frame rate.
Real sleep, mixed-DPI monitors, remote sessions and codecs need native runs.
"""
import argparse,json,os,sys,tempfile,time,threading
from pathlib import Path
from unittest.mock import patch

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    parser.add_argument('--quick',action='store_true');parser.add_argument('--cycles',type=int,default=50)
    args=parser.parse_args()
    if args.cycles<1:parser.error('--cycles must be positive')
    os.environ['QT_QPA_PLATFORM']='offscreen'
    with tempfile.TemporaryDirectory(prefix='flickdock-soak-') as directory:
        root=Path(directory);os.environ['APPDATA']=str(root/'roaming');os.environ['LOCALAPPDATA']=str(root/'local')
        from PySide6.QtCore import QObject,QEvent,QMimeData,QPoint,Qt,QUrl
        from PySide6.QtGui import QImage,QColor,QDragEnterEvent,QDropEvent
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        from shiboken6 import getAllValidWrappers,isValid
        import test_html_native_workspace as w
        from test_clipboard_preview_v1821 import PreviewTests
        from app_settings import SettingsDialog
        from test_html_native_lan import FakeLan
        from lan_panel import LanDialog
        from crash_diagnostics import CrashDiagnostics,process_snapshot
        import ui_theme,shelf_ui_common
        from media_preview import MediaPreview
        fixture=PreviewTests();fixture.setUp();shelf=fixture.shelf;app=w.APP
        destination=Path(args.output).resolve();destination.parent.mkdir(parents=True,exist_ok=True)
        recorder=CrashDiagnostics(app,destination.with_suffix(''),'1.8.37');samples=[];counts={};errors=[]
        settings=None;transfer=None
        def sample(phase):
            result=dict(phase=phase,**process_snapshot())
            result['qobjects']=sum(isinstance(obj,QObject) and isValid(obj) for obj in getAllValidWrappers())
            samples.append(result);print(json.dumps(result),flush=True)
        def steps(phase,total,action):
            count=min(total,5 if args.quick else args.cycles)
            for i in range(count):
                recorder.note('soak.'+phase)
                action(i)
                if i%25==0:w.flush()
                if i%50==0:sample(phase+':'+str(i))
            counts[phase]=count;sample(phase+':complete')
        def finish_motion():
            morph=shelf.geometry_morph
            if morph.active:
                morph.pause()
                for progress in (.25,.5,.75):morph._frame(progress)
                morph.complete()
            shelf.presentation.stop()
        def expand(_):
            shelf.show_contents();finish_motion();shelf.collapse_contents();finish_motion()
        def upload_round():
            from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
            from cloud_api import CloudApi
            received=set();lock=threading.Lock();results=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*_):pass
                def do_PUT(self):
                    remaining=int(self.headers['Content-Length'])
                    with lock:received.add(self.path)
                    try:
                        while remaining:
                            block=self.rfile.read(min(32768,remaining))
                            if not block:return
                            remaining-=len(block);time.sleep(.005)
                        body=b'{"ok":true}';self.send_response(200);self.send_header('Content-Length',str(len(body)))
                        self.end_headers();self.wfile.write(body)
                    except (ConnectionError,OSError):return
            payload=root/'upload.bin';payload.write_bytes(b'x'*(4*1024*1024))
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            api=CloudApi(f'http://127.0.0.1:{server.server_port}',allow_test_http=True)
            try:
                requests=[api.request('PUT',f'/v1/soak/{i}',lambda value,error:results.append((value,error)),
                    authenticated=False,source=payload,source_size=payload.stat().st_size) for i in range(3)]
                w.until(lambda:len(received)==3,seconds=10);api.cancel(requests[1])
                w.until(lambda:len(results)==3,seconds=20)
                assert sum(error is not None and error.code=='cancelled' for _,error in results)==1
                counts['concurrent_uploads']=3;counts['cancelled_uploads']=1
            finally:
                api.close();server.shutdown();server.server_close();worker.join(2);api.deleteLater();w.flush()

        try:
            w.until(lambda:recorder._installed)
            recorder.configure(theme='dark',motion=True)
            fixture.manager.motion=True;shelf.geometry_morph.motion=True
            popup=fixture.overview()
            image=root/'fixture.png';picture=QImage(128,128,QImage.Format_ARGB32);picture.fill(QColor('#168fbc'));picture.save(str(image))
            large=root/'large-text.txt'
            with large.open('wb') as stream:
                if sys.platform=='win32':
                    import ctypes,msvcrt
                    from ctypes import wintypes
                    kernel=ctypes.WinDLL('kernel32');done=wintypes.DWORD()
                    kernel.DeviceIoControl.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
                    if not kernel.DeviceIoControl(wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno())),0x900C4,None,0,None,0,ctypes.byref(done),None):raise ctypes.WinError()
                stream.write(b'bounded text preview\n');stream.seek(2*1024**3+1);stream.write(b'\n')
            paths=[*fixture.paths,str(image),str(large)]
            for i in range(995):
                file=root/f'file-{i}.txt';file.write_text('soak fixture',encoding='utf8');paths.append(str(file))
            shelf.add_paths('permanent',paths);popup.resume_content();w.until(lambda:popup._build_cursor==len(popup.visible_refs()),seconds=30)
            fixture.manager.show_clipboard_preview();fixture.manager.clipboard_preview_dialog.hide()
            settings=SettingsDialog(fixture.settings);transfer=LanDialog(FakeLan(),fixture.settings)
            for window in (settings,transfer):window.show();w.flush();window.hide()
            for _ in range(5):expand(0)
            popup.change_view_mode('icons');popup.resume_content();w.flush()
            popup.change_view_mode('list');popup.resume_content();w.flush()
            sample('cold_components_initialized')
            for ref in paths[:5]:
                popup.preview.show_path(ref)
                w.until(lambda:popup.preview.copy_button.isEnabled(),seconds=10)
                w.flush()
            popup.preview.clear_preview();upload_round();QTest.qWait(100);w.flush()
            sample('baseline_after_warmup')
            with patch('native_ui_common.reduced_motion',return_value=False):
                steps('expand_collapse',500,expand)
                shelf.show_contents();finish_motion();popup.resume_content()
                def theme(i):
                    import theme_accent
                    value='dark' if i%2 else 'light';accent=('#000000','#7C3AED','#22C55E')[i%3]
                    fixture.settings.update(theme=value,accent_color=accent);theme_accent.set_color(accent)
                    recorder.configure(theme=value,motion=True)
                    ui_theme.set_theme(value);ui_theme.apply_application(app,shelf_ui_common.FONT_FAMILY)
                    settings.apply_theme(value);transfer.apply_theme(value);shelf.refresh();w.flush()
                steps('theme_switch',100,theme)
                def view(i):
                    popup.change_view_mode('icons' if i%2 else 'list');popup.resume_content();w.flush()
                steps('view_switch',200,view)
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(image))])
                def drag(i):
                    event=QDragEnterEvent(QPoint(10,10),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                    QApplication.sendEvent(popup,event)
                    QApplication.sendEvent(popup,QEvent(QEvent.DragLeave))
                steps('drag_enter_leave',100,drag)
                def preview(i):
                    popup.preview.show_path(paths[i%len(paths)]);w.flush()
                steps('preview',200,preview)
                def clipboard(i):
                    fixture.clip.text('disposable soak '+str(i));w.flush()
                steps('clipboard',500,clipboard)
                w.flush()
                upload_round()
            sample('before_cleanup')
        except BaseException as error:
            errors.append(type(error).__name__+': '+str(error));raise
        finally:
            for window in (settings,transfer):
                if window is not None:window.hide();window.deleteLater()
            fixture.tearDown();sample('cleanup_requested')
            # QNetworkAccessManager deletes its connection-thread objects
            # asynchronously. Measure after that documented Qt shutdown turn.
            QTest.qWait(500);w.flush();sample('after_cleanup');recorder.wait_closed()
            baseline=next(row for row in samples if row['phase']=='baseline_after_warmup');last=samples[-1]
            limits=dict(rss_growth=(last.get('rss_bytes',0)/max(1,baseline.get('rss_bytes',1))-1),
                python_thread_delta=last['threads']-baseline['threads'],
                user_handle_delta=last.get('user_handles',0)-baseline.get('user_handles',0),
                gdi_handle_delta=last.get('gdi_handles',0)-baseline.get('gdi_handles',0))
            if limits['rss_growth']>=.20:errors.append('RSS growth exceeded 20%')
            if abs(limits['python_thread_delta'])>2:errors.append('Python threads did not return to baseline +/-2')
            if limits['user_handle_delta']>0 or limits['gdi_handle_delta']>0:errors.append('GUI handle growth needs investigation')
            report=dict(ok=not errors,counts=counts,samples=samples,errors=errors,
                limits=limits,
                animation_clock='deterministic compositor seeks; not native frame-rate measurement',
                pending_native=['sleep/wake','mixed DPI monitors','remote desktop/IME','real codec playback','Wi-Fi/LAN physical link'])
            Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if not errors else 1

if __name__=='__main__':raise SystemExit(main())
