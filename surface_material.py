"""One native shelf backdrop, aligned with its existing source-pixmap pose.

Only MotionShadow paints the main surface color. The live card and cached
endpoints contain foreground and subordinate content, never a second base.
No polling, desktop capture, native host resize or foreground opacity change.
"""
from PySide6.QtCore import QObject,QEvent,QRectF
from PySide6.QtGui import QTransform
from window_material import install_material,MaterialRegion
from surface_morph import surface_clip
import ui_theme


class ShelfMaterial(QObject):
    def __init__(self,owner,*,backend=None):
        super().__init__(owner);self.owner=owner;self.card=owner.card
        self.handle=install_material(owner,role='shelf',backend=backend)
        self._theme=None;self._path=None;self._visible=None;self._refreshing=False
        self.card.installEventFilter(self);owner.installEventFilter(self)
        owner.surface_effect.pose_changed.connect(self.sync)
        self.handle.changed.connect(self._policy_changed)
        owner._surface_material=self
        self.sync()

    def background_color(self,color):return self.handle.background_color(color)

    def _policy_changed(self):
        # Endpoint snapshots contain no base color, so a policy change needs
        # no recapture/rebuild and cannot invalidate the user's current pose.
        self.card.update();self.owner.surface_effect.update()

    def sync(self):
        if self._refreshing:return
        self._refreshing=True
        try:
            theme=ui_theme.current_theme()
            if theme!=self._theme:self._theme=theme;self.handle.set_theme(theme)
            effect=self.owner.surface_effect;snapshot=getattr(effect,'_surface_snapshot',None)
            if snapshot is not None and snapshot.active:
                rect=snapshot.content_rect();transform=snapshot.presentation_transform()
            else:
                rect=QRectF(self.card.geometry());center=rect.center();transform=QTransform()
                transform.translate(center.x()+effect.dx,center.y()+effect.dy)
                transform.scale(effect.sx,effect.sy);transform.translate(-center.x(),-center.y())
            path=transform.map(surface_clip(rect))
            if self._path is None or path!=self._path:
                self._path=path;self.handle.set_regions([MaterialRegion('shelf-main',path)])
            visible=not self.card.isHidden()
            if visible!=self._visible:self._visible=visible;self.handle.set_visible(visible)
        finally:self._refreshing=False

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Show,QEvent.Resize,QEvent.Move,QEvent.Hide,
                            QEvent.DevicePixelRatioChange,QEvent.PaletteChange):self.sync()
        return False
