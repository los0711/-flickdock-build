"""Actual compact artwork, cached fan motion and detail-action feedback offscreen."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint,Qt,QEvent,QPointF,QRectF
from PySide6.QtGui import QPainterPath,QEnterEvent,QMouseEvent
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,WorkspaceTests,flush
from content_stack import StackTile
from reference_widgets import MiniStack,refresh_tree
import ui_theme


def pixels(widget):
    image=widget.grab().toImage()
    return bytes(image.constBits())


def synthetic_move(widget,point):
    APP.sendEvent(widget,QMouseEvent(QEvent.MouseMove,QPointF(point),QPointF(widget.mapTo(widget.window(),point)),
        QPointF(widget.mapToGlobal(point)),Qt.NoButton,Qt.NoButton,Qt.NoModifier))


class StackSpreadTests(unittest.TestCase):
    def setUp(self):
        WorkspaceTests.setUp(self)
        # Window routing has separate integration coverage; keep the OS-like
        # pointer left by another test from changing these controlled clocks.
        self.patches.enter_context(patch.object(self.shelf,'_set_stack_spread'))
    tearDown=WorkspaceTests.tearDown
    overview=WorkspaceTests.overview

    def tile(self):
        tile=self.shelf.zones['permanent'].icon
        self.assertIsInstance(tile,MiniStack);tile.motion=True
        tile.landing.stop();tile.arrival=1
        tile.spread_animation.stop();tile._spread_requested=False;tile._spread_frame(0)
        return tile

    def test_actual_mini_stack_fans_cached_cards_without_frame_reads(self):
        tile=self.tile();closed=pixels(tile)
        keys={ref:card.cacheKey() for ref,card in tile._mini_cards.items()}
        with patch('native_ui_common.reduced_motion',return_value=False),patch('reference_widgets.thumbnail',side_effect=AssertionError('Frame requested a thumbnail')),patch('reference_widgets.item_detail',side_effect=AssertionError('Frame read file details')),patch('content_stack.stack_pixmap',side_effect=AssertionError('Frame rebuilt artwork')):
            tile.set_spread(True);tile.spread_animation.pause()
            tile.spread_animation.setCurrentTime(70);middle=pixels(tile)
            tile.spread_animation.setCurrentTime(160);opened=pixels(tile)
            self.assertNotEqual(closed,middle);self.assertNotEqual(middle,opened)
            self.assertEqual(tile.spread,1)
            self.assertEqual({ref:card.cacheKey() for ref,card in tile._mini_cards.items()},keys)
            tile.set_spread(False);tile.spread_animation.setCurrentTime(160)
            self.assertEqual(tile.spread,0);self.assertEqual(pixels(tile),closed)

    def test_reversal_keeps_presented_fraction_and_hidden_resets(self):
        tile=self.tile()
        with patch('native_ui_common.reduced_motion',return_value=False):
            tile.set_spread(True);tile.spread_animation.pause();tile.spread_animation.setCurrentTime(45)
            before=tile.spread;frame=pixels(tile)
            self.assertGreater(before,0);self.assertLess(before,1)
            tile.set_spread(False)
            self.assertEqual(tile.spread,before);self.assertEqual(pixels(tile),frame)
            tile.spread_animation.pause();tile.spread_animation.setCurrentTime(55)
            reverse=tile.spread;tile.set_spread(True);self.assertEqual(tile.spread,reverse)
            tile.hide();flush()
            self.assertEqual(tile.spread,0);self.assertFalse(tile._spread_requested)
            self.assertEqual(tile.spread_animation.state(),tile.spread_animation.State.Stopped)
            tile.show();flush();self.assertEqual(tile.spread,0)

    def test_hit_shape_excludes_blank_space_and_tracks_actual_fanned_cards(self):
        tile=self.tile();self.assertTrue(tile.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertTrue(tile.hit_stack(QPoint(120,85)))
        for point in (QPoint(1,1),QPoint(5,85),QPoint(239,169)):self.assertFalse(tile.hit_stack(point))
        folded=tile.stack_hit_path()
        with patch('native_ui_common.reduced_motion',return_value=True):tile.set_spread(True)
        opened=tile.stack_hit_path()
        newly_exposed=next((QPoint(x,y) for y in range(0,tile.height(),2) for x in range(0,tile.width(),2)
            if opened.contains(QPointF(x,y)) and not folded.contains(QPointF(x,y))),None)
        self.assertIsNotNone(newly_exposed);self.assertTrue(tile.hit_stack(newly_exposed))
        self.assertTrue(tile.stack_hit_rect().contains(QPointF(newly_exposed)))
        tile.set_stack(self.shelf.content,[])
        self.assertFalse(tile.hit_stack(QPoint(120,85)),'Retiring ghosts do not remain interactive')

    def test_maximum_five_cards_stay_inside_tile_and_parent_in_both_themes(self):
        tile=self.tile();refs=list(self.paths)
        for name in ('extra.png','extra.zip'):
            path=self.base/name;path.write_bytes(b'fixture');refs.append(str(path))
        card=QPainterPath();card.addRoundedRect(QRectF(.5,.5,191,127),16,16)
        for theme in ('light','dark'):
            ui_theme.set_theme(theme);tile.set_stack(self.shelf.content,refs);tile.landing.stop();tile.arrival=1
            self.assertEqual(len(tile._mini_cards),5)
            for value in (0,.25,.5,.75,1):
                tile._spread_frame(value)
                for ref,pose in tile._mini_frames().items():
                    bound=tile._mini_transform(ref,pose).map(card).boundingRect()
                    self.assertTrue(QRectF(tile.rect()).contains(bound),(theme,value,ref,bound,tile.rect()))
                    self.assertTrue(QRectF(tile.parentWidget().rect()).contains(bound.translated(tile.pos())),(theme,value,bound))
                pixels(tile)

    def test_async_metadata_invalidates_cached_labels_without_restarting_motion(self):
        tile=self.tile();ref=self.paths[-1]
        with patch('reference_widgets.item_detail',return_value='正在读取文件信息…'):
            tile.set_stack(self.shelf.content,self.paths)
        before=tile._mini_cards[ref].cacheKey()
        tile.landing.stop();tile.arrival=1
        with patch('reference_widgets.item_detail',return_value='42 KB'):
            self.shelf.file_metadata.ready.emit(ref)
        self.assertNotEqual(tile._mini_cards[ref].cacheKey(),before)
        self.assertEqual(tile.landing.state(),tile.landing.State.Stopped)
        self.assertEqual(tile.refs,tuple(self.paths))

    def test_category_indicator_moves_while_filter_changes_immediately(self):
        popup=self.overview();indicator=popup.category_indicator
        indicator._move();indicator.animation.setCurrentTime(160)
        start=QRectF(indicator._rect)
        with patch('native_ui_common.reduced_motion',return_value=False):
            popup.set_category('files');indicator._move()
            self.assertEqual(popup.category,'files')
            self.assertTrue(all(self.shelf.content.category(ref)=='files' for ref in popup.visible_refs()))
            self.assertEqual(indicator._rect,start)
            indicator.animation.pause();indicator.animation.setCurrentTime(65)
            current=QRectF(indicator._rect);destination=QRectF(popup.category_buttons['files'].geometry())
            self.assertGreater(current.top(),start.top());self.assertLess(current.top(),destination.top())
            popup.set_category('audio');indicator._move();self.assertEqual(indicator._rect,current)
            indicator.animation.setCurrentTime(160)
            self.assertEqual(indicator._rect,QRectF(popup.category_buttons['audio'].geometry()))
            popup.sidebar.hide();flush();self.assertEqual(indicator.animation.state(),indicator.animation.State.Stopped)
            popup.sidebar.show();flush()
        with patch('native_ui_common.reduced_motion',return_value=True):
            popup.set_category('images');indicator._move()
            self.assertEqual(indicator.animation.state(),indicator.animation.State.Stopped)
            self.assertEqual(indicator._rect,QRectF(popup.category_buttons['images'].geometry()))

    def test_empty_or_single_preserves_intent_until_a_second_file_exists(self):
        tile=self.tile()
        with patch('native_ui_common.reduced_motion',return_value=False):
            for refs in ([],self.paths[:1]):
                tile.set_stack(self.shelf.content,refs);tile.set_spread(True)
                self.assertEqual(tile.spread,0);self.assertTrue(tile._spread_requested)
                self.assertEqual(tile.spread_animation.state(),tile.spread_animation.State.Stopped)
            tile.set_stack(self.shelf.content,self.paths[:2]);tile.landing.stop();tile.arrival=1
            self.assertEqual(tile.spread_animation.state(),tile.spread_animation.State.Running)
            tile.spread_animation.setCurrentTime(160);self.assertEqual(tile.spread,1)
            tile.set_stack(self.shelf.content,self.paths[:1]);tile.removal.setCurrentTime(200);tile.spread_animation.setCurrentTime(160)
            self.assertEqual(tile.spread,0)

    def test_reduced_motion_and_colored_stack_use_the_same_api(self):
        tile=StackTile(motion=True);tile.resize(170,140);tile.show()
        try:
            with patch('native_ui_common.reduced_motion',return_value=True):
                tile.set_stack(self.shelf.content,self.paths);closed=pixels(tile)
                with patch('content_stack.stack_pixmap',side_effect=AssertionError('Uncached spread frame')):
                    tile.set_spread(True);self.assertEqual(tile.spread,1)
                    self.assertNotEqual(pixels(tile),closed)
                    self.assertEqual(tile.spread_animation.state(),tile.spread_animation.State.Stopped)
                    tile.set_spread(False);self.assertEqual(pixels(tile),closed)
        finally:tile.close();tile.deleteLater();flush()

    def test_removals_retarget_in_parallel_while_spread_is_active(self):
        tile=self.tile()
        with patch('native_ui_common.reduced_motion',return_value=False):
            tile.set_spread(True);tile.spread_animation.setCurrentTime(160)
            tile.set_stack(self.shelf.content,self.paths[:2]);self.assertEqual(tile.refs,tuple(self.paths[:2]))
            tile.removal.pause();tile.removal.setCurrentTime(65)
            before=tile._mini_frames()
            # A second remote deletion starts from this current visual pose.
            tile.set_stack(self.shelf.content,[])
            self.assertEqual(tile.refs,());self.assertEqual(tile._mini_frames(),before)
            with patch('reference_widgets.thumbnail',side_effect=AssertionError('Deletion decoded thumbnail')),patch('reference_widgets.item_detail',side_effect=AssertionError('Deletion read file')):
                tile.removal.setCurrentTime(100);pixels(tile)
                tile.removal.setCurrentTime(200);pixels(tile)
            self.assertFalse(tile._mini_exit_cards);self.assertFalse(tile._mini_cards)

    def test_detail_open_action_has_hover_and_pressed_pixels_and_full_hit_area(self):
        popup=self.overview();control=popup.preview.open_button
        for theme in ('dark','light'):
            ui_theme.set_theme(theme);refresh_tree(popup);flush()
            APP.sendEvent(control,QEvent(QEvent.Leave));synthetic_move(popup.header,QPoint(1,1));flush()
            control.setAttribute(Qt.WA_UnderMouse,False)
            normal=control.grab().toImage();ratio=normal.devicePixelRatio();point=QPoint(round(6*ratio),round(control.height()/2*ratio))
            normal_color=normal.pixelColor(point).name()
            hover=QPoint(20,control.height()//2)
            # Supply the widget-local crossing state and valid event payload;
            # never move the user's cursor to exercise the real QSS paint.
            control.setAttribute(Qt.WA_UnderMouse,True)
            APP.sendEvent(control,QEnterEvent(QPointF(hover),QPointF(control.mapTo(control.window(),hover)),QPointF(control.mapToGlobal(hover))))
            synthetic_move(control,hover);flush()
            hover_color=control.grab().toImage().pixelColor(point).name()
            with patch.object(self.shelf,'open_path') as opened:
                QTest.mousePress(control,Qt.LeftButton,pos=QPoint(2,control.height()//2));flush()
                pressed_color=control.grab().toImage().pixelColor(point).name()
                QTest.mouseRelease(control,Qt.LeftButton,pos=QPoint(2,control.height()//2));flush()
                opened.assert_called_once_with(popup.preview.current_path)
            self.assertEqual(len({normal_color,hover_color,pressed_color}),3,(theme,normal_color,hover_color,pressed_color))
            APP.sendEvent(control,QEvent(QEvent.Leave))
            control.setAttribute(Qt.WA_UnderMouse,False)
        synthetic_move(popup.header,QPoint(1,1));flush()


if __name__=='__main__':unittest.main()
