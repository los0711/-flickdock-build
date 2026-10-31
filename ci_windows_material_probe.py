"""Independent Windows CI pixel gate; checkerboard is a dummy app window.

Screen captures are measurements only and are never used as material textures.
No application installation, global settings changes or unreviewed DLL search.
"""
import argparse,ctypes,hashlib,json,math,os,platform,sys,time
from pathlib import Path


def average(rows):return [sum(row[index] for row in rows)/len(rows) for index in range(3)]
def distance(first,second):return math.sqrt(sum((a-b)**2 for a,b in zip(first,second)))
def spread(rows):
    mean=average(rows);return math.sqrt(sum(sum((row[i]-mean[i])**2 for i in range(3)) for row in rows)/len(rows))
def difference(first,second):return sum(sum(abs(a-b) for a,b in zip(x,y)) for x,y in zip(first,second))/(len(first)*3)
def frequency_response(base0,base1,enabled0,enabled1):
    high=spread(enabled0)/max(.001,spread(base0))
    low=distance(average(enabled0),average(enabled1))/max(.001,distance(average(base0),average(base1)))
    ratio=high/max(.001,low)
    return dict(high_frequency_gain=high,low_frequency_response=low,normalized_blur_ratio=ratio,frequency_verified=bool(low>.03 and ratio<.35))


def architecture_evidence(kernel):
    """Raw process/OS facts; these do not relax any native pixel gate."""
    evidence=dict(python_pointer_bits=ctypes.sizeof(ctypes.c_void_p)*8,platform_machine=platform.machine(),
                  environment={name:os.environ.get(name,'') for name in ('RUNNER_ARCH','PROCESSOR_ARCHITECTURE','PROCESSOR_ARCHITEW6432','ImageOS','ImageVersion')})
    try:
        kernel.GetCurrentProcess.argtypes=[];kernel.GetCurrentProcess.restype=ctypes.c_void_p
        query=kernel.IsWow64Process2
        query.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ushort),ctypes.POINTER(ctypes.c_ushort)];query.restype=ctypes.c_int
        process_machine=ctypes.c_ushort();native_machine=ctypes.c_ushort()
        ok=bool(query(kernel.GetCurrentProcess(),ctypes.byref(process_machine),ctypes.byref(native_machine)))
        evidence['is_wow64_process2']=dict(ok=ok,process_machine=process_machine.value,native_machine=native_machine.value)
        if not ok:evidence['is_wow64_process2']['error']=ctypes.get_last_error()
    except Exception as error:evidence['is_wow64_process2']=dict(ok=False,error=type(error).__name__+': '+str(error))
    return evidence


def order_dummy_windows(user,checker_hwnd,foreground_hwnd):
    """Only reorder our two dummy HWNDs, without activation or geometry edits."""
    before=int(user.GetForegroundWindow() or 0)
    flags=0x0001|0x0002|0x0010|0x0200  # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER
    results=[bool(user.SetWindowPos(hwnd,-1,0,0,0,0,flags)) for hwnd in (checker_hwnd,foreground_hwnd)]
    after=int(user.GetForegroundWindow() or 0)
    return dict(checker_topmost_ok=results[0],foreground_topmost_ok=results[1],foreground_before=before,foreground_after=after,foreground_unchanged=after==before,flags=flags)


def dummy_source_evidence(read_pixel,phase):
    """Recognize the unmodified dummy baseline, never a blur output."""
    palette=(((15,65,100),(240,180,70)),((80,10,130),(180,255,255)))[phase]
    # Cell centers avoid antialiasing/DPR edges; include mask interior and gutter.
    points=((213,135),(219,135),(213,141),(219,141),(357,99),(363,99),(357,105),(363,105),(33,303),(39,303),(33,309),(39,309))
    observed=[dict(point=[x,y],expected=palette[(x//6+y//6)%2],actual=tuple(read_pixel(x,y))) for x,y in points]
    opaque=[tuple(read_pixel(x,y)) for y in range(90,112) for x in range(100,135)]
    icon=[tuple(read_pixel(x,y)) for y in range(90,114) for x in range(145,169)]
    icon_expected=[(0,0,0) if ((145+((x-145)//2)*2)//2+y//2)%2 else (255,255,255) for y in range(90,114) for x in range(145,169)]
    known=all(row['actual']==row['expected'] for row in observed);red=all(value==(255,0,0) for value in opaque);black_white=icon==icon_expected
    return dict(ready=known and red and black_white,known_source_swatches=observed,known_source_matches=known,opaque_red_matches=red,opaque_icon_matches=black_white,
                opaque_red_first=opaque[0],opaque_icon_colors=sorted(set(icon)))


def verify(build_json,output):
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('Restricted to the approved disposable Windows CI runner')
    os.environ['QT_QPA_PLATFORM']='windows'
    from PySide6.QtCore import Qt,QRect,QPoint
    from PySide6.QtGui import QColor,QPainter,QImage,QPen
    from PySide6.QtWidgets import QApplication,QWidget
    from PySide6.QtTest import QTest
    from window_material_win import NativeProbeLibrary
    build_path=Path(build_json).resolve(strict=True);build=json.loads(build_path.read_text(encoding='utf-8'))
    if not build.get('ok') or build.get('source_sha256')!=hashlib.sha256(Path(__file__).with_name('native_material_probe.cpp').read_bytes()).hexdigest():raise RuntimeError('Build evidence does not bind the reviewed native source')
    for name,digest in build.get('source_hashes',{}).items():
        if name!=Path(name).name or hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Build source fingerprint differs from the executing probe: '+name)
    driver=NativeProbeLibrary(build_path.parent/build['dll'],build['dll_sha256'])
    out=Path(output).resolve();out.parent.mkdir(parents=True,exist_ok=True)
    report=dict(verified=False,backend='Clipped Windows.UI.Composition and independent DWM Acrylic candidates',dll_sha256=driver.sha256,source_sha256=build['source_sha256'],source_hashes=build['source_hashes'],github_sha=os.environ.get('GITHUB_SHA',''),windows_build=sys.getwindowsversion().build,windows_product_type=sys.getwindowsversion().product_type,modes=[])
    from ci_windows_desktop_fixture import read_desktop_evidence
    report['desktop_context_before_qt']=read_desktop_evidence()
    user=ctypes.WinDLL('user32',use_last_error=True);dwm=ctypes.WinDLL('dwmapi');gdi=ctypes.WinDLL('gdi32');kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    report['architecture']=architecture_evidence(kernel)
    from ctypes.wintypes import RECT
    user.GetWindowLongPtrW.argtypes=[ctypes.c_void_p,ctypes.c_int];user.GetWindowLongPtrW.restype=ctypes.c_ssize_t
    user.SetWindowPos.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_uint];user.SetWindowPos.restype=ctypes.c_int
    user.GetForegroundWindow.argtypes=[];user.GetForegroundWindow.restype=ctypes.c_void_p
    user.GetSystemMetrics.argtypes=[ctypes.c_int];user.GetSystemMetrics.restype=ctypes.c_int
    user.GetWindowRgn.argtypes=[ctypes.c_void_p,ctypes.c_void_p];user.GetWindowRgn.restype=ctypes.c_int
    user.GetGuiResources.argtypes=[ctypes.c_void_p,ctypes.c_uint];user.GetGuiResources.restype=ctypes.c_uint
    gdi.CreateRectRgn.argtypes=[ctypes.c_int]*4;gdi.CreateRectRgn.restype=ctypes.c_void_p
    gdi.GetRgnBox.argtypes=[ctypes.c_void_p,ctypes.POINTER(RECT)];gdi.GetRgnBox.restype=ctypes.c_int
    gdi.DeleteObject.argtypes=[ctypes.c_void_p];gdi.DeleteObject.restype=ctypes.c_int
    kernel.GetCurrentProcess.argtypes=[];kernel.GetCurrentProcess.restype=ctypes.c_void_p
    dwm.DwmSetWindowAttribute.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint];dwm.DwmSetWindowAttribute.restype=ctypes.c_long
    dwm.DwmFlush.argtypes=[];dwm.DwmFlush.restype=ctypes.c_long
    dwm.DwmIsCompositionEnabled.argtypes=[ctypes.POINTER(ctypes.c_int)];dwm.DwmIsCompositionEnabled.restype=ctypes.c_long
    composition_enabled=ctypes.c_int();composition_hresult=dwm.DwmIsCompositionEnabled(ctypes.byref(composition_enabled))
    report.update(remote_session=bool(user.GetSystemMetrics(0x1000)),dwm_composition_enabled=bool(composition_enabled.value),dwm_composition_hresult=composition_hresult)
    app=QApplication.instance() or QApplication(['FlickDock isolated material evidence']);app.setQuitOnLastWindowClosed(False)
    material_rects=[(80,70,260,160,30,1),(410,250,130,100,22,1)]
    class Checker(QWidget):
        phase=0
        def paintEvent(self,event):
            painter=QPainter(self)
            palette=((QColor(15,65,100),QColor(240,180,70)),(QColor(80,10,130),QColor(180,255,255)))[self.phase]
            for y in range(0,self.height(),6):
                for x in range(0,self.width(),6):painter.fillRect(x,y,6,6,palette[(x//6+y//6)%2])
    class Foreground(QWidget):
        tint=0.
        def paintEvent(self,event):
            painter=QPainter(self);painter.setCompositionMode(QPainter.CompositionMode_Source);painter.fillRect(self.rect(),Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver);painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255,255,255,round(self.tint*255)))
            for x,y,w,h,r,_ in material_rects:painter.drawRoundedRect(QRect(x,y,w,h),r,r)
            painter.fillRect(100,90,35,22,QColor(255,0,0))
            for y in range(90,114,2):
                for x in range(145,169,2):painter.fillRect(x,y,2,2,QColor('black' if (x//2+y//2)%2 else 'white'))
            painter.setPen(QPen(QColor('black'),1));painter.drawText(180,105,'OPAQUE FOREGROUND')
    checker=Checker();foreground=Foreground()
    for widget in (checker,foreground):
        widget.setWindowFlags(Qt.Window|Qt.FramelessWindowHint|Qt.WindowDoesNotAcceptFocus|Qt.WindowStaysOnTopHint)
        widget.setAttribute(Qt.WA_ShowWithoutActivating);widget.resize(620,400)
    foreground.setAttribute(Qt.WA_TranslucentBackground)
    area=app.primaryScreen().availableGeometry();position=QPoint(area.left()+30,area.top()+30)
    for widget in (checker,foreground):widget.move(position)
    focus_before_show=int(user.GetForegroundWindow() or 0)
    checker.show();foreground.show();app.processEvents()
    report['dummy_initial_order']=order_dummy_windows(user,int(checker.winId()),int(foreground.winId()))
    QTest.qWait(150)
    report['dummy_initial_order'].update(foreground_before_show=focus_before_show,show_foreground_unchanged=int(user.GetForegroundWindow() or 0)==focus_before_show)
    hwnd=int(foreground.winId());original_geometry=foreground.geometry().getRect();original_owner=int(user.GetWindowLongPtrW(hwnd,-8));ex_style=int(user.GetWindowLongPtrW(hwnd,-20))
    report['desktop_context_own_windows']=read_desktop_evidence((int(checker.winId()),hwnd))
    def host_region():
        region=gdi.CreateRectRgn(0,0,0,0)
        if not region:raise OSError('Cannot allocate the temporary host-region query')
        try:
            kind=user.GetWindowRgn(hwnd,region);bounds=RECT()
            if kind:gdi.GetRgnBox(region,ctypes.byref(bounds))
            return dict(kind=kind,bounds=[bounds.left,bounds.top,bounds.right,bounds.bottom] if kind else None)
        finally:gdi.DeleteObject(region)
    original_region=host_region()
    report.update(host_ex_style=ex_style,host_layered=bool(ex_style&0x80000),host_geometry=original_geometry,host_owner=original_owner)
    def capture(label,phase,tint):
        checker.phase=phase;checker.update();foreground.tint=tint;foreground.update();app.processEvents();dwm.DwmFlush();QTest.qWait(180);app.processEvents()
        screen=foreground.screen();pixmap=screen.grabWindow(0,foreground.x(),foreground.y(),foreground.width(),foreground.height())
        if pixmap.isNull():raise RuntimeError('Composed desktop capture is unavailable')
        pixmap.save(str(out.parent/(label+'.png')));return pixmap.toImage()
    def samples(image,rect,step=2):
        ratio=image.devicePixelRatio();x,y,w,h=rect
        return [image.pixelColor(round(xx*ratio),round(yy*ratio)).getRgb()[:3] for yy in range(y,y+h,step) for xx in range(x,x+w,step)]
    inside=(210,130,80,65);outside=(355,95,40,80);corner=(81,71,8,8);opaque=(100,90,35,22);icon=(145,90,24,24)
    def analyze(base0,base1,enabled0,enabled1):
        b0,b1,e0,e1=[samples(image,inside) for image in (base0,base1,enabled0,enabled1)]
        response=frequency_response(b0,b1,e0,e1)
        foreground_equal=samples(base0,opaque,1)==samples(enabled0,opaque,1) and samples(base0,icon,1)==samples(enabled0,icon,1)
        outer=difference(samples(base0,outside),samples(enabled0,outside));round_corner=difference(samples(base0,corner,1),samples(enabled0,corner,1))
        return dict(**response,baseline_mean=average(b0),material_mean=average(e0),changed_background_material_mean=average(e1),foreground_pixels_identical=foreground_equal,outside_mean_abs_diff=outer,rounded_corner_mean_abs_diff=round_corner,
                    verified=bool(response['frequency_verified'] and foreground_equal and outer<2 and round_corner<2))
    try:
        report['policy']=driver.policy()
        if report['windows_build']<22000:raise RuntimeError('Public host-backdrop support requires Windows 11 build 22000 or later')
        if not report['policy']['advanced_effects'] or report['policy']['high_contrast']:raise RuntimeError('policy-disabled: advanced effects are disabled or High Contrast is enabled; no system settings were changed')
        if not report['host_layered']:raise AssertionError('Probe did not create an actual Qt layered foreground HWND')
        for mode,name in enumerate(('same-hwnd','child-hwnd','underlay-hwnd','system-acrylic-underlay')):
            evidence=dict(mode=name,verified=False,mask='rounded HRGN union only on an independent background HWND; binary coverage, no fractional alpha' if mode==3 else 'Composition rounded geometric clip')
            try:
                foreground.tint=0.;foreground.update()
                # Fresh dummy HWND only. Establish the known initial value for
                # direct/child experiments; never do this to a product window.
                if mode!=3:
                    disabled=ctypes.c_int(0);code=dwm.DwmSetWindowAttribute(hwnd,17,ctypes.byref(disabled),ctypes.sizeof(disabled))
                    if code<0:raise OSError(f'Host-backdrop flag unsupported: 0x{code&0xffffffff:08x}')
                elif report['windows_build']<22621:raise RuntimeError('Public system Acrylic requires Windows 11 build 22621 or later')
                evidence['baseline_readiness']=[]
                for attempt in range(3):
                    ordering=order_dummy_windows(user,int(checker.winId()),hwnd)
                    suffix='' if attempt==0 else '-retry'+str(attempt)
                    base0=capture(name+'-baseline-0'+suffix,0,0);base1=capture(name+'-baseline-1'+suffix,1,0)
                    def source_state(image,phase):
                        ratio=image.devicePixelRatio()
                        return dummy_source_evidence(lambda x,y:image.pixelColor(round(x*ratio),round(y*ratio)).getRgb()[:3],phase)
                    readiness=dict(attempt=attempt+1,ordering=ordering,phase0=source_state(base0,0),phase1=source_state(base1,1))
                    readiness['ready']=bool(ordering['checker_topmost_ok'] and ordering['foreground_topmost_ok'] and ordering['foreground_unchanged'] and readiness['phase0']['ready'] and readiness['phase1']['ready'])
                    evidence['baseline_readiness'].append(readiness)
                    if readiness['ready']:break
                if not evidence['baseline_readiness'][-1]['ready']:
                    evidence['measurement_stage']='not-exercised-baseline-obstructed'
                    raise AssertionError('Known dummy baseline is obstructed or unavailable; native material was not exercised')
                evidence['measurement_stage']='baseline-ready'
                focus_before=int(user.GetForegroundWindow() or 0);gdi_before=int(user.GetGuiResources(kernel.GetCurrentProcess(),0))
                driver.attach(hwnd,mode);ratio=foreground.devicePixelRatioF();regions=[tuple(value*ratio for value in row[:5])+(row[5],) for row in material_rects]
                driver.regions(regions);driver.visible(True)
                evidence['measurement_stage']='native-material-exercised'
                enabled0=capture(name+'-native-0',0,0);enabled1=capture(name+'-native-1',1,0)
                evidence['clear_tint']=analyze(base0,base1,enabled0,enabled1)
                driver.visible(False);tint0=capture(name+'-tint90-baseline-0',0,.9);tint1=capture(name+'-tint90-baseline-1',1,.9)
                driver.visible(True);native0=capture(name+'-tint90-native-0',0,.9);native1=capture(name+'-tint90-native-1',1,.9)
                evidence['tint90']=analyze(tint0,tint1,native0,native1)
                driver.regions([]);empty=capture(name+'-empty',0,0)
                evidence['empty_mask_diff']=difference(samples(base0,(20,20,570,350),6),samples(empty,(20,20,570,350),6))
                evidence['native_focus_unchanged']=int(user.GetForegroundWindow() or 0)==focus_before
                evidence['host_geometry_unchanged']=foreground.geometry().getRect()==original_geometry
                evidence['host_owner_unchanged']=int(user.GetWindowLongPtrW(hwnd,-8))==original_owner
                evidence['host_region_before']=original_region;evidence['host_region_after']=host_region()
                evidence['host_region_unchanged']=evidence['host_region_after']==original_region
                driver.visible(False);driver.close();app.processEvents();dwm.DwmFlush();QTest.qWait(50)
                evidence['resources_after_close']=driver.stats()
                evidence['gdi_objects_before']=gdi_before;evidence['gdi_objects_after_close']=int(user.GetGuiResources(kernel.GetCurrentProcess(),0))
                evidence['verified']=bool(evidence['clear_tint']['verified'] and evidence['tint90']['verified'] and evidence['empty_mask_diff']<2 and evidence['native_focus_unchanged'] and report['dummy_initial_order']['show_foreground_unchanged'] and evidence['host_geometry_unchanged'] and evidence['host_owner_unchanged'] and evidence['host_region_unchanged'] and evidence['gdi_objects_after_close']-gdi_before<=8 and all(evidence['resources_after_close'][key]==0 for key in ('contexts','windows','visuals')))
            except Exception as error:evidence['error']=type(error).__name__+': '+str(error)
            finally:
                try:driver.close()
                except Exception as error:evidence['close_error']=str(error)
                report['modes'].append(evidence)
        report['verified']=any(mode['verified'] for mode in report['modes'])
        report['reason']='pixel-verified-candidate-only' if report['verified'] else ('baseline-obstructed-native-material-not-exercised' if all(mode.get('measurement_stage')=='not-exercised-baseline-obstructed' for mode in report['modes']) else 'no-compatible-pixel-verified-candidate')
    except Exception as error:report['reason']=type(error).__name__+': '+str(error)
    finally:
        try:driver.shutdown();report['resources_after_shutdown']=driver.stats()
        except Exception as error:report['shutdown_error']=str(error);report['verified']=False
        foreground.hide();checker.hide();foreground.deleteLater();checker.deleteLater();app.processEvents()
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--build-json',required=True);parser.add_argument('--output',required=True)
    arguments=parser.parse_args();result=verify(arguments.build_json,arguments.output);print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['verified'] else 2)
