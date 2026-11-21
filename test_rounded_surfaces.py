"""Physical-pixel checks for the shared rounded surface painter."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QRectF,Qt
from PySide6.QtGui import QColor,QImage,QPainter

from native_ui_common import paint_rounded_surface


class RoundedSurfaceTests(unittest.TestCase):
    def test_straight_outline_occupies_one_device_pixel_at_supported_scales(self):
        for theme in ('dark','light'):
            for scale in (1.,1.25,1.5,1.75,2.):
                with self.subTest(theme=theme,scale=scale):
                    background=QColor('#111111' if theme=='dark' else '#F6F6F6')
                    fill=QColor('#272727' if theme=='dark' else '#FFFFFF')
                    stroke=QColor('#D8D8D8' if theme=='dark' else '#444444')
                    image=QImage(round(100*scale),round(70*scale),QImage.Format_ARGB32_Premultiplied)
                    image.setDevicePixelRatio(scale);image.fill(background)
                    painter=QPainter(image)
                    paint_rounded_surface(painter,QRectF(4,4,80,50),12,fill,stroke)
                    painter.end()
                    x=round(44*scale);y=round(4*scale)
                    def difference(actual,expected):
                        return max(abs(a-b) for a,b in zip(actual.getRgb(),expected.getRgb()))
                    self.assertLessEqual(difference(image.pixelColor(x,y),stroke),3)
                    self.assertLessEqual(difference(image.pixelColor(x,y+1),fill),1)
                    self.assertLessEqual(difference(image.pixelColor(x,y-1),background),3)
                    left=round(4*scale);center_y=round(29*scale)
                    self.assertLessEqual(difference(image.pixelColor(left,center_y),stroke),3)
                    self.assertLessEqual(difference(image.pixelColor(left+1,center_y),fill),1)
                    self.assertLessEqual(difference(image.pixelColor(left-1,center_y),background),3)
                    corner=[image.pixelColor(px,py).rgba()
                            for px in range(round(4*scale),round(16*scale))
                            for py in range(round(4*scale),round(16*scale))]
                    self.assertTrue(any(pixel not in (background.rgba(),fill.rgba(),stroke.rgba()) for pixel in corner))

    def test_transparent_fill_and_painter_state_are_preserved(self):
        image=QImage(80,60,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
        painter=QPainter(image);painter.setOpacity(.7)
        painter.setRenderHint(QPainter.Antialiasing,False)
        paint_rounded_surface(painter,QRectF(5,5,60,40),8,QColor('#FFFFFF'))
        self.assertAlmostEqual(painter.opacity(),.7)
        self.assertFalse(painter.testRenderHint(QPainter.Antialiasing))
        painter.end()
        self.assertEqual(image.pixelColor(0,0).alpha(),0)
        self.assertGreater(image.pixelColor(35,25).alpha(),0)
        self.assertEqual(image.pixelColor(5,5).alpha(),0)

    def test_fractional_origins_keep_all_preview_edges_in_parent_composite(self):
        for scale in ('1','1.25','1.5','1.75','2'):
            with self.subTest(scale=scale):
                env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QT_SCALE_FACTOR=scale,PYTHONUTF8='1')
                run=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--composite-probe'],
                                   cwd=Path(__file__).parent,env=env,capture_output=True,text=True,
                                   encoding='utf-8',errors='replace',timeout=30)
                self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)


def _composite_probe():
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    import tempfile
    from unittest.mock import patch
    import test_html_native_workspace as fixture
    import shelf_ui_common,ui_theme
    import overview_ui,content_grid
    from native_ui_common import colors
    from reference_widgets import refresh_tree
    case=fixture.WorkspaceTests();case.setUp()
    try:
        popup=case.overview();popup.select_item(case.paths[1]);fixture.flush()
        for theme in ('dark','light'):
            ui_theme.set_theme(theme);case.settings.update(theme=theme)
            ui_theme.apply_application(fixture.APP,shelf_ui_common.FONT_FAMILY)
            refresh_tree(case.shelf);fixture.flush()
            image=popup.grab().toImage();scale=image.devicePixelRatioF()
            palette=colors(theme)
            def check_edges(widget,expected):
                origin=widget.mapTo(popup,QPoint());x=origin.x();y=origin.y();w=widget.width();h=widget.height()
                mid_x=round((x+w/2)*scale);mid_y=round((y+h/2)*scale)
                edges=((mid_x,round(y*scale),0,1),(mid_x,round((y+h)*scale),0,1),
                       (round(x*scale),mid_y,1,0),(round((x+w)*scale),mid_y,1,0))
                for px,py,dx,dy in edges:
                    samples=[image.pixelColor(px+dx*offset,py+dy*offset).red() for offset in range(-3,4)]
                    assert sum(abs(value-expected)<=2 for value in samples)==1,(theme,scale,widget.objectName(),samples)
            row=next(item for item in popup.rows_body.findChildren(overview_ui.FileRow) if item.path==case.paths[1])
            for widget in (popup.preview.stack,popup.preview.status_card,popup.preview.size_card,popup.preview.path_card):
                check_edges(widget,41 if theme=='dark' else 229)
            check_edges(popup.search_edit,52 if theme=='dark' else 229)
            check_edges(row,QColor(palette['selected_border']).red())
            popup.view_grid.click();QTest.qWait(220);fixture.flush();image=popup.grab().toImage()
            tile=next(item for item in popup.rows_body.findChildren(content_grid.IconItem) if item.ref==case.paths[1])
            check_edges(tile,QColor(palette['grid_selected_border']).red())
            popup.view_list.click();QTest.qWait(220);fixture.flush()
    finally:case.tearDown()
    from app_settings import SettingsDialog,SettingsStore
    with tempfile.TemporaryDirectory(prefix='flickdock-settings-aa-') as root:
        with patch('app_settings._startup_runtime_allowed',return_value=False):
            dialog=SettingsDialog(SettingsStore(Path(root)));dialog.chrome.prepare_open=lambda:None
            try:
                dialog.show();QTest.qWait(80)
                for theme in ('dark','light'):
                    ui_theme.set_theme(theme);ui_theme.apply_application(fixture.APP,shelf_ui_common.FONT_FAMILY)
                    dialog.apply_theme(theme);dialog.close_button.setFocus();QTest.qWait(60)
                    image=dialog.grab().toImage();scale=image.devicePixelRatioF()
                    for widget,expected in ((dialog.shelf_shortcut_edit,58 if theme=='dark' else 229),
                                            (dialog.overview_shortcut_edit,58 if theme=='dark' else 229)):
                        origin=widget.mapTo(dialog,QPoint());x=origin.x();y=origin.y();w=widget.width();h=widget.height()
                        mid_x=round((x+w/2)*scale);mid_y=round((y+h/2)*scale)
                        edges=((mid_x,round(y*scale),0,1),(mid_x,round((y+h)*scale),0,1),
                               (round(x*scale),mid_y,1,0),(round((x+w)*scale),mid_y,1,0))
                        for px,py,dx,dy in edges:
                            samples=[image.pixelColor(px+dx*offset,py+dy*offset).red() for offset in range(-3,4)]
                            assert sum(abs(value-expected)<=2 for value in samples)==1,(theme,scale,widget.accessibleName(),samples)
            finally:
                dialog.close();dialog._preference_writer.close();dialog.deleteLater();fixture.flush()


if __name__=='__main__':
    if sys.argv[1:]==['--composite-probe']:_composite_probe()
    else:unittest.main()
