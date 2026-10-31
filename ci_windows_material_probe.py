"""Independent Windows CI pixel gate; checkerboard is a dummy app window.

Screen captures are measurements only and are never used as material textures.
No application installation, global settings changes or unreviewed DLL search.
"""
import argparse,ctypes,hashlib,json,math,os,sys,time
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
    report=dict(verified=False,backend='Windows.UI.Composition HostBackdropBrush + rounded geometric clips',dll_sha256=driver.sha256,source_sha256=build['source_sha256'],source_hashes=build['source_hashes'],github_sha=os.environ.get('GITHUB_SHA',''),windows_build=sys.getwindowsversion().build,modes=[])
    user=ctypes.WinDLL('user32',use_last_error=True);dwm=ctypes.WinDLL('dwmapi')
    user.GetWindowLongPtrW.argtypes=[ctypes.c_void_p,ctypes.c_int];user.GetWindowLongPtrW.restype=ctypes.c_ssize_t
    user.GetForegroundWindow.argtypes=[];user.GetForegroundWindow.restype=ctypes.c_void_p
    dwm.DwmSetWindowAttribute.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint];dwm.DwmSetWindowAttribute.restype=ctypes.c_long
    dwm.DwmFlush.argtypes=[];dwm.DwmFlush.restype=ctypes.c_long
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
        widget.setWindowFlags(Qt.Window|Qt.FramelessWindowHint|Qt.WindowDoesNotAcceptFocus)
        widget.setAttribute(Qt.WA_ShowWithoutActivating);widget.resize(620,400)
    foreground.setAttribute(Qt.WA_TranslucentBackground)
    area=app.primaryScreen().availableGeometry();position=QPoint(area.left()+30,area.top()+30)
    for widget in (checker,foreground):widget.move(position)
    checker.show();foreground.show();checker.raise_();foreground.raise_();app.processEvents();QTest.qWait(150)
    hwnd=int(foreground.winId());original_geometry=foreground.geometry().getRect();original_owner=int(user.GetWindowLongPtrW(hwnd,-8));ex_style=int(user.GetWindowLongPtrW(hwnd,-20))
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
        return dict(**response,foreground_pixels_identical=foreground_equal,outside_mean_abs_diff=outer,rounded_corner_mean_abs_diff=round_corner,
                    verified=bool(response['frequency_verified'] and foreground_equal and outer<2 and round_corner<2))
    try:
        report['policy']=driver.policy()
        if report['windows_build']<22000:raise RuntimeError('Public host-backdrop support requires Windows 11 build 22000 or later')
        if not report['policy']['advanced_effects'] or report['policy']['high_contrast']:raise RuntimeError('policy-disabled: advanced effects are disabled or High Contrast is enabled; no system settings were changed')
        if not report['host_layered']:raise AssertionError('Probe did not create an actual Qt layered foreground HWND')
        for mode,name in enumerate(('same-hwnd','child-hwnd','underlay-hwnd')):
            evidence=dict(mode=name,verified=False)
            try:
                foreground.tint=0.;foreground.update()
                # Fresh dummy HWND only. Establish the known initial value for
                # direct/child experiments; never do this to a product window.
                disabled=ctypes.c_int(0);code=dwm.DwmSetWindowAttribute(hwnd,17,ctypes.byref(disabled),ctypes.sizeof(disabled))
                if code<0:raise OSError(f'Host-backdrop flag unsupported: 0x{code&0xffffffff:08x}')
                base0=capture(name+'-baseline-0',0,0);base1=capture(name+'-baseline-1',1,0)
                if len(set(samples(base0,icon,1)))<2 or samples(base0,opaque,1)[0]!=(255,0,0):raise AssertionError('Dummy foreground capture is obstructed or unavailable')
                focus_before=int(user.GetForegroundWindow() or 0)
                driver.attach(hwnd,mode);ratio=foreground.devicePixelRatioF();regions=[tuple(value*ratio for value in row[:5])+(row[5],) for row in material_rects]
                driver.regions(regions);driver.visible(True)
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
                driver.visible(False);driver.close();app.processEvents();dwm.DwmFlush();QTest.qWait(50)
                evidence['resources_after_close']=driver.stats()
                evidence['verified']=bool(evidence['clear_tint']['verified'] and evidence['tint90']['verified'] and evidence['empty_mask_diff']<2 and evidence['native_focus_unchanged'] and evidence['host_geometry_unchanged'] and evidence['host_owner_unchanged'] and all(evidence['resources_after_close'][key]==0 for key in ('contexts','windows','visuals')))
            except Exception as error:evidence['error']=type(error).__name__+': '+str(error)
            finally:
                try:driver.close()
                except Exception as error:evidence['close_error']=str(error)
                report['modes'].append(evidence)
        report['verified']=any(mode['verified'] for mode in report['modes'])
        report['reason']='pixel-verified-candidate-only' if report['verified'] else 'no-compatible-pixel-verified-candidate'
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
