"""Clipped native *backgrounds*, independent of Qt foreground compositors.

Nothing installs itself globally. Owners explicitly opt in and provide their
current painted silhouette, never the bounds of a large animation host. The
native boundary is injectable; offscreen tests cannot accidentally touch DWM
or AppKit. Backend availability is not a claim of visual pixel verification.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import sys
import weakref

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, Signal, QThread
from PySide6.QtGui import QColor, QGuiApplication, QPainterPath
from shiboken6 import isValid


@dataclass(frozen=True)
class MaterialCapabilities:
    backend: str
    supported: bool
    clipped_regions: bool
    reason: str
    verified: bool = False


@dataclass(frozen=True)
class MaterialRegion:
    key: str
    path: QPainterPath
    alpha: float = 1.0

    def __post_init__(self):
        if not self.key or len(self.key) > 128:
            raise ValueError('A material region requires a bounded, nonempty key')
        if not math.isfinite(self.alpha) or not 0 <= self.alpha <= 1:
            raise ValueError('Material region alpha must be between zero and one')
        if self.path.elementCount() > 4096:
            raise ValueError('Material region geometry exceeds the bounded path budget')
        for i in range(self.path.elementCount()):
            e = self.path.elementAt(i)
            if not math.isfinite(e.x) or not math.isfinite(e.y) or max(abs(e.x),abs(e.y)) > 1e7:
                raise ValueError('Material region coordinates must be finite')
        object.__setattr__(self, 'path', QPainterPath(self.path))

    @classmethod
    def rounded(cls, key, rect, radius, alpha=1.0):
        rect = QRectF(rect)
        if any(not math.isfinite(n) or abs(n) > 1e7 for n in (rect.x(),rect.y(),rect.width(),rect.height())):
            raise ValueError('Material region coordinates must be finite and bounded')
        if not math.isfinite(radius) or radius < 0:
            raise ValueError('Material corner radius must be finite and positive')
        from continuous_corners import continuous_rect_path
        path = continuous_rect_path(rect,radius)
        return cls(key, path, alpha)


def material_background(color, factor=.90):
    """Only explicitly designated background paint may use this helper.

    It does not solve overlapping background ownership. Do not apply it to
    text, icons, image pixels, a complete QWidget snapshot or windowOpacity.
    """
    if not math.isfinite(factor) or not 0 <= factor <= 1:
        raise ValueError('Background alpha factor must be between zero and one')
    result = QColor(color)
    if not result.isValid():
        raise ValueError('Invalid material background color')
    result.setAlphaF(result.alphaF()*factor)
    return result


class UnavailableMaterialBackend:
    def __init__(self, reason='unsupported', backend='none'):
        self.reason, self.name = reason, backend

    def capabilities(self):
        return MaterialCapabilities(self.name, False, False, self.reason)

    def attach(self, owner, role): return False
    def set_regions(self, regions, revision=0): pass
    def set_theme(self, theme): pass
    def set_visible(self, visible): pass
    def close(self): pass


def _backend_for_platform():
    app = QGuiApplication.instance()
    plugin = app.platformName().lower() if app is not None else ''
    if plugin in ('offscreen', 'minimal') or not plugin:
        return UnavailableMaterialBackend('offscreen-disabled')
    if sys.platform == 'darwin' and plugin == 'cocoa':
        try:
            return CocoaMaterialBackend()
        except (ImportError, AttributeError, OSError):
            return UnavailableMaterialBackend('native-unavailable', 'cocoa')
    if sys.platform == 'win32' and plugin == 'windows':
        try:
            from window_material_win import create_backend
            return create_backend()
        except (ImportError, AttributeError, OSError):
            return UnavailableMaterialBackend('native-unavailable', 'windows')
    return UnavailableMaterialBackend('unsupported-platform')


class MaterialHandle(QObject):
    """One local owner, one native backing view, no animation clock or polling."""
    changed = Signal()

    def __init__(self, owner, *, role='panel', backend=None):
        app = QGuiApplication.instance()
        if app is None or app.thread() != QThread.currentThread():
            raise RuntimeError('Native materials require the GUI thread')
        if owner.window() is not owner:
            raise ValueError('Material owners must be top-level windows')
        previous = getattr(owner, '_window_material', None)
        if previous is not None and not previous._closed:
            raise ValueError('An owner can have only one material; use install_material')
        super().__init__(owner)
        self._owner = weakref.ref(owner)
        self.role = role
        self.backend = backend if backend is not None else _backend_for_platform()
        self._regions = ()
        self._revision = -1
        self._theme = 'light'
        self._requested_visible = True
        self._attached = False
        self._closed = False
        self._failure = None
        self._error_count = 0
        self._last_public_state = None
        owner.installEventFilter(self)
        owner.destroyed.connect(self._owner_destroyed)
        if hasattr(self.backend, 'set_policy_callback'):
            self.backend.set_policy_callback(self._policy_changed)
        owner._window_material = self

    def capabilities(self):
        if self._failure:
            return MaterialCapabilities(type(self.backend).__name__, False, False, 'native-failed')
        try: return self.backend.capabilities()
        except Exception as error:
            self._failure = type(error).__name__; self._error_count += 1
            try: self.backend.close()
            except Exception: pass
            self._attached = False
            return MaterialCapabilities(type(self.backend).__name__, False, False, 'native-failed')

    def status(self):
        c = self.capabilities()
        return dict(backend=c.backend, supported=c.supported, clipped_regions=c.clipped_regions,
                    reason=c.reason, verified=c.verified, attached=self._attached,
                    visible=bool(self._attached and c.supported and self._effective_visible()),
                    requested_visible=bool(self._effective_visible()), regions=len(self._regions),
                    revision=self._revision, closed=self._closed, error_count=self._error_count,
                    error_type=self._failure)

    def background_color(self, color, factor=.90):
        """Native-policy fallback; use only at the single background owner."""
        c = self.capabilities()
        return (material_background(color, factor) if not self._closed and c.supported and self._attached
                else QColor(color))

    def _notify_changed(self):
        c = self.capabilities()
        state = (c.backend,c.supported,c.reason,self._attached,self._closed)
        if state != self._last_public_state:
            self._last_public_state = state
            self.changed.emit()

    def set_regions(self, regions, revision=None):
        self._require_gui_thread()
        if self._closed: return False
        value = self._revision+1 if revision is None else int(revision)
        if value < self._revision: return False
        regions = tuple(regions)
        if len(regions) > 64 or len({r.key for r in regions}) != len(regions):
            raise ValueError('Material regions must have at most 64 unique keys')
        # Freeze callers' paths: QPainterPath remains mutable despite dataclass.
        copied = tuple(MaterialRegion(r.key, r.path, r.alpha) for r in regions
                       if r.alpha > 0 and not r.path.isEmpty())
        self._regions, self._revision = copied, value
        self._sync()
        return True

    def set_theme(self, theme):
        self._require_gui_thread()
        if theme not in ('light', 'dark'): raise ValueError('Unknown material theme')
        self._theme = theme
        self._sync()

    def set_visible(self, visible):
        self._require_gui_thread()
        self._requested_visible = bool(visible)
        self._sync()

    def _require_gui_thread(self):
        if self.thread() != QThread.currentThread():
            raise RuntimeError('Native material updates require the GUI thread')

    def _effective_visible(self):
        owner = self._owner()
        return (not self._closed and self._requested_visible and bool(self._regions)
                and owner is not None and isValid(owner) and owner.isVisible())

    def _sync(self):
        if self._closed or self._failure: return
        owner = self._owner()
        if owner is None or not isValid(owner): return
        visible = bool(self._effective_visible())
        try:
            if not self._attached and visible:
                self._attached = bool(self.backend.attach(owner, self.role))
            if self._attached:
                self.backend.set_theme(self._theme)
                # Set the clip before unhiding native material; never briefly
                # expose a preallocated rectangular animation reservation.
                self.backend.set_regions(self._regions if visible else (), revision=self._revision)
                self.backend.set_visible(visible)
        except Exception as error:
            # Never log geometry, clipboard data, filenames or native objects.
            self._failure = type(error).__name__
            self._error_count += 1
            try: self.backend.close()
            except Exception: pass
            self._attached = False
        self._notify_changed()

    def _policy_changed(self): self._sync()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.Hide:
            if self._attached:
                try:
                    self.backend.set_regions((), revision=self._revision)
                    self.backend.set_visible(False)
                except Exception as error:
                    self._failure = type(error).__name__; self._error_count += 1
                    try: self.backend.close()
                    except Exception: pass
                    self._attached = False
        elif kind == QEvent.Type.WinIdChange:
            if self._attached:
                try: self.backend.close()
                except Exception as error:
                    self._failure = type(error).__name__; self._error_count += 1
                self._attached = False
                self._notify_changed()
            # This event may happen while winId() is creating a native view.
            # Do not recursively attach from inside that native creation.
        elif kind in (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.Move,
                      QEvent.Type.DevicePixelRatioChange):
            self._sync()
        return False

    def _owner_destroyed(self, *_): self.close()

    def close(self):
        self._require_gui_thread()
        if self._closed: return
        self._closed = True
        self._regions = ()
        owner = self._owner()
        if owner is not None and isValid(owner): owner.removeEventFilter(self)
        try: self.backend.close()
        except Exception as error:
            self._failure = type(error).__name__; self._error_count += 1
        self._attached = False
        self._notify_changed()


def install_material(owner, *, role='panel', backend=None):
    previous = getattr(owner, '_window_material', None)
    if previous is not None and not previous._closed:
        return previous
    handle = MaterialHandle(owner, role=role, backend=backend)
    owner._window_material = handle
    return handle


_cocoa_view_class = None


def _cocoa_view_type(appkit):
    global _cocoa_view_class
    if _cocoa_view_class is None:
        class FlickDockMaterialView(appkit.NSVisualEffectView):
            def isFlipped(self): return True
            def isOpaque(self): return False
            def acceptsFirstResponder(self): return False
            def hitTest_(self, point): return None
            def allowsVibrancy(self): return False
        _cocoa_view_class = FlickDockMaterialView
    return _cocoa_view_class


def _bezier_path(appkit, path, offset):
    result = appkit.NSBezierPath.bezierPath()
    if path.fillRule() == Qt.FillRule.OddEvenFill:
        result.setWindingRule_(appkit.NSEvenOddWindingRule)
    i = 0
    while i < path.elementCount():
        e = path.elementAt(i); point = (e.x-offset.x(), e.y-offset.y())
        if e.isMoveTo(): result.moveToPoint_(point)
        elif e.isLineTo(): result.lineToPoint_(point)
        else:
            if i+2 >= path.elementCount(): raise ValueError('Malformed material curve')
            c2, end = path.elementAt(i+1), path.elementAt(i+2)
            result.curveToPoint_controlPoint1_controlPoint2_(
                (end.x-offset.x(), end.y-offset.y()), point, (c2.x-offset.x(), c2.y-offset.y()))
            i += 2
        i += 1
    return result


class CocoaMaterialBackend:
    """A masked native sibling *below* QNSView; never an overlay on its text.

    AppKit handles native blur. maskImage is a lazily rasterized vector image,
    bounded to visible regions rather than the preallocated NSWindow. No host
    geometry/opacity/level/delegate/contentView setting is changed.
    """
    def __init__(self, *, appkit=None, foundation=None, objc=None, view_factory=None,
                 native_view=None):
        if appkit is None: import AppKit as appkit
        if foundation is None: import Foundation as foundation
        if objc is None: import objc
        self.appkit, self.foundation, self.objc = appkit, foundation, objc
        self._view_factory, self._native_view_resolver = view_factory, native_view
        self.owner = self.native_view = self.parent_view = self.view = None
        self._workspace = appkit.NSWorkspace.sharedWorkspace()
        self._observer = None
        self._callback = None
        self._regions = ()
        self._visible = False
        self._theme = 'light'
        self._mask = None
        self._mask_owner_size = None
        self._reason = 'native-api-unverified'

    def set_policy_callback(self, callback):
        try: self._callback = weakref.WeakMethod(callback)
        except TypeError: self._callback = lambda: callback

    def _reduced(self):
        return bool(self._workspace.accessibilityDisplayShouldReduceTransparency())

    def capabilities(self):
        reduced = self._reduced()
        supported = not reduced and self._reason not in ('opaque-qt-host','missing-native-parent')
        return MaterialCapabilities('cocoa-visual-effect', supported, True,
                                    'policy-disabled' if reduced else self._reason, False)

    def attach(self, owner, role):
        if self.view is not None: return True
        if not owner.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
            self._reason = 'opaque-qt-host'; return False
        import ctypes
        native = (self._native_view_resolver(owner) if self._native_view_resolver else
                  self.objc.objc_object(c_void_p=ctypes.c_void_p(int(owner.winId()))))
        parent = native.superview()
        if parent is None or native.window() is None:
            self._reason = 'missing-native-parent'; return False
        view_type = self._view_factory or _cocoa_view_type(self.appkit)
        view = view_type.alloc().initWithFrame_(((0, 0), (1, 1)))
        view.setHidden_(True)
        try:
            view.setAccessibilityElement_(False)
            view.setBlendingMode_(self.appkit.NSVisualEffectBlendingModeBehindWindow)
            view.setMaterial_(self.appkit.NSVisualEffectMaterialPopover)
            view.setState_(self.appkit.NSVisualEffectStateActive)
            # This insertion changes no Qt view ownership, menu, delegate,
            # window focus or level; background is below the entire Qt tree.
            parent.addSubview_positioned_relativeTo_(view, self.appkit.NSWindowBelow, native)
            self._reason = 'native-api-unverified'
            self.owner = weakref.ref(owner)
            self.native_view, self.parent_view, self.view = native, parent, view
            center = self._workspace.notificationCenter()
            backend_ref = weakref.ref(self)
            def changed(_):
                backend = backend_ref()
                if backend is not None:
                    backend._apply_visibility()
                    callback = backend._callback() if backend._callback else None
                    if callback is not None: callback()
            self._observer = center.addObserverForName_object_queue_usingBlock_(
                self.appkit.NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification,
                None, self.foundation.NSOperationQueue.mainQueue(), changed)
        except Exception:
            view.removeFromSuperview()
            self.close()
            raise
        return True

    def set_theme(self, theme):
        self._theme = theme
        if self.view is not None:
            name = self.appkit.NSAppearanceNameDarkAqua if theme == 'dark' else self.appkit.NSAppearanceNameAqua
            self.view.setAppearance_(self.appkit.NSAppearance.appearanceNamed_(name))

    def set_regions(self, regions, revision=0):
        regions = tuple(regions)
        if self.view is None: return
        owner = self.owner() if self.owner else None
        if owner is None or not isValid(owner): self.close(); return
        # Crop the backing image/frame, not the Bezier paths. Boolean path
        # unions/intersections may flatten overlapping G2 curves into polygons.
        # The image bounds safely clip partial regions without altering their
        # original cubic control points or the Qt foreground/input window.
        allowed = QRectF(owner.rect())
        clipped = tuple(MaterialRegion(r.key, r.path, r.alpha) for r in regions
                        if r.alpha > 0 and not r.path.isEmpty()
                        and r.path.boundingRect().intersects(allowed))
        if not clipped:
            self._regions = (); self._mask = None; self._mask_owner_size = None
            self.view.setHidden_(True); self.view.setMaskImage_(None)
            return
        owner_size=(owner.width(),owner.height())
        if clipped == self._regions and self._mask is not None and owner_size==self._mask_owner_size:
            self._apply_visibility(); return
        bounds = QRectF()
        for region in clipped: bounds = bounds.united(region.path.boundingRect())
        bounds = bounds.intersected(allowed).toAlignedRect()
        if bounds.isEmpty(): return
        origin = bounds.topLeft()
        # Qt owner coordinates are top-left logical points; convertRect handles
        # the native superview's flippedness instead of guessing screen height.
        native_rect = self.native_view.convertRect_toView_(
            ((float(bounds.x()), float(bounds.y())), (float(bounds.width()), float(bounds.height()))), self.parent_view)
        if not self.native_view.isFlipped():
            h = float(self.native_view.bounds().size.height)
            native_rect = self.native_view.convertRect_toView_(
                ((float(bounds.x()), h-float(bounds.bottom()+1)), (float(bounds.width()), float(bounds.height()))), self.parent_view)
        kit = self.appkit
        # Copy each original path in alpha order. Equal-alpha overlap stays at
        # that alpha, while all cubic curves and any odd-even holes survive.
        # A geometric union would turn overlapping continuous corners into
        # hundreds of straight segments before AppKit ever receives them.
        paths = tuple((region.alpha, _bezier_path(kit, region.path, origin))
                      for region in sorted(clipped,key=lambda region:region.alpha))
        def draw(_rect):
            context = kit.NSGraphicsContext.currentContext()
            kit.NSGraphicsContext.saveGraphicsState()
            try:
                context.setCompositingOperation_(kit.NSCompositingOperationCopy)
                for alpha, path in paths:
                    kit.NSColor.colorWithCalibratedWhite_alpha_(1., alpha).setFill()
                    path.fill()
            finally: kit.NSGraphicsContext.restoreGraphicsState()
            return True
        mask = kit.NSImage.imageWithSize_flipped_drawingHandler_((bounds.width(), bounds.height()), True, draw)
        self.view.setHidden_(True)
        self.view.setFrame_(native_rect)
        self.view.setMaskImage_(mask)
        self._regions, self._mask = clipped, mask
        self._mask_owner_size = owner_size
        self._apply_visibility()

    def set_visible(self, visible):
        self._visible = bool(visible); self._apply_visibility()

    def _apply_visibility(self):
        if self.view is not None:
            self.view.setHidden_(not (self._visible and self._regions and not self._reduced()))

    def close(self):
        self._visible = False; self._regions = (); self._mask = None; self._mask_owner_size = None
        observer, self._observer = self._observer, None
        if observer is not None:
            self._workspace.notificationCenter().removeObserver_(observer)
        view, self.view = self.view, None
        if view is not None:
            view.setHidden_(True); view.setMaskImage_(None); view.removeFromSuperview()
        self.native_view = self.parent_view = self.owner = None
