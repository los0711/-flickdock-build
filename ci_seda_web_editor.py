"""Exercise real Windows UIA against two controlled Seda-style Electron webviews."""
import json
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
from time import sleep
from unittest.mock import patch

from ci_live_translation_web import wait_for
from live_translation_editor import focused_editor

SOURCE='这个聊天输入框里的原文需要替换。'
TARGET='Replace this draft, please. 😀'
CLIPBOARD='FlickDock test original clipboard'


def verify(folder, electron, editor):
    import clipboard_history_native as native
    folder.mkdir()
    child=subprocess.Popen([electron,str(Path(__file__).with_suffix('.cjs')),str(folder),editor])
    try:
        def sample():
            try:return json.loads((folder/'state.json').read_bytes())
            except (OSError,ValueError):return {}

        assert wait_for(lambda:sample().get('focused'),interval=14), 'Electron webview never became focused'
        point=sample()['point']
        user=ctypes.windll.user32
        assert user.SetCursorPos(*point)
        sleep(.08)
        user.mouse_event(2,0,0,0,0)
        sleep(.05)
        user.mouse_event(4,0,0,0,0)
        stop=Event()
        state=wait_for(lambda:focused_editor(stop),interval=8)
        if state is None:
            from live_translation_editor import _windows_editor
            problems=[]
            _windows_editor(stop,None,None,False,False,rejected=problems.append)
            return dict(accessible=False,focus=sample().get('focused'),problems=problems)
        flags=[]
        observations=[]
        actual_reader=focused_editor
        def read(*args,**options):
            current=actual_reader(*args,**options)
            if len(observations)<24:
                observations.append((current.selection, len(current.text))
                                    if current is not None else None)
            return current
        selector=native.select_editor_text
        paste=native.paste_editor_text
        typed=getattr(native,'type_windows_editor_text',None)
        def event(link,func):
            def run(*args,**kwargs):
                result=func(*args,**kwargs)
                flags.append((link,bool(result),len(sample().get('selected',''))))
                return result
            return run
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch('live_translation_editor.focused_editor',side_effect=read))
            stack.enter_context(patch.object(native,'select_editor_text',event('keyboard_selection',selector)))
            stack.enter_context(patch.object(native,'paste_editor_text',event('paste',paste)))
            if typed is not None:stack.enter_context(patch.object(native,'type_windows_editor_text',event('unicode',typed)))
            accepted=focused_editor(stop,expected=state,insertion=TARGET)
        wait_for(lambda:sample().get('text')==TARGET and sample().get('clipboard')==CLIPBOARD,interval=3)
        observed=sample()
        return dict(accessible=True,original_matches=state.text.strip()==SOURCE, accepted=bool(accepted),
                    actually_replaced=observed.get('text')==TARGET,input_events=observed.get('inputs'),
                    original_preserved=observed.get('text')==SOURCE,
                    adjacent_preserved=observed.get('other')=='Other input must remain unchanged',
                    clipboard_preserved=observed.get('clipboard')==CLIPBOARD, events=flags,
                    **({} if accepted else {'selection_reads':observations,
                            'terminal_codepoint':ord(state.text[-1]) if state.text else None}))
    finally:
        (folder/'exit').touch()
        try:child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.terminate();child.wait(timeout=5)


if __name__=='__main__':
    if os.environ.get('GITHUB_ACTIONS')!='true' or sys.platform!='win32':
        raise RuntimeError('The Electron input probe requires a disposable Windows desktop.')
    report={}
    try:
        with tempfile.TemporaryDirectory(prefix='flickdock-seda-webview-') as scratch:
            for app in ('whatsapp','telegram'):
                report[app]=verify(Path(scratch)/app,sys.argv[1],app)
    finally:
        Path(sys.argv[2]).write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
    for app,result in report.items():
        assert all(result.get(key) for key in ('accessible','original_matches','accepted','actually_replaced',
            'input_events','adjacent_preserved','clipboard_preserved')), (app,result)
