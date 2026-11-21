"""Live trigger preview geometry, visibility and mouse transparency, offscreen."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication
from dock_geometry import trigger_rect
from dock_preview import DockPreview

app=QApplication([])
area=QRect(0,0,1920,1080)
view=DockPreview()
base={'dock_edge':'right','dock_position':8,'dock_length':76,'dock_thickness':8,
      'dock_color':'#dd3344','dock_opacity':60,'trigger_visible':True}
for edge in ('right','left','top','bottom'):
    for position in (0,35,100):
        values=dict(base,dock_edge=edge,dock_position=position,dock_length=240,dock_thickness=18)
        view.show_preview(area,values);app.processEvents()
        assert view.isVisible()
        assert view.target_rect==trigger_rect(area,edge,position,240,18)
        assert area.contains(view.badge_rect)
        assert view.geometry().contains(view.target_rect)
        assert view.geometry().contains(view.badge_rect)
        assert not view.badge_rect.intersects(view.target_rect)
        assert '240 × 18' in view.caption
        assert view.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert view.windowFlags() & Qt.WindowType.WindowTransparentForInput
        image=view.grab().toImage()
        point=view.target_rect.center()-view.geometry().topLeft()
        pixel=image.pixelColor(round(point.x()*image.devicePixelRatio()),round(point.y()*image.devicePixelRatio()))
        assert abs(pixel.alpha()-153)<=2,(pixel.alpha(),edge)
        assert pixel.red()>200 and pixel.blue()<90
view.show_preview(area,dict(base,trigger_visible=False,dock_opacity=0));app.processEvents()
assert '已隐藏标记' in view.caption
view.hide();assert not view.isVisible()
print('DOCK_PREVIEW_V16_OK four edges/position/length/thickness/color/opacity/input transparency')
