"""Offscreen regressions for real switch input, platform fonts and crisp UI."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QPoint,QPointF,QRect,QRectF,QSignalBlocker,Qt
from PySide6.QtGui import QColor,QFont,QFontDatabase,QFontInfo,QFontMetricsF,QImage,QPainter,QTextLayout,QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QButtonGroup,QHBoxLayout,QLabel,QPushButton,QScrollArea,QVBoxLayout,QWidget
from app_settings import SettingsDialog,SettingsStore,SettingsToggleSwitch
from clipboard_preview import clipboard_is_link
from native_ui_common import colors,configure_reference_font,icon,pixel_aligned_border,readable_qss,set_ui_weight

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
configure_reference_font(APP)

class PlatformUiTests(unittest.TestCase):
    def test_every_enabled_settings_switch_tracks_real_click_and_keyboard(self):
        with tempfile.TemporaryDirectory() as temp,patch('app_settings._startup_runtime_allowed',return_value=False),patch.dict(os.environ,FLICKDOCK_REDUCE_MOTION='1'):
            store=SettingsStore(Path(temp)/'profile');dialog=SettingsDialog(store);dialog.show();APP.processEvents()
            switches=[s for s in dialog.findChildren(SettingsToggleSwitch) if s.isEnabled() and s.isVisible()]
            self.assertGreaterEqual(len(switches),10)
            try:
                for control in switches:
                    with self.subTest(control=control.accessibleName()):
                        dialog.scroll.ensureWidgetVisible(control);APP.processEvents()
                        before=control.isChecked()
                        QTest.mouseClick(control,Qt.LeftButton,Qt.NoModifier,QPoint(control.width()-25,control.height()//2));APP.processEvents()
                        self.assertNotEqual(control.isChecked(),before)
                        self.assertEqual(control._thumb_position,float(control.isChecked()))
                        QTest.keyClick(control,Qt.Key_Space);APP.processEvents()
                        self.assertEqual(control.isChecked(),before)
                        self.assertEqual(control._thumb_position,float(before))
                control=switches[0]
                with QSignalBlocker(control):control.setChecked(not control.isChecked())
                self.assertEqual(control._thumb_position,float(control.isChecked()))
            finally:dialog.close();dialog.deleteLater();APP.processEvents()

    def test_switch_animation_finishes_on_click(self):
        with patch.dict(os.environ,FLICKDOCK_REDUCE_MOTION='0'):
            control=SettingsToggleSwitch('test');control.setFixedSize(44,32);control.show();APP.processEvents()
            QTest.mouseClick(control,Qt.LeftButton,Qt.NoModifier,QPoint(19,16));QTest.qWait(220)
            self.assertTrue(control.isChecked());self.assertAlmostEqual(control._thumb_position,1.,places=2)
            control.close()

    def test_settings_navigation_reaches_each_category_and_latest_rapid_click(self):
        with tempfile.TemporaryDirectory() as temp,patch('app_settings._startup_runtime_allowed',return_value=False),patch.dict(os.environ,FLICKDOCK_REDUCE_MOTION='0'):
            dialog=SettingsDialog(SettingsStore(Path(temp)/'profile'));dialog.show();APP.processEvents()
            try:
                nav=dialog._anchor_navigation
                for index in (*range(len(dialog.setting_categories)),2,6,1):
                    rect=dialog.category_list.visualItemRect(dialog.category_list.item(index))
                    QTest.mouseClick(dialog.category_list.viewport(),Qt.LeftButton,Qt.NoModifier,rect.center());APP.processEvents()
                    if nav.motion.state()==nav.motion.Running:nav.motion.setCurrentTime(nav.motion.duration())
                    APP.processEvents()
                    section=dialog.setting_categories[index][1][0]
                    expected=max(nav.bar.minimum(),min(nav.bar.maximum(),section.y()-20))
                    self.assertEqual(dialog.category_list.currentRow(),index)
                    self.assertLessEqual(abs(nav.bar.value()-expected),1,(index,nav.bar.value(),expected))
                for index in (1,5,2):
                    rect=dialog.category_list.visualItemRect(dialog.category_list.item(index))
                    QTest.mouseClick(dialog.category_list.viewport(),Qt.LeftButton,Qt.NoModifier,rect.center());APP.processEvents()
                    if nav.motion.state()==nav.motion.Running:nav.motion.setCurrentTime(45)
                if nav.motion.state()==nav.motion.Running:nav.motion.setCurrentTime(nav.motion.duration())
                self.assertEqual(dialog.category_list.currentRow(),2)
                self.assertEqual(nav.anchor[0],2)
            finally:dialog.close();dialog.deleteLater();APP.processEvents()

    def test_other_scroll_area_mouse_wheel_glides_and_can_reverse(self):
        with patch.dict(os.environ,FLICKDOCK_REDUCE_MOTION='0'):
            area=QScrollArea();area.resize(240,160);body=QWidget();body.resize(220,1600);area.setWidget(body);area.show();APP.processEvents()
            try:
                view=area.viewport();pos=QPointF(view.rect().center());global_pos=QPointF(view.mapToGlobal(pos.toPoint()))
                def wheel(angle,pixels=0,phase=Qt.NoScrollPhase):
                    event=QWheelEvent(pos,global_pos,QPoint(0,pixels),QPoint(0,angle),Qt.NoButton,Qt.NoModifier,phase,False)
                    APP.sendEvent(view,event);APP.processEvents()
                wheel(-120)
                controller=area._flickdock_wheel_scroll
                self.assertEqual(controller.motion.state(),controller.motion.Running)
                self.assertLess(area.verticalScrollBar().value(),controller.target)
                controller.motion.setCurrentTime(100);middle=area.verticalScrollBar().value()
                wheel(120);self.assertLess(controller.target,middle)
                controller.motion.setCurrentTime(controller.motion.duration());self.assertLessEqual(area.verticalScrollBar().value(),middle)
                wheel(-120);wheel(0,-7,Qt.ScrollMomentum)
                self.assertEqual(controller.motion.state(),controller.motion.Stopped)
                with patch.dict(os.environ,FLICKDOCK_REDUCE_MOTION='1'):
                    wheel(-120);self.assertEqual(controller.motion.state(),controller.motion.Stopped)
            finally:area.close();area.deleteLater();APP.processEvents()

    def test_selection_motion_retargets_and_long_vertical_travel_fades(self):
        from ui_transitions import SelectionSurface
        with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            horizontal=SelectionSurface();row=QHBoxLayout(horizontal);group=QButtonGroup(horizontal);group.setExclusive(True)
            first=QPushButton('一',horizontal);second=QPushButton('二',horizontal)
            for button in (first,second):
                button.setCheckable(True);button.setFixedSize(70,30);row.addWidget(button);group.addButton(button);horizontal.add_selection_button(button)
            first.setChecked(True);horizontal.show();APP.processEvents()
            try:
                second.click();spring=horizontal._selection_animation
                self.assertTrue(second.isChecked());self.assertTrue(spring._timer.isActive())
                spring._last_tick-=.04;spring._tick();middle=horizontal._selection_rect.x()
                self.assertGreater(middle,first.x());self.assertLess(middle,second.x()+2)
                velocity=spring._velocities['x'];first.click()
                self.assertEqual(spring._targets['x'],float(first.x()))
                self.assertEqual(spring._velocities['x'],velocity)
                for _ in range(50):spring._last_tick-=.016;spring._tick()
                self.assertLessEqual(abs(horizontal._selection_rect.x()-first.x()),1.)
            finally:horizontal.close();horizontal.deleteLater();APP.processEvents()
            vertical=SelectionSurface(vertical=True,color_role='action',border_role=None);column=QVBoxLayout(vertical);group=QButtonGroup(vertical)
            buttons=[]
            for index in range(5):
                button=QPushButton(str(index),vertical);button.setCheckable(True);button.setFixedSize(100,30)
                column.addWidget(button);group.addButton(button);vertical.add_selection_button(button);buttons.append(button)
            buttons[0].setChecked(True);vertical.show();APP.processEvents()
            try:
                buttons[1].click();animation=vertical._selection_animation
                self.assertFalse(vertical._selection_warp);animation.setCurrentTime(70)
                self.assertGreater(vertical._selection_rect.y(),buttons[0].y());self.assertEqual(vertical._selection_opacity,1.)
                buttons[4].click();self.assertTrue(vertical._selection_warp)
                animation.setCurrentTime(animation.duration()//2);self.assertLess(vertical._selection_opacity,.05)
                animation.setCurrentTime(round(animation.duration()*.75));self.assertGreater(vertical._selection_opacity,.3)
                opacity=vertical._selection_opacity;buttons[0].click()
                self.assertAlmostEqual(vertical._selection_start_opacity,opacity,places=3)
                animation.setCurrentTime(animation.duration());self.assertAlmostEqual(vertical._selection_opacity,1.)
            finally:vertical.close();vertical.deleteLater();APP.processEvents()
        with patch.dict(os.environ,FLICKDOCK_REDUCE_MOTION='1'):
            surface=SelectionSurface();row=QHBoxLayout(surface);group=QButtonGroup(surface)
            a=QPushButton('a',surface);b=QPushButton('b',surface)
            for button in (a,b):button.setCheckable(True);row.addWidget(button);group.addButton(button);surface.add_selection_button(button)
            a.setChecked(True);surface.show();APP.processEvents();b.click()
            self.assertEqual(surface._selection_rect,QRectF(b.geometry()))
            self.assertFalse(surface._selection_animation._timer.isActive())
            surface.close();surface.deleteLater();APP.processEvents()

    def test_system_font_has_real_latin_and_chinese_glyphs(self):
        font=configure_reference_font(APP)
        if sys.platform=='win32':
            self.assertEqual(font.family(),'Microsoft YaHei')
            self.assertEqual(QFontInfo(font).family(),'Microsoft YaHei')
        for character in ('A','7','中'):
            layout=QTextLayout(character,font);layout.beginLayout();layout.createLine();layout.endLayout()
            runs=layout.glyphRuns();self.assertTrue(runs)
            self.assertTrue(all(run.rawFont().isValid() and all(glyph!=0 for glyph in run.glyphIndexes()) for run in runs))

    @unittest.skipUnless(sys.platform=='win32','Windows SVG glyph rendering')
    def test_svg_file_type_labels_inherit_actual_microsoft_yahei(self):
        from flickdock_icons import document_svg,_render_svg
        source=document_svg('.zip')
        self.assertIn('>ZIP</text>',source)
        self.assertNotIn('font-family=',source)
        explicit=source.replace('<text x=', '<text font-family="Microsoft YaHei" x=',1)
        actual=_render_svg(source,128,1).toImage()
        yahei=_render_svg(explicit,128,1).toImage()
        self.assertEqual(bytes(actual.constBits()),bytes(yahei.constBits()))
        arial=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'Fonts'/'arial.ttf'
        if arial.is_file() and QFontDatabase.addApplicationFont(str(arial))>=0:
            other=_render_svg(explicit.replace('Microsoft YaHei','Arial'),128,1).toImage()
            self.assertNotEqual(bytes(actual.constBits()),bytes(other.constBits()))

    def test_emphasis_is_lighter_and_reapplying_styles_does_not_reduce_it_again(self):
        style=readable_qss('QLabel{font-weight:600;} QPushButton{font-weight:500;}')
        self.assertIn('font-weight:500',style)
        self.assertIn('font-weight:400',style)
        self.assertEqual(readable_qss(style),style)
        font=QFont(APP.font());set_ui_weight(font,600);self.assertEqual(int(font.weight()),500)
        set_ui_weight(font,400);self.assertEqual(int(font.weight()),400)
        with tempfile.TemporaryDirectory() as temp,patch('app_settings._startup_runtime_allowed',return_value=False):
            dialog=SettingsDialog(SettingsStore(Path(temp)/'profile'));dialog.show();APP.processEvents()
            try:
                titles=dialog.findChildren(QLabel,'settingsSection')
                self.assertTrue(titles)
                self.assertTrue(all(int(title.font().weight())<=500 for title in titles))
                from reference_widgets import IconTextButton
                segments=[b for b in dialog.findChildren(QPushButton,'settingsSegmentButton') if b.property('segmentGlyph')]
                self.assertEqual(len(segments),4)
                self.assertTrue(all(isinstance(button,IconTextButton) and button.iconSize().height()==15 for button in segments))
                for button in segments:
                    self.assertEqual(int(button.font().weight()),400)
                    self.assertGreaterEqual(button.height(),button.fontMetrics().height())
                segments[1].click();APP.processEvents()
                self.assertEqual(dialog.theme_combo.currentData(),'dark')
                self.assertTrue(all(button.property('referenceTheme')=='dark' for button in segments))
            finally:dialog.close();dialog.deleteLater();APP.processEvents()

    def test_link_detection_has_a_visible_chain_icon(self):
        self.assertTrue(clipboard_is_link(('text',{'text':'https://example.com/path'})))
        self.assertTrue(clipboard_is_link(('text',{'text':'example.com'})))
        self.assertFalse(clipboard_is_link(('text',{'text':'这是一段普通文字'})))
        self.assertFalse(clipboard_is_link(('files',['a.txt'])))
        image=icon('link','#000000').pixmap(24,24).toImage()
        self.assertTrue(any(image.pixelColor(x,y).alpha()>0 for x in range(image.width()) for y in range(image.height())))

    def test_edge_trigger_options_have_their_theme_background(self):
        with tempfile.TemporaryDirectory() as temp,patch('app_settings._startup_runtime_allowed',return_value=False),patch.dict(os.environ,FLICKDOCK_REDUCE_MOTION='1'):
            dialog=SettingsDialog(SettingsStore(Path(temp)/'profile'));dialog.select_category('外观与交互');dialog.show();APP.processEvents()
            try:
                for theme in ('light','dark'):
                    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData(theme));APP.processEvents()
                    for switch in (dialog.trigger_visible_checkbox,dialog.keep_empty_handle_checkbox):
                        option=switch.parentWidget();image=option.grab().toImage()
                        self.assertEqual(image.pixelColor(8,image.height()//2),QColor(colors(theme)['control']))
            finally:dialog.close();dialog.deleteLater();APP.processEvents()

    def test_segment_icons_align_with_the_visible_text_in_both_themes(self):
        with tempfile.TemporaryDirectory() as temp,patch('app_settings._startup_runtime_allowed',return_value=False),patch.dict(os.environ,FLICKDOCK_REDUCE_MOTION='1'):
            dialog=SettingsDialog(SettingsStore(Path(temp)/'profile'));dialog.select_category('外观与交互');dialog.show();APP.processEvents()
            try:
                for theme in ('light','dark'):
                    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData(theme));APP.processEvents()
                    buttons=[b for b in dialog.findChildren(QPushButton,'settingsSegmentButton') if b.property('segmentGlyph')]
                    for button in buttons:
                        image=dialog.grab(QRect(button.mapTo(dialog,QPoint(0,0)),button.size())).toImage()
                        metrics=QFontMetricsF(button.font());size=button.iconSize();gap=6
                        x=(button.width()-(metrics.horizontalAdvance(button.text())+size.width()+gap))/2
                        bg=image.pixelColor(2,image.height()//2)
                        def ink_range(left,right):
                            ys=[]
                            for px in range(max(0,int(left)),min(image.width(),int(right))):
                                for py in range(4,image.height()-4):
                                    color=image.pixelColor(px,py)
                                    if sum(abs(getattr(color,k)()-getattr(bg,k)()) for k in ('red','green','blue'))>105:ys.append(py)
                            self.assertTrue(ys,(theme,button.text(),left,right))
                            return (min(ys)+max(ys))/2
                        icon_center=ink_range(x,x+size.width())
                        text_center=ink_range(x+size.width()+gap,button.width()-4)
                        self.assertLessEqual(abs(icon_center-text_center),1.,(theme,button.text(),icon_center,text_center))
            finally:dialog.close();dialog.deleteLater();APP.processEvents()

    def test_border_geometry_snaps_at_common_display_scales(self):
        for dpr in (1.,1.25,1.5,2.):
            image=QImage(round(80*dpr),round(50*dpr),QImage.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr);image.fill(QColor('transparent'))
            p=QPainter(image);rect,width=pixel_aligned_border(p,QRectF(0,0,80,50));transform=p.deviceTransform()
            physical_width=width*transform.m11()
            self.assertAlmostEqual(physical_width,round(physical_width))
            self.assertAlmostEqual(rect.left()*transform.m11()+transform.dx()-physical_width/2,
                                   round(rect.left()*transform.m11()+transform.dx()-physical_width/2))
            p.end()

if __name__=='__main__':unittest.main(verbosity=2)
