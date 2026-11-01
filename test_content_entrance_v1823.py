"""Real offscreen paint/layout/clicks; no native input, app launch or source I/O."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint,Qt,QVariantAnimation
from PySide6.QtGui import QColor,QPainter
from PySide6.QtWidgets import QWidget,QScrollArea,QPushButton,QLabel,QGraphicsDropShadowEffect
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,flush
from content_entrance import ContentEntrance


class ContentEntranceTests(unittest.TestCase):
    def setUp(self):
        self.root=QWidget();self.root.resize(400,320)
        self.scroll=QScrollArea(self.root);self.scroll.setGeometry(0,0,380,300)
        self.body=QWidget();self.body.resize(350,42000);self.scroll.setWidget(self.body)
        self.buttons=[]
        for index in range(1000):
            button=QPushButton(str(index),self.body);button.setGeometry(8,index*42,330,36);button.setCheckable(True)
            self.buttons.append(button)
        self.root.show();flush();self.controller=ContentEntrance(self.body)
        self.motion=patch('content_entrance.motion_allowed',return_value=True);self.motion.start()
    def tearDown(self):self.root.hide();self.root.deleteLater();flush();self.motion.stop()

    def test_only_visible_items_enter_with_one_clock_and_immediate_real_hit_targets(self):
        native=self.root.winId();positions=[button.geometry() for button in self.buttons[:8]]
        self.controller.reveal(self.buttons,self.scroll.viewport())
        self.assertGreater(self.controller.active_count,0);self.assertLessEqual(self.controller.active_count,8)
        self.assertEqual(len(self.controller.findChildren(QVariantAnimation)),1)
        self.assertLessEqual(max(entry[2] for entry in self.controller.entries.values()),96)
        button=self.buttons[0];self.assertIsNotNone(button.graphicsEffect())
        self.assertEqual(button.graphicsEffect().alpha,0)
        QTest.mouseClick(button,Qt.LeftButton,pos=button.rect().center());self.assertTrue(button.isChecked())
        self.controller.animation.setCurrentTime(80)
        self.assertGreater(button.graphicsEffect().alpha,0);self.assertLess(button.graphicsEffect().dy,6)
        self.scroll.viewport().grab()
        self.assertEqual([button.geometry() for button in self.buttons[:8]],positions)
        self.assertEqual(self.root.winId(),native)
        self.controller.animation.setCurrentTime(256);flush()
        self.assertEqual(self.controller.active_count,0);self.assertTrue(button.isChecked())
        self.assertTrue(all(button.graphicsEffect() is None for button in self.buttons))

    def test_actual_pixels_fade_and_rise_instead_of_only_changing_effect_properties(self):
        class Solid(QWidget):
            def paintEvent(self,event):
                p=QPainter(self);p.fillRect(self.rect(),QColor('red'));p.end()
        owner=QWidget();owner.resize(200,100);owner.setStyleSheet('background:white;')
        child=Solid(owner);child.setGeometry(20,20,100,50);owner.show();flush()
        controller=ContentEntrance(owner);controller.reveal([child],owner);controller.animation.pause()
        try:
            effect=child.graphicsEffect();self.assertIsNotNone(effect)
            samples=[]
            for alpha in (0.,.5,1.):
                effect.alpha=alpha;effect.dy=6;effect.update();flush()
                samples.append(owner.grab().toImage())
            self.assertEqual(samples[0].pixelColor(70,45),QColor('white'))
            middle=samples[1].pixelColor(70,45)
            self.assertEqual(middle.red(),255);self.assertTrue(125<=middle.green()<=130)
            self.assertEqual(samples[2].pixelColor(70,21),QColor('white'))
            self.assertEqual(samples[2].pixelColor(70,73),QColor('red'))
            controller.cancel();flush()
            self.assertEqual(owner.grab().toImage().pixelColor(70,21),QColor('red'))
        finally:owner.hide();owner.deleteLater();flush()

    def test_incremental_batches_share_epoch_and_ignore_stale_tokens(self):
        c=self.controller;token=c.arm(self.scroll.viewport());first=c.batch(self.buttons[:2],token=token)
        self.assertEqual(len(first),2);effect=self.buttons[0].graphicsEffect()
        c.animation.setCurrentTime(80)
        self.assertEqual(c.batch(self.buttons[:2],token=token),());self.assertIs(self.buttons[0].graphicsEffect(),effect)
        self.buttons[0].setText('metadata updated');self.assertIs(self.buttons[0].graphicsEffect(),effect)
        newer=c.arm(self.scroll.viewport());self.assertIsNone(self.buttons[0].graphicsEffect())
        self.assertEqual(c.batch(self.buttons[2:4],token=token),())
        self.assertEqual(len(c.batch(self.buttons[2:4],token=newer)),2)
        c.animation.setCurrentTime(256);flush();self.assertEqual(c.batch(self.buttons[:2],token=newer),())

    def test_same_visual_line_shares_delay_and_existing_effect_is_never_replaced(self):
        button=self.buttons[0];label=QLabel('label',self.body);label.setGeometry(0,0,5,36);label.show()
        self.controller.reveal([label,button],self.scroll.viewport())
        self.assertEqual({entry[2] for entry in self.controller.entries.values()},{0})
        self.controller.cancel();shadow=QGraphicsDropShadowEffect(button);button.setGraphicsEffect(shadow)
        self.controller.reveal([button],self.scroll.viewport())
        self.assertIs(button.graphicsEffect(),shadow);self.assertEqual(self.controller.skipped_effects,1)
        self.assertEqual(self.controller.active_count,0)

    def test_hide_scroll_and_reduced_motion_remove_temporary_effects(self):
        c=self.controller;c.reveal(self.buttons,self.scroll.viewport());self.scroll.verticalScrollBar().setValue(42)
        self.assertEqual(c.active_count,0)
        c.reveal(self.buttons,self.scroll.viewport());self.assertGreater(c.active_count,0)
        with patch('content_entrance.motion_allowed',return_value=False):c._frame(80)
        self.assertEqual(c.active_count,0)
        with patch('content_entrance.motion_allowed',return_value=False):c.reveal(self.buttons,self.scroll.viewport())
        self.assertEqual(c.active_count,0)
        c.reveal(self.buttons,self.scroll.viewport());self.root.hide();flush();self.assertEqual(c.active_count,0)

    def test_repeated_intents_release_effects_without_accumulating_objects(self):
        c=self.controller
        for _ in range(100):
            c.reveal(self.buttons[:7],self.scroll.viewport());c.animation.setCurrentTime(256)
        self.assertEqual(c.active_count,0);self.assertEqual(len(c.findChildren(QVariantAnimation)),1)
        self.assertTrue(all(button.graphicsEffect() is None for button in self.buttons))

    def test_nested_items_do_not_get_competing_parent_and_child_effects(self):
        parent=self.buttons[0];child=QLabel('child',parent);child.setGeometry(0,0,30,20);child.show()
        self.controller.reveal([parent,child],self.scroll.viewport())
        self.assertEqual(self.controller.active_count,1);self.assertIsNone(child.graphicsEffect())


if __name__=='__main__':unittest.main()
