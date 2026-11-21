"""Exercise native Finder copy drops in an isolated Apple Silicon CI session."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QCursor


def enabled(report_path):
    runner_temp = os.environ.get('RUNNER_TEMP')
    if (sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.environ.get('QT_QPA_PLATFORM') != 'cocoa' or not runner_temp):
        return False
    scratch = Path(runner_temp).resolve()
    home = Path.home().resolve()
    return home != scratch and home.is_relative_to(scratch) and report_path.resolve().is_relative_to(scratch)


class ShelfShows(QObject):
    def __init__(self):
        super().__init__()
        self.count = 0

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show:
            self.count += 1
        return False


class DragEvents(QObject):
    def __init__(self):
        super().__init__()
        self.events = []

    def eventFilter(self, watched, event):
        name=event.type().name
        if name in ('DragEnter','DragMove','DragLeave','Drop') and watched.objectName()=='flickdockMagneticFileDestination':
            pos=event.position().toTuple() if hasattr(event,'position') else None
            self.events.append((name,pos,watched.geometry().getRect(),QCursor.pos().toTuple()))
            del self.events[:-60]
        return False


def run(manager, report_path):
    import AppKit
    import Quartz
    from app_version import APP_VERSION
    from display_triggers import display_id
    from platform_native import mouse_buttons_mask

    report = dict(ok=False, version=APP_VERSION, frozen=bool(getattr(sys, 'frozen', False)),
                  arch=platform.machine(), source='com.apple.finder', cases={}, native_events=[])
    shows = ShelfShows()
    native = DragEvents()
    manager.app.installEventFilter(native)
    shelf = manager.shelf
    shelf.installEventFilter(shows)
    manager.edge_file_drag.note = report['native_events'].append
    stage = 'initialize'
    held = False

    def sample(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            manager.app.processEvents()
            time.sleep(.008)

    def until(predicate, message, seconds=4):
        end = time.monotonic() + seconds
        while not predicate():
            assert time.monotonic() < end, message
            sample(.025)

    def post(kind, point):
        event = Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft)
        assert event is not None, 'Could not create a Quartz pointer event'
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def glide(start, end, *, steps=18, delay=.022):
        for tick in range(1,steps+1):
            ratio = tick/steps
            point = tuple(left+(right-left)*ratio for left,right in zip(start,end))
            post(Quartz.kCGEventLeftMouseDragged, point)
            sample(delay)

    def finder_start(fixture):
        # Finder owns the pasteboard and the drag session. Quartz merely moves
        # the pointer, presses and releases like a user on the disposable Mac.
        script = '''
on run argv
    with timeout of 7 seconds
        tell application "Finder"
            activate
            reveal (POSIX file (item 1 of argv) as alias)
            set bounds of front Finder window to {85, 80, 650, 495}
        end tell
    end timeout
end run
'''
        subprocess.run(['osascript','-e',script,str(fixture)], check=True, timeout=11,
                       capture_output=True, text=True)
        sample(.4)
        helper = os.environ['FLICKDOCK_FINDER_AX']
        result = subprocess.run([helper,fixture.name], check=False, timeout=8, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr.strip()
        point = tuple(map(float,result.stdout.split()))
        assert len(point)==2 and 85<=point[0]<=650 and 80<=point[1]<=495, point
        front=AppKit.NSWorkspace.sharedWorkspace().frontmostApplication().bundleIdentifier()
        assert front == 'com.apple.finder', f'Foreground app was {front}'
        return point

    def press(point):
        nonlocal held
        post(Quartz.kCGEventMouseMoved, point); sample(.1)
        post(Quartz.kCGEventLeftMouseDown, point)
        held=True
        sample(.12)
        assert mouse_buttons_mask()&1, 'Cocoa did not observe the pressed mouse button'

    def release(point):
        nonlocal held
        post(Quartz.kCGEventLeftMouseUp, point)
        held=False

    def check_intake(fixture):
        until(lambda:shelf.files['permanent'].count(str(fixture))==1, 'Finder file did not land exactly once')
        expected=fixtures[fixture]
        assert hashlib.sha256(fixture.read_bytes()).hexdigest()==expected, 'Finder original was changed'
        saved=json.loads((Path.home()/'Library/Application Support/FlickDock/shelves.json').read_text('utf-8'))
        assert saved['shelf']['permanent'].count(str(fixture))==1, 'Shelf file was not persisted exactly once'

    try:
        assert report['arch']=='arm64' and manager.app.platformName()=='cocoa'
        assert Quartz.CGPreflightPostEventAccess(), 'Quartz pointer permission unavailable'
        screen=manager.edge.display
        area=screen.geometry()
        manager.settings.update(dock_edge='right',dock_length=110,dock_position=43,dock_thickness=12,
            keep_empty_handle=True,trigger_visible=True,trigger_view='compact',capture_mode='reference',
            sync_upload_enabled=False,sync_receive_enabled=False,magnetic_edges={'enabled':True,'displays':{
                display_id(screen):{'right':{'enabled':True,'width':140,'length':100,'position':50}}
            }})
        manager.update_handles()
        manager.timer.start(20)
        trigger=manager.edge.file_preview_region()
        edge_x=area.right()+.75
        strip_point=(edge_x,trigger.center().y())
        magnetic_point=(edge_x,min(area.bottom()-80,trigger.bottom()+110))
        assert magnetic_point[0]>650 and magnetic_point[1]>trigger.bottom()
        report['screen']={'geometry':area.getRect(),'trigger':trigger.getRect(),'scale':screen.devicePixelRatio()}
        fixture_dir=report_path.parent/'finder-fixtures';fixture_dir.mkdir()
        fixtures={}
        for index, name in enumerate(('FlickDock-Magnetic-Fixture.txt','FlickDock-Preview-Fixture.txt'),1):
            fixture=fixture_dir/name
            payload=(f'FlickDock native Finder fixture {index}\n').encode()
            fixture.write_bytes(payload)
            fixtures[fixture]=hashlib.sha256(payload).hexdigest()
        magnetic_file,preview_file=fixtures

        stage='outer_edge_hover'
        post(Quartz.kCGEventMouseMoved, magnetic_point);sample(.28)
        assert shows.count==0 and not manager.edge_absorb.overlay.isVisible(), 'Bare pointer activated the magnet'
        post(Quartz.kCGEventMouseMoved, strip_point)
        until(shelf.isVisible,'Outermost mouse sample did not reveal the trigger strip')
        assert shelf.presentation_kind=='dock'
        report['cases'][stage]={'ok':True,'global_pointer':QCursor.pos().toTuple()}
        post(Quartz.kCGEventMouseMoved,(area.center().x(),magnetic_point[1]))
        until(lambda:not shelf.isVisible(),'First hover preview did not close')
        sample(.2)

        stage='magnetic_only'
        start=finder_start(magnetic_file)
        count=shows.count
        press(start)
        bend=(area.right()-180,magnetic_point[1])
        glide(start,bend)
        glide(bend,magnetic_point)
        report['magnetic_pointer']=QCursor.pos().toTuple()
        until(lambda:manager.edge_file_drag.validated and manager.edge_absorb.intensity>.05,
              'Native Finder drag did not light the magnetic band')
        def native_icon_available():
            icon=manager.edge_absorb.icons.image(str(magnetic_file))
            return icon is not None and not icon.isNull()
        until(native_icon_available,'The native icon subprocess did not return artwork')
        for delta in (44,8,45,5):
            point=(edge_x-delta,magnetic_point[1])
            post(Quartz.kCGEventLeftMouseDragged,point);sample(.028)
        post(Quartz.kCGEventLeftMouseDragged,magnetic_point);sample(.22)
        assert manager.edge_file_drag.validated, 'Native magnetic target lost the active drag before release'
        assert shows.count==count and not shelf.isVisible(), 'Magnet revealed the shelf before release'
        release(magnetic_point)
        check_intake(magnetic_file)
        sample(.55)
        assert shows.count==count and not shelf.isVisible(), 'Magnet revealed the shelf during landing'
        assert report['native_events'].count('edge.magnetic.drop_accepted')==1
        report['cases'][stage]={'ok':True,'shelf_shows':shows.count-count,'source_sha256':fixtures[magnetic_file]}

        stage='trigger_strip'
        start=finder_start(preview_file)
        press(start)
        glide(start,bend);glide(bend,magnetic_point)
        until(lambda:manager.edge_file_drag.validated,'Second native Finder drag never reached magnetic band')
        assert shows.count==count and not shelf.isVisible(), 'Preview appeared before the pointer entered the strip'
        glide(magnetic_point,strip_point,steps=15)
        until(shelf.isVisible,'Finder drag in the exact trigger strip did not open preview')
        assert shelf.presentation_kind=='dock'
        until(lambda:shelf.visible_state=='floating' or not shelf.geometry_morph.active,
              'Dock preview geometry did not settle')
        sample(.35)
        point=shelf.visual_geometry().center()
        glide(strip_point,(point.x(),point.y()),steps=12)
        release((point.x(),point.y()))
        check_intake(preview_file)
        sample(.35)
        assert shows.count==count+1 and report['native_events'].count('edge.magnetic.preview_drop_accepted')==1
        assert report['native_events'].count('edge.magnetic.drop_accepted')==1
        report['cases'][stage]={'ok':True,'shelf_shows':shows.count-count,'source_sha256':fixtures[preview_file]}
        report['ok']=True
    except (AssertionError, KeyError, OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        report['failed_at']=stage
        report['traceback']=traceback.format_exc()
        report['state']={'pointer':QCursor.pos().toTuple(),'shelf_visible':shelf.isVisible(),
            'shelf_kind':shelf.presentation_kind,'shelf_state':shelf.visible_state,'shelf_shows':shows.count,
            'destination_prepared':manager.edge_file_drag.prepared,'field':manager.edge_absorb.state}
    finally:
        if held:
            point=QCursor.pos()
            release((point.x(),point.y()))
            sample(.15)
        shelf.removeEventFilter(shows)
        manager.app.removeEventFilter(native)
        report['native_drag_tail']=native.events
        report_path.write_text(json.dumps(report,indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report,indent=2), flush=True)
    return 0 if report['ok'] else 1
