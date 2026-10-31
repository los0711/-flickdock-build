"""Native material pixel gate, restricted to disposable GitHub Actions hosts.

No clipboard, screenshot of a user's desktop, account, network or global
setting is read or changed. Two local dummy windows provide known pixels.
The captured CI window regions are evidence, not a replacement blur texture.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def _known_source_readiness(phase, samples):
    """Only known *outside* source + opaque swatches; never a blur metric."""
    dark,bright = [30.,55.,85.],[210.,180.,145.]
    source = ({'outside_left':dark,'outside_right':bright} if phase == 1 else
              {'outside_left':bright,'outside_right':dark} if phase == 2 else None)
    if source is None: raise ValueError('Unknown low-frequency source phase')
    expected = dict(source,foreground_red=[255.,0.,0.],foreground_blue=[0.,51.,255.],
                    foreground_white=[255.,255.,255.],foreground_black=[0.,0.,0.])
    errors = {key:max(abs(float(actual)-target) for actual,target in zip(samples[key],value))
              for key,value in expected.items()}
    return dict(ready=all(value<=2. for value in errors.values()),phase=phase,
                expected=expected,actual={key:samples[key] for key in expected},max_channel_error=errors)


def _await_known_source(phase, capture, inspect, wait, record, clock=None):
    """Bounded source-readiness gate; no retries based on material quality."""
    clock = clock or time.monotonic
    start = clock();deadline = start+.9
    for attempt in range(7):
        remaining = deadline-clock()
        if remaining<=0:break
        wait(min(.08 if attempt==0 else .12,remaining))
        if clock()>=deadline:break
        image = capture(attempt)
        state = _known_source_readiness(phase,inspect(image))
        elapsed = clock()-start
        state.update(attempt=attempt+1,elapsed_ms=round(elapsed*1000,3),within_budget=elapsed<=.9)
        record(state)
        if state['ready'] and state['within_budget']:return image
        if elapsed>=.9:break
    raise AssertionError('Known dummy source/opaque foreground not ready within 900ms/7 captures; no blur metric accepted')


def _native_bool_readback(workspace, selector, objc):
    """Independent scalar BOOL read, never modifies an accessibility setting."""
    import ctypes
    signature = workspace.methodSignatureForSelector_(selector)
    encoding = bytes(signature.methodReturnType()).decode('ascii')
    if encoding not in ('B','c','C'):
        raise RuntimeError('Unexpected Objective-C BOOL return encoding')
    library = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    register = library.sel_registerName
    register.argtypes = [ctypes.c_char_p]; register.restype = ctypes.c_void_p
    send = library.objc_msgSend
    send.argtypes = [ctypes.c_void_p,ctypes.c_void_p]
    send.restype = ctypes.c_byte if encoding == 'c' else ctypes.c_ubyte
    value = int(send(ctypes.c_void_p(int(objc.pyobjc_id(workspace))),
                     register(selector.encode('ascii'))))
    return dict(return_encoding=encoding,native_return=value,native_bool=bool(value))


def _policy_report(appkit, foundation, objc, native_reader=None):
    """Read-only raw/type/ABI evidence; no defaults writes or forced policy."""
    reader = native_reader or _native_bool_readback
    workspace = appkit.NSWorkspace.sharedWorkspace()
    result = dict(os_version=str(foundation.NSProcessInfo.processInfo().operatingSystemVersionString()),
                  pyobjc_version=str(getattr(objc,'__version__','unknown')))
    for key,selector in (('reduce_transparency','accessibilityDisplayShouldReduceTransparency'),
                         ('increase_contrast','accessibilityDisplayShouldIncreaseContrast')):
        item = dict(selector=selector)
        try:
            raw = getattr(workspace,selector)()
            item.update(raw_repr=repr(raw)[:120],raw_type=type(raw).__module__+'.'+type(raw).__name__,
                        raw_bool=bool(raw),scalar_bool=isinstance(raw,(bool,int)))
            item.update(reader(workspace,selector,objc))
            item['bridge_matches_native'] = bool(item['scalar_bool'] and item['raw_bool']==item['native_bool'])
        except Exception as error:
            item.update(error_type=type(error).__name__,bridge_matches_native=False)
        result[key] = item
    return result


def verify_mac(output):
    if sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Native pixel proof is restricted to a disposable macOS CI runner')
    os.environ['QT_QPA_PLATFORM'] = 'cocoa'
    import AppKit
    import Foundation
    import Quartz
    import objc
    import ctypes
    from PySide6.QtCore import QRect, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtTest import QTest
    from window_material import install_material, MaterialRegion

    app = QApplication.instance() or QApplication(['FlickDock isolated native material proof'])
    app.setQuitOnLastWindowClosed(False)
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    result = dict(ok=False, proof='real Cocoa material + known local checkerboard + compositor pixels',
                  qt_platform=app.platformName(), screenshots=[], native_user_desktop=False,
                  fallback_verified=False,blur_verified=False,source_readiness={})
    handle = background = foreground = None
    class Checkerboard(QWidget):
        signal_phase = 0
        def paintEvent(self, event):
            p = QPainter(self)
            for y in range(0, self.height(), 4):
                for x in range(0, self.width(), 4):
                    parity = (x//4+y//4)%2
                    if not self.signal_phase:
                        color = QColor('#F0F0F0' if parity else '#101010')
                    else:
                        bright = (x >= self.width()/2) == (self.signal_phase == 1)
                        values = (210,180,145) if bright else (30,55,85)
                        adjustment = 8 if parity else -8
                        color = QColor(*(v+adjustment for v in values))
                    p.fillRect(x, y, 4, 4, color)
            p.end()
    class Foreground(QWidget):
        paint_count = 0
        fallback_color = None
        def paintEvent(self, event):
            self.paint_count += 1
            p = QPainter(self)
            if self.fallback_color is not None:
                from PySide6.QtGui import QPainterPath
                path=QPainterPath();path.addRoundedRect(QRectF(90,70,300,200),24,24)
                p.fillPath(path,self.fallback_color)
            p.fillRect(115, 105, 230, 42, Qt.white)
            p.setPen(Qt.black); p.setFont(QFont('Helvetica', 18))
            p.drawText(QRect(120, 110, 220, 36), Qt.AlignLeft | Qt.AlignVCenter, 'FlickDock TEXT 123')
            # These opaque original-content pixels must survive native blur.
            p.fillRect(130, 160, 50, 16, QColor('#FF0000'))
            p.fillRect(200, 160, 50, 16, QColor('#0033FF'))
            p.fillRect(130, 190, 120, 3, Qt.black)
            p.end()
    def capture(label):
        native = objc.objc_object(c_void_p=ctypes.c_void_p(int(foreground.winId())))
        number = int(native.window().windowNumber())
        infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, number) or []
        bounds = next(item[Quartz.kCGWindowBounds] for item in infos
                      if int(item[Quartz.kCGWindowNumber]) == number)
        rect = Quartz.CGRectMake(bounds['X'], bounds['Y'], bounds['Width'], bounds['Height'])
        image = Quartz.CGWindowListCreateImage(rect, Quartz.kCGWindowListOptionOnScreenOnly,
                                              Quartz.kCGNullWindowID, Quartz.kCGWindowImageBoundsIgnoreFraming)
        if image is None: raise AssertionError('CI compositor capture unavailable; cannot claim native blur')
        rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
        qt_image = QImage.fromData(bytes(data), 'PNG')
        if qt_image.isNull(): raise AssertionError('Invalid compositor capture')
        path = output.with_name(output.stem+'-'+label+'.png')
        if not qt_image.save(str(path)): raise AssertionError('Could not save compositor proof')
        result['screenshots'].append(path.name)
        return qt_image
    def roi_values(image, rect):
        sx, sy = image.width()/foreground.width(), image.height()/foreground.height()
        values = []
        for y in range(round(rect.top()*sy), round((rect.bottom()+1)*sy)):
            for x in range(round(rect.left()*sx), round((rect.right()+1)*sx)):
                c = image.pixelColor(x, y); values.append((c.red(), c.green(), c.blue()))
        return values
    def energy(values):
        gray = [(r+g+b)/3 for r,g,b in values]
        mean = sum(gray)/max(1, len(gray))
        return sum((v-mean)**2 for v in gray)/max(1, len(gray))
    def difference(a, b):
        if len(a) != len(b): raise AssertionError('Capture geometry changed')
        return sum(abs(x-y) for ca,cb in zip(a,b) for x,y in zip(ca,cb))/(3*max(1,len(a)))
    def mean_color(values):
        return [sum(c[i] for c in values)/max(1,len(values)) for i in range(3)]
    def luminance(mean): return sum(mean)/3
    def capture_ready_source(label,phase):
        rois={'outside_left':QRect(25,215,40,28),'outside_right':QRect(430,215,40,28),
              'foreground_red':QRect(135,163,35,8),'foreground_blue':QRect(205,163,35,8),
              'foreground_white':QRect(120,108,20,2),'foreground_black':QRect(135,190,100,2)}
        attempts=[];result['source_readiness'][label]=attempts
        accepted=_await_known_source(phase,
            lambda attempt:capture(label+'-readiness-'+str(attempt+1)),
            lambda image:{key:mean_color(roi_values(image,roi)) for key,roi in rois.items()},
            lambda seconds:QTest.qWait(max(1,round(seconds*1000))),attempts.append)
        path=output.with_name(output.stem+'-'+label+'.png')
        if not accepted.save(str(path)):raise AssertionError('Could not save accepted known-source frame')
        result['screenshots'].append(path.name)
        return accepted
    try:
        result['policy'] = _policy_report(AppKit,Foundation,objc)
        if not result['policy']['reduce_transparency']['bridge_matches_native']:
            raise AssertionError('Reduce Transparency raw/native BOOL mismatch or unavailable; policy cannot be trusted')
        available = app.primaryScreen().availableGeometry()
        x, y = available.center().x()-260, available.center().y()-180
        flags = Qt.Window | Qt.FramelessWindowHint
        background = Checkerboard(); background.setWindowFlags(flags)
        background.setGeometry(x, y, 520, 360); background.show()
        foreground = Foreground(); foreground.setWindowFlags(flags)
        foreground.setAttribute(Qt.WA_TranslucentBackground)
        foreground.setGeometry(x+10, y+10, 500, 340); foreground.show(); foreground.raise_()
        QTest.qWait(350); app.processEvents()
        native = objc.objc_object(c_void_p=ctypes.c_void_p(int(foreground.winId())))
        window = native.window()
        before = dict(content=int(objc.pyobjc_id(window.contentView())), delegate=int(objc.pyobjc_id(window.delegate())),
                      level=int(window.level()), geometry=foreground.geometry().getRect(), opacity=foreground.windowOpacity())
        bare = capture('before')
        handle = install_material(foreground)
        handle.set_theme('light')
        card = QRectF(90, 70, 300, 200)
        handle.set_regions([MaterialRegion.rounded('actual-card', card, 24)], revision=1)
        QTest.qWait(650); app.processEvents()
        result['material'] = handle.status()
        if handle.status()['attached'] and handle.capabilities().reason == 'policy-disabled':
            if not result['policy']['reduce_transparency']['native_bool']:
                raise AssertionError('Backend disabled material despite native Reduce Transparency=false')
            effect=handle.backend.view
            if not bool(effect.isHidden()): raise AssertionError('Reduced-transparency native material remained visible')
            fallback=handle.background_color('#FFFFFF')
            if fallback.alpha()!=255: raise AssertionError('Reduced-transparency fallback is not opaque')
            foreground.fallback_color=fallback;foreground.update();QTest.qWait(180);app.processEvents()
            fallback_image=capture('policy-fallback')
            samples=roi_values(fallback_image,QRect(275,210,90,35))
            if min(min(c) for c in samples)<250 or energy(samples)>2:
                raise AssertionError('Opaque fallback did not cover the actual card')
            outside_delta=difference(roi_values(bare,QRect(25,285,55,25)),roi_values(fallback_image,QRect(25,285,55,25)))
            corner_delta=difference(roi_values(bare,QRect(91,71,3,3)),roi_values(fallback_image,QRect(91,71,3,3)))
            content_rois=(QRect(135,163,35,8),QRect(205,163,35,8),QRect(135,190,100,2),QRect(120,110,220,36))
            foreground_deltas=[difference(roi_values(bare,roi),roi_values(fallback_image,roi)) for roi in content_rois]
            if max(outside_delta,corner_delta,*foreground_deltas)>2:
                raise AssertionError(('Policy fallback changed foreground or leaked into empty host',outside_delta,corner_delta,foreground_deltas))
            after=dict(content=int(objc.pyobjc_id(window.contentView())),delegate=int(objc.pyobjc_id(window.delegate())),
                       level=int(window.level()),geometry=foreground.geometry().getRect(),opacity=foreground.windowOpacity())
            if before!=after: raise AssertionError('Policy fallback changed native foreground/host identity')
            handle.close();foreground.fallback_color=None;foreground.update();QTest.qWait(180);app.processEvents()
            detached=capture('policy-detached')
            restored=difference(roi_values(bare,QRect(275,210,90,35)),roi_values(detached,QRect(275,210,90,35)))
            if effect.superview() is not None or effect.maskImage() is not None or handle.backend._observer is not None:
                raise AssertionError('Reduced-transparency fallback did not release native view/mask/observer')
            if restored>2: raise AssertionError(('Policy fallback native pixels remained after detach',restored))
            result.update(fallback_verified=True,blur_verified=False,outcome='policy-disabled',owner_unchanged=True,
                          fallback_metrics=dict(background_alpha=fallback.alpha(),inside_variance=energy(samples),
                            outside_delta=outside_delta,corner_delta=corner_delta,foreground_deltas=foreground_deltas,
                            detached_delta=restored,view_hidden=True,view_detached=True,observer_removed=True))
            raise AssertionError('OS Reduce Transparency=true: opaque fallback verified; native blur remains unverified')
        if not handle.status()['attached'] or not handle.capabilities().supported:
            raise AssertionError(('Native material did not attach', handle.status()))
        effect = handle.backend.view
        if effect.superview() != native.superview(): raise AssertionError('Native backing not in the real Qt sibling hierarchy')
        views = list(native.superview().subviews())
        if views.index(effect) >= views.index(native): raise AssertionError('Material is above Qt foreground')
        if effect.hitTest_((10,10)) is not None: raise AssertionError('Material intercepts input')
        blurred = capture('blurred')
        # Source pixels are known high-frequency local checkerboard, not a
        # wallpaper guess. Blur must reduce its variance inside actual card.
        inside = QRect(275, 210, 90, 35)
        outside = QRect(25, 285, 55, 25)
        e_before, e_after = energy(roi_values(bare, inside)), energy(roi_values(blurred, inside))
        outside_delta = difference(roi_values(bare, outside), roi_values(blurred, outside))
        if e_before < 500: raise AssertionError(('Known checkerboard did not reach the screenshot', e_before))
        if e_after >= e_before*.60: raise AssertionError(('No measured native background blur', e_before, e_after))
        if outside_delta > 2: raise AssertionError(('Material leaked outside current card into the large host', outside_delta))
        # Rounded corner (92,72) is inside bounding rect, outside rounded path.
        corner_delta = difference(roi_values(bare,QRect(91,71,3,3)), roi_values(blurred,QRect(91,71,3,3)))
        if corner_delta > 2: raise AssertionError(('Material leaked into transparent rounded corner', corner_delta))
        red_delta = difference(roi_values(bare,QRect(135,163,35,8)), roi_values(blurred,QRect(135,163,35,8)))
        blue_delta = difference(roi_values(bare,QRect(205,163,35,8)), roi_values(blurred,QRect(205,163,35,8)))
        black_delta = difference(roi_values(bare,QRect(135,190,100,2)), roi_values(blurred,QRect(135,190,100,2)))
        if max(red_delta,blue_delta,black_delta) > 2:
            raise AssertionError(('Native material changed opaque foreground bytes/pixels', red_delta,blue_delta,black_delta))
        text_before, text_after = roi_values(bare,QRect(120,110,220,36)), roi_values(blurred,QRect(120,110,220,36))
        text_black = [i for i,c in enumerate(text_before) if max(c)<8]
        if not text_black: raise AssertionError('No crisp text pixels found')
        text_delta = difference(text_before,text_after)
        # Text is deliberately on a known opaque foreground swatch, so both
        # antialias edges and black glyph interiors must remain unchanged.
        crisp_after = sum(max(c)<8 for c in text_after)
        if crisp_after < 100 or text_delta > 2:
            raise AssertionError(('Foreground text changed after native blur', crisp_after,text_delta))
        # A gray opaque rectangle also suppresses checkerboard variance. Prove
        # real, live behind-window sampling with an independent low-frequency
        # signal: swap dark/bright color halves behind the *same* material.
        # Only the background window is repainted; do not rebuild the effect,
        # reset its mask or repaint the foreground to manufacture the result.
        paint_count = foreground.paint_count
        effect_identity = int(objc.pyobjc_id(effect))
        mask_identity = int(objc.pyobjc_id(effect.maskImage()))
        signal_rois = {'inside_left':QRect(110,215,40,28),'inside_right':QRect(330,215,40,28),
                       'outside_left':QRect(25,215,40,28),'outside_right':QRect(430,215,40,28)}
        signals=[]
        for phase in (1,2):
            background.signal_phase = phase; background.update()
            app.processEvents()
            signal_image = capture_ready_source('live-source-'+str(phase),phase)
            signals.append({name:mean_color(roi_values(signal_image,roi)) for name,roi in signal_rois.items()})
        result['live_source_signal'] = dict(means=signals, foreground_paints_before=paint_count,
            foreground_paints_after=foreground.paint_count, material_identity_unchanged=int(objc.pyobjc_id(handle.backend.view))==effect_identity,
            mask_identity_unchanged=int(objc.pyobjc_id(effect.maskImage()))==mask_identity)
        outside_left_delta = luminance(signals[1]['outside_left'])-luminance(signals[0]['outside_left'])
        outside_right_delta = luminance(signals[0]['outside_right'])-luminance(signals[1]['outside_right'])
        inside_left_delta = luminance(signals[1]['inside_left'])-luminance(signals[0]['inside_left'])
        inside_right_delta = luminance(signals[0]['inside_right'])-luminance(signals[1]['inside_right'])
        inside_contrasts = [luminance(signals[0]['inside_right'])-luminance(signals[0]['inside_left']),
                            luminance(signals[1]['inside_left'])-luminance(signals[1]['inside_right'])]
        result['live_source_signal']['deltas'] = dict(outside=[outside_left_delta,outside_right_delta],
            inside=[inside_left_delta,inside_right_delta],inside_spatial_contrasts=inside_contrasts)
        if min(outside_left_delta,outside_right_delta) < 70:
            raise AssertionError(('Known low-frequency source did not change',signals))
        if min(inside_left_delta,inside_right_delta,*inside_contrasts) < 3:
            raise AssertionError(('Opaque tint or unavailable blur: live source signal did not reach material',signals))
        if (foreground.paint_count != paint_count or int(objc.pyobjc_id(handle.backend.view)) != effect_identity
                or int(objc.pyobjc_id(effect.maskImage())) != mask_identity):
            raise AssertionError('Foreground/material was rebuilt while measuring live native background sampling')
        background.signal_phase = 0; background.update(); QTest.qWait(350); app.processEvents()
        # Simulate an in-host surface morph. Host remains fixed; only exact
        # native material clip/region changes, and old footprint becomes clear.
        handle.set_regions([MaterialRegion.rounded('actual-card',QRectF(160,90,160,120),18)],revision=2)
        QTest.qWait(200); app.processEvents(); small = capture('smaller-card')
        retired_delta = difference(roi_values(bare,QRect(95,220,55,25)),roi_values(small,QRect(95,220,55,25)))
        if retired_delta > 2: raise AssertionError(('Old material footprint remained after pose update',retired_delta))
        after = dict(content=int(objc.pyobjc_id(window.contentView())),delegate=int(objc.pyobjc_id(window.delegate())),
                     level=int(window.level()),geometry=foreground.geometry().getRect(),opacity=foreground.windowOpacity())
        if before != after: raise AssertionError(('Material changed native owner/host/opacity', before,after))
        handle.close(); QTest.qWait(180); app.processEvents(); detached = capture('detached')
        if effect.superview() is not None: raise AssertionError('Native material view was not detached')
        restored = difference(roi_values(bare,inside),roi_values(detached,inside))
        if restored > 2: raise AssertionError(('Native material remained after close',restored))
        result.update(ok=True,blur_verified=True, metrics=dict(checkerboard_variance_before=e_before,checkerboard_variance_blurred=e_after,
                      variance_ratio=e_after/e_before,outside_delta=outside_delta,corner_delta=corner_delta,
                      original_foreground_delta=[red_delta,blue_delta,black_delta],crisp_text_pixels=crisp_after,
                      text_delta=text_delta,retired_pose_delta=retired_delta,detached_delta=restored),owner_unchanged=True)
    except Exception as error:
        result['error'] = type(error).__name__+': '+str(error)
    finally:
        if handle is not None: handle.close()
        for widget in (foreground,background):
            if widget is not None: widget.hide(); widget.deleteLater()
        app.processEvents()
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if not result['ok']: raise AssertionError(result)
    return result


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: ci_material_probe.py OUTPUT.json')
    print(json.dumps(verify_mac(sys.argv[1]),ensure_ascii=False))
