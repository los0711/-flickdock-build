"""Isolated Qt/injected native contract: never accesses a real desktop."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from PySide6.QtCore import QEvent,QRectF,Qt
from PySide6.QtGui import QColor,QPainterPath
from PySide6.QtWidgets import QApplication,QWidget
from window_material import (MaterialCapabilities,MaterialHandle,MaterialRegion,
                             CocoaMaterialBackend,install_material,material_background)

APP=QApplication.instance() or QApplication(['material isolated tests'])


class RecordingBackend:
    def __init__(self):self.calls=[];self.attached=False
    def capabilities(self):return MaterialCapabilities('injected',True,True,'injected-unverified')
    def attach(self,owner,role):self.calls.append(('attach',role));self.attached=True;return True
    def set_policy_callback(self,callback):self.callback=callback
    def set_regions(self,regions,revision=0):self.regions=tuple(regions);self.calls.append(('regions',len(regions),revision))
    def set_visible(self,visible):self.visible=visible;self.calls.append(('visible',visible))
    def set_theme(self,theme):self.calls.append(('theme',theme))
    def close(self):self.calls.append(('close',));self.attached=False


class NativeView:
    @classmethod
    def alloc(cls):return cls()
    def initWithFrame_(self,frame):self.frame=frame;self.hidden=True;self.parent=None;return self
    def setHidden_(self,value):self.hidden=value
    def setAccessibilityElement_(self,value):self.ax=value
    def setBlendingMode_(self,value):self.blending=value
    def setMaterial_(self,value):self.material=value
    def setState_(self,value):self.state=value
    def setAppearance_(self,value):self.appearance=value
    def setFrame_(self,value):self.frame=value
    def setMaskImage_(self,value):self.mask=value
    def removeFromSuperview(self):
        if self.parent is not None:self.parent.children.remove(self)
        self.parent=None
    def superview(self):return self.parent


class NativePath:
    def __init__(self):self.ops=[]
    def setWindingRule_(self,rule):self.rule=rule
    def moveToPoint_(self,p):self.ops.append(('move',p))
    def lineToPoint_(self,p):self.ops.append(('line',p))
    def curveToPoint_controlPoint1_controlPoint2_(self,p,c1,c2):self.ops.append(('curve',p,c1,c2))
    def fill(self):pass


def cocoa_fixture():
    center=NS(observers=[],removed=[])
    def observe(name,obj,queue,block):
        token=NS(name=name,block=block);center.observers.append(token);return token
    center.addObserverForName_object_queue_usingBlock_=observe
    def remove(token):center.observers.remove(token);center.removed.append(token)
    center.removeObserver_=remove
    workspace=NS(reduced=False,accessibilityDisplayShouldReduceTransparency=lambda:workspace.reduced,notificationCenter=lambda:center)
    parent=NS(children=[])
    native=NS(window=lambda:NS(),isFlipped=lambda:True,
              bounds=lambda:NS(size=NS(height=300)),superview=lambda:parent,
              convertRect_toView_=lambda rect,destination:rect)
    parent.children=[native]
    def add(view,position,relative):
        parent.children.insert(parent.children.index(relative),view);view.parent=parent;view.position=position
    parent.addSubview_positioned_relativeTo_=add
    paths=[];images=[]
    def path():
        p=NativePath();paths.append(p);return p
    def image(size,flipped,draw):
        value=NS(size=size,flipped=flipped,draw=draw);images.append(value);return value
    context=NS(saveGraphicsState=lambda:None,restoreGraphicsState=lambda:None,setCompositingOperation_=lambda op:None)
    kit=NS(NSWorkspace=NS(sharedWorkspace=lambda:workspace),NSVisualEffectBlendingModeBehindWindow=0,
           NSVisualEffectMaterialPopover=1,NSVisualEffectStateActive=2,NSWindowBelow=-1,
           NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification='reduce-transparency',
           NSAppearanceNameDarkAqua='Dark',NSAppearanceNameAqua='Light',NSAppearance=NS(appearanceNamed_=lambda name:name),
           NSEvenOddWindingRule=0,NSBezierPath=NS(bezierPath=path),NSImage=NS(imageWithSize_flipped_drawingHandler_=image),
           NSGraphicsContext=NS(currentContext=lambda:context,saveGraphicsState=lambda:None,restoreGraphicsState=lambda:None),NSCompositingOperationCopy=1,
           NSColor=NS(colorWithCalibratedWhite_alpha_=lambda white,alpha:NS(setFill=lambda:None)))
    foundation=NS(NSOperationQueue=NS(mainQueue=lambda:'main'))
    backend=CocoaMaterialBackend(appkit=kit,foundation=foundation,objc=NS(),view_factory=NativeView,native_view=lambda owner:native)
    return NS(backend=backend,native=native,parent=parent,workspace=workspace,center=center,paths=paths,images=images)


class MaterialTests(unittest.TestCase):
    def setUp(self):self.windows=[]
    def tearDown(self):
        for window in self.windows:
            handle=getattr(window,'_window_material',None)
            if handle:handle.close()
            window.hide();window.deleteLater()
        APP.processEvents()
    def owner(self):
        owner=QWidget();owner.setAttribute(Qt.WA_TranslucentBackground);owner.resize(500,300)
        self.windows.append(owner);return owner
    def region(self):return MaterialRegion.rounded('card',QRectF(80,50,240,160),20)

    def test_offscreen_default_does_not_load_any_native_backend(self):
        owner=self.owner()
        with patch('window_material.CocoaMaterialBackend',side_effect=AssertionError('No native calls')):
            handle=install_material(owner);handle.set_regions([self.region()]);owner.show();APP.processEvents()
        self.assertFalse(handle.status()['attached']);self.assertEqual(handle.status()['reason'],'offscreen-disabled')

    def test_unique_owner_and_background_only_color(self):
        owner=self.owner();backend=RecordingBackend();handle=install_material(owner,backend=backend)
        self.assertIs(handle,install_material(owner,backend=RecordingBackend()))
        with self.assertRaises(ValueError):MaterialHandle(owner,backend=RecordingBackend())
        original=QColor(11,22,33,120);tinted=material_background(original)
        self.assertEqual(original.getRgb(),(11,22,33,120));self.assertAlmostEqual(tinted.alpha(),108,delta=1)
        self.assertEqual(tinted.getRgb()[:3],original.getRgb()[:3])
        with self.assertRaises(ValueError):install_material(QWidget(owner),backend=backend)

    def test_revision_and_path_copy_exclude_stale_or_mutated_pose(self):
        owner=self.owner();backend=RecordingBackend();handle=install_material(owner,backend=backend)
        region=self.region();handle.set_regions([region],revision=8);region.path.addRect(QRectF(0,0,5,5))
        self.assertFalse(handle._regions[0].path.contains(QRectF(0,0,5,5)))
        self.assertFalse(handle.set_regions([],revision=7));self.assertEqual(handle.status()['regions'],1)
        owner.show();APP.processEvents();self.assertEqual(backend.regions[0].path.boundingRect(),QRectF(80,50,240,160))
        with self.assertRaises(ValueError):handle.set_regions([self.region(),self.region()])

    def test_hiding_clears_mask_before_native_visibility_and_no_host_resize(self):
        owner=self.owner();backend=RecordingBackend();handle=install_material(owner,backend=backend)
        handle.set_regions([self.region()]);owner.show();APP.processEvents();geometry=owner.geometry()
        self.assertEqual(sum(c[0]=='attach' for c in backend.calls),1)
        for i in range(100):handle.set_regions([MaterialRegion.rounded('card',QRectF(80+i%3,50,240,160),20)])
        self.assertEqual(owner.geometry(),geometry);self.assertEqual(owner.windowOpacity(),1)
        self.assertIsNone(owner.graphicsEffect())
        owner.hide();APP.processEvents();self.assertEqual(backend.calls[-2][0:2],('regions',0));self.assertEqual(backend.calls[-1],('visible',False))
        owner.show();APP.processEvents();self.assertTrue(backend.visible);self.assertEqual(len(backend.regions),1)
        handle.close();self.assertEqual(handle.status()['regions'],0);self.assertFalse(handle.status()['attached'])

    def test_backend_failure_is_bounded_and_visible_without_private_error_text(self):
        class Failed(RecordingBackend):
            def set_regions(self,*args,**kwargs):raise RuntimeError('PRIVATE CLIPBOARD CONTENT')
        owner=self.owner();backend=Failed();handle=install_material(owner,backend=backend)
        handle.set_regions([self.region()]);owner.show();APP.processEvents()
        for _ in range(100):handle.set_visible(True)
        status=handle.status();self.assertEqual(status['error_count'],1);self.assertEqual(status['error_type'],'RuntimeError')
        self.assertNotIn('PRIVATE',str(status));self.assertEqual(status['reason'],'native-failed')

    def test_cocoa_one_background_sibling_below_qt_and_bounded_vector_mask(self):
        owner=self.owner();f=cocoa_fixture();handle=install_material(owner,backend=f.backend)
        handle.set_regions([self.region()]);owner.show();APP.processEvents()
        view=f.backend.view;self.assertEqual(f.parent.children,[view,f.native]);self.assertEqual(view.position,-1)
        self.assertEqual(view.frame,((80.,50.),(240.,160.)));self.assertTrue(view.mask.flipped)
        self.assertEqual(view.mask.size,(240,160));self.assertFalse(view.hidden)
        self.assertEqual(view.appearance,'Light');handle.set_theme('dark');self.assertEqual(view.appearance,'Dark')
        self.assertTrue(view.mask.draw(None));self.assertTrue(any(op[0]=='curve' for op in f.paths[0].ops))
        self.assertEqual(len(f.center.observers),1);self.assertFalse(handle.capabilities().verified)
        for i in range(100):handle.set_regions([MaterialRegion.rounded('card',QRectF(90+i%2,50,200,100),18)])
        self.assertEqual(f.parent.children,[view,f.native]);self.assertEqual(len(f.center.observers),1)

    def test_cocoa_reduced_transparency_hides_and_recovers_without_reparenting(self):
        owner=self.owner();f=cocoa_fixture();handle=install_material(owner,backend=f.backend)
        handle.set_regions([self.region()]);owner.show();APP.processEvents();view=f.backend.view
        f.workspace.reduced=True;f.center.observers[0].block(None)
        self.assertTrue(view.hidden);self.assertEqual(handle.capabilities().reason,'policy-disabled')
        self.assertEqual(handle.background_color('#FFFFFF').alpha(),255)
        self.assertFalse(handle.status()['visible'])
        f.workspace.reduced=False;f.center.observers[0].block(None)
        self.assertFalse(view.hidden);self.assertEqual(f.parent.children,[view,f.native])
        self.assertAlmostEqual(handle.background_color('#FFFFFF').alpha(),230,delta=1)
        owner.hide();APP.processEvents();self.assertTrue(view.hidden);self.assertIsNone(view.mask)
        handle.close();self.assertEqual(f.center.observers,[]);self.assertEqual(f.parent.children,[f.native]);self.assertIsNone(view.mask)
        self.assertIsNone(view.superview());f.backend.close();self.assertEqual(len(f.center.removed),1)

    def test_policy_change_signal_and_fail_safe_opaque_host(self):
        owner=self.owner();f=cocoa_fixture();handle=install_material(owner,backend=f.backend)
        changed=[];handle.changed.connect(lambda:changed.append(handle.capabilities().reason))
        handle.set_regions([self.region()]);owner.show();APP.processEvents()
        before=len(changed)
        for _ in range(10):handle.set_theme('dark')
        self.assertEqual(len(changed),before)
        f.workspace.reduced=True;f.center.observers[0].block(None)
        self.assertEqual(changed[-1],'policy-disabled')
        handle.close()
        other=self.owner();other.setAttribute(Qt.WA_TranslucentBackground,False)
        second=cocoa_fixture().backend
        self.assertFalse(second.attach(other,'panel'));self.assertFalse(second.capabilities().supported)
        self.assertEqual(second.capabilities().reason,'opaque-qt-host')

    def test_cocoa_outside_owner_is_clipped_and_empty_never_glasses_host(self):
        owner=self.owner();f=cocoa_fixture();handle=install_material(owner,backend=f.backend)
        handle.set_regions([MaterialRegion.rounded('part',QRectF(-30,-30,100,100),10)]);owner.show();APP.processEvents()
        self.assertEqual(f.backend.view.frame,((0.,0.),(70.,70.)))
        handle.set_regions([MaterialRegion.rounded('outside',QRectF(800,800,100,100),10)])
        self.assertTrue(f.backend.view.hidden);self.assertIsNone(f.backend.view.mask)

    def test_native_ci_probe_has_hard_user_desktop_guard(self):
        from ci_material_probe import verify_mac
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}):
            with self.assertRaisesRegex(RuntimeError,'disposable macOS'):verify_mac('must-not-write.json')

    def test_shape_budget_and_foreign_thread_updates_are_rejected_before_native(self):
        import threading
        for rect in (QRectF(float('nan'),0,10,10),QRectF(0,0,float('inf'),10),QRectF(1e20,0,10,10)):
            with self.assertRaises(ValueError):MaterialRegion.rounded('bad',rect,10)
        owner=self.owner();backend=RecordingBackend();handle=install_material(owner,backend=backend)
        failures=[]
        def wrong_thread():
            try:handle.set_regions([self.region()])
            except RuntimeError as error:failures.append(str(error))
        thread=threading.Thread(target=wrong_thread);thread.start();thread.join(1)
        self.assertFalse(thread.is_alive());self.assertEqual(len(failures),1)
        self.assertEqual(handle._regions,());self.assertEqual(backend.calls,[])


if __name__=='__main__':unittest.main()
