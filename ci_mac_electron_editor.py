"""Native input and a close-to-production rich Electron editor on a clean Mac."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import sleep
from unittest.mock import patch

import Quartz

from ci_live_translation_web import wait_for
from clipboard_history_native import front_target, mac_history_paste_blocker
from live_translation_editor import _mac_editor, focused_editor


SOURCE = '这个聊天输入框里的原文需要替换。'
TARGET = 'Replace this draft, please. 😀'
CLIPBOARD = 'FlickDock test original clipboard'


def verify(folder, electron, editor, *, deny_clipboard):
    folder.mkdir()
    fixture = Path(__file__).with_name('ci_seda_web_editor.cjs')
    process = subprocess.Popen([electron, str(fixture), str(folder), editor])
    try:
        def sample():
            try:
                return json.loads((folder / 'state.json').read_bytes())
            except (OSError, ValueError):
                return {}

        assert wait_for(lambda: sample().get('point'), interval=10), 'No Electron editor frame'
        x, y = sample()['point']
        for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            event = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            sleep(.08)
        cancel = Event()
        expected = wait_for(lambda: focused_editor(cancel), interval=8)
        if expected is None:
            reasons = []
            _mac_editor(cancel,None,None,False,rejected=reasons.append)
            return dict(read=False,reasons=reasons,external=front_target()==process.pid)
        permission = (patch('clipboard_history_native.mac_history_paste_blocker',return_value='clipboard')
                      if deny_clipboard else nullcontext())
        with permission:
            result = focused_editor(cancel,expected=expected,insertion=TARGET)
        wait_for(lambda:sample().get('text')==TARGET and sample().get('clipboard')==CLIPBOARD,interval=3)
        observed=sample()
        return dict(read=expected.text.strip()==SOURCE,accepted=bool(result),blocked=deny_clipboard,
                    adapter=mac_history_paste_blocker(), status=getattr(result,'name',None),
                    target=observed.get('text')==TARGET,inputs=observed.get('inputs',0),
                    selected=len(observed.get('selected','')),
                    neighbors=observed.get('other')=='Other input must remain unchanged',
                    clipboard=observed.get('clipboard')==CLIPBOARD)
    finally:
        (folder/'exit').touch()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


if __name__=='__main__':
    if sys.platform!='darwin' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('Requires a disposable Mac runner')
    report={}
    try:
        with TemporaryDirectory(prefix='flickdock-native-electron-') as path:
            for editor in ('whatsapp','telegram'):
                report[editor]=verify(Path(path)/editor,sys.argv[1],editor,deny_clipboard=editor=='telegram')
    finally:
        Path(sys.argv[2]).write_text(json.dumps(report),encoding='utf-8')
        print(json.dumps(report),flush=True)
    for editor,result in report.items():
        assert all(result.get(k) for k in ('read','accepted','target','inputs','neighbors','clipboard')), (editor,result)
