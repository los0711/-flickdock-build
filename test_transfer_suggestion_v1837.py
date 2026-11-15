"""Large-file and full-cloud suggestions use actionable copy-style capsules."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
import test_html_native_workspace as w
import ui_theme,shelf_ui_common
from intake_notice import CapsuleNotice
from shelf_capacity import CloudCapacityNotice
from shelf_intake_limit import make_notice
from video_prompt import PROMPT_DURATION_MS


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        import theme_accent
        self._original_accent=theme_accent.current();theme_accent.set_color('#087CEF')
        self.fixture=w.WorkspaceTests();self.fixture.setUp()
        self.manager=self.fixture.manager;self.paths=self.fixture.paths[:2];self.actions=[]
        self.manager._remote_intake=lambda paths:self.actions.append(('remote',paths))
        self.manager.show_transfer_dialog=lambda:self.actions.append(('open',None))
        self.manager.lan_dialog=SimpleNamespace(panel=SimpleNamespace(queue_paths=lambda paths:self.actions.append(('queue',paths))))

    def tearDown(self):
        import theme_accent
        theme_accent.set_color(self._original_accent);self.fixture.tearDown()

    def test_both_suggestions_share_black_capsule_and_keep_transfer_action(self):
        for theme in ('light','dark'):
            self.fixture.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(w.APP,shelf_ui_common.FONT_FAMILY)
            for kind in ('large','capacity'):
                notice=make_notice(self.manager) if kind=='large' else CloudCapacityNotice(self.manager)
                try:
                    with patch('intake_notice.motion_allowed',return_value=False):
                        notice.show_paths([*self.paths,self.paths[0]]);w.flush()
                        self.assertIsInstance(notice,CapsuleNotice);self.assertTrue(notice.isVisible())
                        self.assertEqual(notice.paths,self.paths);self.assertIn('文件互传',notice.accessibleName())
                        self.assertEqual(notice.timer.interval(),PROMPT_DURATION_MS)
                        self.assertNotIn('远程传输',notice.accessibleName());self.assertFalse(notice._art.isNull())
                        self.assertIn('点击',notice.accessibleDescription())
                        with patch.object(notice,'_begin_presentation',wraps=notice._begin_presentation) as begin:
                            notice.show_paths(self.paths);self.assertEqual(begin.call_count,0)
                            self.assertEqual(notice.timer.interval(),PROMPT_DURATION_MS)
                        center=notice.capsule_rect().center().toPoint();image=notice.grab().toImage()
                        self.assertEqual(image.pixelColor(center.x(),center.y()-15),QColor('#18181B'))
                        area=notice.capsule_rect().toAlignedRect()
                        self.assertTrue(any(image.pixelColor(x,y).blue()>180 and image.pixelColor(x,y).red()<100
                            for x in range(area.right()-38,area.right()-8) for y in range(center.y()-12,center.y()+12)))
                        self.actions.clear();QTest.mouseClick(notice,Qt.LeftButton,pos=center);w.flush()
                        self.assertEqual(self.actions,[('open',None),('queue',self.paths)])
                        self.assertTrue(all(__import__('pathlib').Path(path).exists() for path in self.paths))
                        self.assertTrue(all(path in self.fixture.shelf.files['permanent'] for path in self.paths))
                finally:notice.supersede();notice.deleteLater();w.flush()


if __name__=='__main__':unittest.main(verbosity=2)
