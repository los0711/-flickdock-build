"""50 isolated translator lifecycles. No real clipboard, audio or accounts."""
import argparse,gc,json,sys,traceback
from pathlib import Path
from PySide6.QtCore import QCoreApplication,QEvent,QObject
from shiboken6 import getAllValidWrappers,isValid
from test_translator_v1844 import NativeFixTests,APP,settle
from crash_diagnostics import process_snapshot


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--cycles',type=int,default=50);args=parser.parse_args()
    errors=[];samples=[];previous=sys.excepthook
    sys.excepthook=lambda *value:errors.append(''.join(traceback.format_exception(*value)))
    def sample(cycle):
        gc.collect();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents()
        samples.append(dict(cycle=cycle,**process_snapshot(),qobjects=sum(isinstance(v,QObject) and isValid(v) for v in getAllValidWrappers())))
    try:
        for index in range(args.cycles+5):
            fixture=NativeFixTests();fixture.setUp()
            try:
                fixture.test_real_close_button_stays_closed_after_refresh()
                fixture.test_delay_popup_has_no_mouse_replay_and_second_toggle_closes()
                fixture.test_source_compare_can_be_closed_and_target_scope_does_not_redirect_source_undo()
                fixture.view.hide();fixture.view.show();fixture.view.refresh()
            finally:fixture.tearDown()
            del fixture;gc.collect();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);settle(timeout=.005)
            if index==4 or index==args.cycles+4:sample(max(0,index-4))
        before,after=samples
        assert not errors,errors
        assert after['rss_bytes']<before['rss_bytes']*1.2,(before,after)
        assert abs(after['threads']-before['threads'])<=2,(before,after)
        assert after['qobjects']<=before['qobjects']+8,(before,after)
        for key in ('gdi_handles','user_handles'):assert after[key]<=before[key]+8,(before,after)
        result=dict(ok=True,cycles=args.cycles,warmup=5,samples=samples,python_errors=errors,offscreen=True,real_screen_fps=False)
    except Exception:
        result=dict(ok=False,samples=samples,python_errors=errors,traceback=traceback.format_exc());raise
    finally:
        sys.excepthook=previous;Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
