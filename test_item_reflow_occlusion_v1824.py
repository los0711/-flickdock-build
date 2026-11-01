"""Real incremental shelf painting, disposable originals; no native input."""
import time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRect
from PySide6.QtGui import QPalette
import ui_theme
from reference_widgets import refresh_tree,palette
from sync_adapter import ShelfAdapter
import test_html_native_workspace as ws


class ReflowOcclusionTests(unittest.TestCase):
    def setUp(self):
        self.f=ws.WorkspaceTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.clock=[0.]
        clock_patch=patch('item_transitions.time',SimpleNamespace(perf_counter=lambda:self.clock[0]))
        clock_patch.start();self.addCleanup(clock_patch.stop)
        shared=patch('native_ui_common.reduced_motion',return_value=False)
        shared.start();self.addCleanup(shared.stop)
        for index in range(3):
            path=self.f.base/f'kept-{index}.txt';path.write_text('unchanged original')
            self.f.shelf.add_paths('permanent',[str(path)])
        self.adapter=ShelfAdapter(self.f.manager);self.base=self.adapter.snapshot()
        self.peer=self.f.base/'peer-new.txt';self.peer.write_text('new original')
        self.popup=self.f.overview()

    def reset(self,mode):
        self.f.manager.motion=False;self.adapter.restore(self.base)
        self.f.settings.update(view_mode=mode);self.popup._render_signature=None;self.popup.rebuild()
        ws.until(lambda:self.popup._build_cursor==len(self.popup.visible_refs()))
        self.popup.item_transitions.finish();self.f.manager.motion=True

    def add_peer(self):
        view=self.adapter.snapshot();view['permanent'].append(str(self.peer));self.adapter.restore(view)
        ws.until(lambda:self.popup._build_cursor==len(self.popup.visible_refs()))
        ws.flush();return self.popup.item_transitions

    def title_rect(self,widget):
        if hasattr(widget,'name_label'):
            label=widget.name_label;return QRect(label.mapTo(self.popup.rows_body,QPoint()),label.size())
        return QRect(widget.x()+12,widget.y()+118,widget.width()-24,20)

    def rename_metadata(self,ref):
        view=self.adapter.snapshot();view['metadata'].setdefault(ref,{})['title']='WWWW underlying title changed'
        self.adapter.restore(view);ws.flush()

    def test_new_front_card_never_shows_underlying_live_title_during_real_add(self):
        for mode in ('icons','list'):
            with self.subTest(mode=mode):
                self.reset(mode);old_first=self.popup.visible_refs()[0]
                old=self.popup._item_widgets[old_first];origin=QPoint(old.pos())
                motion=self.add_peer();front=self.popup._item_widgets[str(self.peer)]
                self.assertEqual(front.pos(),origin);self.assertEqual(old.pos(),origin)
                self.assertFalse(motion.ghosts);self.assertTrue(motion.moves)
                rect=self.title_rect(front);before=self.popup.rows_body.grab(rect).toImage();generation=motion.generation
                self.rename_metadata(old_first)
                self.assertEqual(motion.generation,generation)
                self.assertEqual(self.popup.rows_body.grab(rect).toImage(),before)
                self.assertEqual(self.peer.read_text(),'new original')

    def test_crossing_live_survivor_does_not_transmit_lower_title_during_remove(self):
        self.reset('icons');motion=self.add_peer()
        self.clock[0]=.25;motion._tick();ws.flush()
        view=self.adapter.snapshot();view['permanent'].remove(str(self.peer));self.clock[0]=1.
        self.adapter.restore(view);ws.flush();self.clock[0]=1.065;motion._tick();ws.flush()
        self.assertTrue(motion.moves);self.assertEqual(len(motion.ghosts),1)
        widgets=self.popup._item_widgets;stack=list(self.popup.rows_body.children());sample=None
        inset=palette()['card_radius']+1
        for lower_ref,lower in widgets.items():
            for upper in widgets.values():
                if upper is lower or stack.index(upper)<stack.index(lower):continue
                # Rounded AA edge pixels intentionally remain translucent;
                # test the fully covered interior, across platform radii.
                rect=self.title_rect(lower).intersected(upper.geometry().adjusted(inset,inset,-inset,-inset))
                if rect.width()>12 and rect.height()>4:sample=(lower_ref,rect);break
            if sample:break
        self.assertIsNotNone(sample,'Fixture must expose a genuinely covered lower title')
        ref,rect=sample;before=self.popup.rows_body.grab(rect).toImage();generation=motion.generation
        self.rename_metadata(ref)
        self.assertEqual(motion.generation,generation)
        self.assertEqual(self.popup.rows_body.grab(rect).toImage(),before)

    def test_backing_preserves_real_theme_hover_selection_and_restores_endpoint(self):
        for mode in ('icons','list'):
            self.reset(mode);widget=next(iter(self.popup._item_widgets.values()));motion=self.popup.item_transitions
            for theme in ('dark','light'):
                self.f.settings.update(theme=theme);ui_theme.set_theme(theme)
                ui_theme.apply_application(ws.APP,ws.subject.FONT_FAMILY);refresh_tree(self.f.shelf);ws.flush()
                for selected in (False,True):
                    for hover in (0.,1.):
                        self.popup.selected={widget.path} if selected else set()
                        widget.motion.values['hover' if mode=='icons' else 'highlight']=hover
                        widget.update();ws.flush();before=self.popup.rows_body.grab().toImage()
                        auto=widget.autoFillBackground();mask=widget.palette().resolveMask()
                        motion._back_card(widget)
                        self.assertEqual(self.popup.rows_body.grab().toImage(),before,(mode,theme,selected,hover))
                        motion.finish()
                        self.assertEqual(widget.autoFillBackground(),auto)
                        self.assertEqual(widget.palette().resolveMask(),mask)
                        self.assertEqual(self.popup.rows_body.grab().toImage(),before)

    def test_finish_and_host_hide_release_backings_without_late_reopen(self):
        self.reset('icons');motion=self.add_peer();self.assertTrue(motion._backings)
        self.clock[0]=.25;motion._tick();ws.flush()
        self.assertFalse(motion._backings);self.assertFalse(motion.timer.isActive())
        view=self.adapter.snapshot();view['permanent'].remove(str(self.peer));self.clock[0]=1.
        self.adapter.restore(view);self.popup.middle.hide();ws.flush()
        self.assertFalse(motion._backings);self.assertFalse(motion.moves);self.assertFalse(motion.ghosts)
        self.assertFalse(motion.timer.isActive())
        self.assertTrue(all(not w.autoFillBackground() for w in self.popup._item_widgets.values()))

    def test_late_fifth_card_joins_running_reflow_without_restarting_it(self):
        self.reset('icons');p=self.popup;view=self.adapter.snapshot();original_first=p.visible_refs()[0]
        p.select_item(original_first);selected=set(p.selected);scroll=p.scroll.verticalScrollBar().value()
        additions=[]
        for index in range(5):
            path=self.f.base/f'bulk-{index}.txt';path.write_text('original bulk file');additions.append(str(path))
        view['permanent'].extend(additions)
        bounded=SimpleNamespace(perf_counter=lambda:0.,monotonic=time.monotonic,time=time.time,sleep=time.sleep)
        with patch.object(ws.subject,'time',bounded),patch.object(p._build_timer,'start'):
            self.adapter.restore(view);ws.flush();motion=p.item_transitions
            built=set(p._item_widgets)&set(additions)
            self.assertEqual(len(built),4);self.assertTrue(motion.moves)
            generation=motion.generation;started={ref:row[3] for ref,row in motion.moves.items()}
            p._append_batch();ws.flush()
        late_ref=next(iter(set(additions)-built));late=p._item_widgets[late_ref]
        self.assertEqual(motion.generation,generation)
        self.assertEqual({ref:row[3] for ref,row in motion.moves.items()},started)
        self.clock[0]=.09;motion._tick();ws.flush();inset=palette()['card_radius']+1
        sample=None
        for ref,row in motion.moves.items():
            rect=self.title_rect(row[0]).intersected(late.geometry().adjusted(inset,inset,-inset,-inset))
            if rect.width()>12 and rect.height()>4:sample=(ref,rect);break
        self.assertIsNotNone(sample,'Late card must genuinely cover a moving lower title')
        ref,rect=sample;before=p.rows_body.grab(rect).toImage();self.rename_metadata(ref)
        self.assertEqual(p.rows_body.grab(rect).toImage(),before)
        self.assertEqual(p.selected,selected);self.assertEqual(p.scroll.verticalScrollBar().value(),scroll)
        self.assertTrue(late.property('_flickdock_reflow_backing'))
        motion.finish();self.assertFalse(motion._backings)
        self.assertTrue(all(not w.property('_flickdock_reflow_backing') for w in p._item_widgets.values()))
        self.assertTrue(all(Path(ref).read_text()=='original bulk file' for ref in additions))

    def test_queued_old_tick_cannot_finish_a_new_pending_reflow(self):
        self.reset('icons');motion=self.add_peer();self.assertTrue(motion.moves)
        path=self.f.base/'second-peer.txt';path.write_text('second original')
        view=self.adapter.snapshot();view['permanent'].append(str(path));self.adapter.restore(view)
        self.assertTrue(motion.layout_pending);generation=motion.generation
        self.assertIn(str(path),self.popup.visible_refs())
        # A timeout can already have been posted before capture stops its clock.
        motion._tick()
        self.assertEqual(motion.generation,generation);self.assertTrue(motion.layout_pending)
        ws.flush();self.assertFalse(motion.layout_pending)
        self.assertEqual(motion.generation,generation);self.assertTrue(motion.moves)
        self.assertTrue(motion.timer.isActive())
        self.clock[0]=.25;motion._tick()
        self.assertFalse(motion.moves);self.assertFalse(motion._backings);self.assertFalse(motion.timer.isActive())
        self.assertEqual(path.read_text(),'second original')

    def test_pending_reflow_keeps_existing_exit_pixels_until_new_begin(self):
        self.reset('icons');motion=self.add_peer();self.clock[0]=.25;motion._tick();ws.flush()
        view=self.adapter.snapshot();view['permanent'].remove(str(self.peer));self.clock[0]=1.
        self.adapter.restore(view);ws.flush();self.assertEqual(len(motion.ghosts),1)
        keys=[row[0].cacheKey() for row in motion.ghosts]
        path=self.f.base/'peer-during-exit.txt';path.write_text('original during exit')
        other=self.f.base/'second-during-exit.txt';other.write_text('second original during exit')
        view=self.adapter.snapshot();view['permanent'].extend((str(path),str(other)));self.clock[0]=1.04
        self.adapter.restore(view);self.assertTrue(motion.layout_pending);generation=motion.generation
        motion._tick()
        self.assertEqual(motion.generation,generation);self.assertTrue(motion.layout_pending)
        self.assertEqual([row[0].cacheKey() for row in motion.ghosts],keys)
        ws.flush();self.assertTrue(motion.moves);self.assertTrue(motion.timer.isActive())
        self.assertEqual([row[0].cacheKey() for row in motion.ghosts],keys)
        self.clock[0]=1.3;motion._tick();ws.flush()
        self.assertFalse(motion.ghosts);self.assertFalse(motion.moves);self.assertFalse(motion._backings)
        self.assertFalse(motion.timer.isActive());self.assertFalse(motion.overlay.isVisible())
        self.assertEqual(path.read_text(),'original during exit')
        self.assertEqual(other.read_text(),'second original during exit')


if __name__=='__main__':unittest.main(verbosity=2)
