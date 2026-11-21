"""Upload thumbnails share the local dot without hiding transfer progress elsewhere."""
import hashlib,unittest
from unittest.mock import patch
from PySide6.QtCore import QEvent,QPoint,QRect,QRectF,Qt
from PySide6.QtGui import QColor,QImage,QPainter,QPixmap
import test_html_native_workspace as w
import ui_theme,shelf_ui_common
from transfer_visuals import draw_cloud_status_badge,thumbnail_transfer_progress_visible
from native_ui_common import colors


def digest(image):
    converted=image.convertToFormat(QImage.Format_ARGB32)
    return hashlib.sha256(bytes(converted.constBits())).hexdigest()


def badge(status,state=None):
    result=QImage(32,32,QImage.Format_ARGB32_Premultiplied);result.fill(Qt.transparent)
    painter=QPainter(result);draw_cloud_status_badge(painter,QRectF(6,6,20,20),status,state);painter.end()
    return result


class UploadBadgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=w.WorkspaceTests();self.fixture.setUp()
        self.shelf=self.fixture.shelf;self.ref=self.fixture.paths[0]
        self.status={'value':'local'}
        self.fixture.manager.sync=w.SyncView(sync_status_for_ref=lambda _:self.status['value'],
            sync_statuses_for_refs=lambda refs:{ref:self.status['value'] for ref in refs},
            is_upload_paused=lambda _:False)

    def tearDown(self):
        self.shelf.image_cache.close();self.shelf.image_cache._pool.waitForDone(5000)
        self.fixture.tearDown()

    def set_phase(self,phase,status):
        self.status['value']=status
        if phase is None:self.shelf.content.transfers.pop(self.ref,None)
        else:self.shelf.content.transfers[self.ref]=dict(id='sample',phase=phase,remote=False,received=42,total=100)

    def test_presence_dot_matches_local_for_queued_and_uploading_in_both_themes(self):
        for theme in ('dark','light'):
            ui_theme.set_theme(theme);local=digest(badge('local'))
            for status,phase in (('pending',None),('uploading',None),('pending','queued'),
                                 ('pending','preparing'),('uploading','uploading'),('uploading','verifying')):
                state=dict(phase=phase,remote=False,total=100,received=42) if phase else None
                self.assertEqual(digest(badge(status,state)),local,(theme,status,phase))
            image=badge('synced');self.assertNotEqual(digest(image),local)
            self.assertEqual(image.pixelColor(16,16),QColor('#19B372'))
            remote=badge('pending',dict(phase='downloading',remote=True,total=100,received=42))
            self.assertEqual(digest(remote),local)
            for status in ('error','paused','pending','uploading'):self.assertEqual(digest(badge(status)),local)

    def test_real_list_grid_and_mini_thumbnails_use_identical_local_artwork(self):
        popup=self.fixture.overview()
        for theme in ('dark','light'):
            self.fixture.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(w.APP,shelf_ui_common.FONT_FAMILY)
            self.shelf._tree_theme=None;self.shelf.refresh()
            for mode in ('list','icons'):
                popup.change_view_mode(mode);popup.resume_content()
                w.until(lambda:popup._build_cursor==len(popup.visible_refs()) and popup.retirement.is_idle)
                item=popup._item_widgets[self.ref]
                target=item.icon_label if mode=='list' else item
                shots=[]
                for phase,status in ((None,'local'),('queued','pending'),('uploading','uploading')):
                    self.set_phase(phase,status);popup.refresh_cloud_status();target.update();w.flush()
                    shots.append(digest(target.grab().toImage()))
                    self.assertEqual(item.toolTip(),'仅保存在本机')
                self.assertEqual(len(set(shots)),1,(theme,mode,'upload art differs from local'))
            tile=self.shelf.zones['permanent'].icon
            self.set_phase(None,'local')
            w.until(lambda:self.shelf.file_metadata.request(self.ref) is not None)
            tile.set_stack(self.shelf.content,[self.ref],self.shelf.preview_cache);w.flush();tile._cache_cards()
            shots=[]
            for phase,status in ((None,'local'),('queued','pending'),('uploading','uploading')):
                self.set_phase(phase,status);w.flush()
                result=QPixmap(192,128);result.fill(Qt.transparent);painter=QPainter(result)
                tile.paint_card(painter,self.ref,True);painter.end();shots.append(digest(result.toImage()))
            self.assertEqual(len(set(shots)),1,(theme,'mini','upload art differs from local'))
            image=result.toImage()
            blues=[(x,y) for x in range(38,60) for y in range(38,60)
                   if image.pixelColor(x,y).blue()>150 and image.pixelColor(x,y).red()<80]
            self.assertTrue(blues)
            self.assertLessEqual(max(x for x,_ in blues),56)
            self.assertLessEqual(max(y for _,y in blues),56)

    def test_footer_background_tracks_progress_and_keeps_percentage_on_the_right(self):
        popup=self.fixture.overview();popup.set_category('all')
        for theme in ('dark','light'):
            self.fixture.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(w.APP,shelf_ui_common.FONT_FAMILY)
            self.shelf._tree_theme=None;self.shelf.refresh()
            for percent in (0,42,100):
                self.shelf.content.transfers[self.ref]=dict(phase='uploading',received=percent,total=100)
                popup.update_transfer_status();w.flush();strip=popup.transfer_strip
                self.assertTrue(strip.isVisible());self.assertAlmostEqual(strip.progress_fraction,percent/100)
                row,name,label,_=popup.strip_rows[0]
                self.assertEqual(label.text(),f'{percent}%');self.assertLess(name.geometry().right(),label.geometry().left())
                self.assertLessEqual(abs(name.geometry().center().y()-label.geometry().center().y()),1)
                image=strip.grab().toImage();ratio=image.devicePixelRatio()
                sample=lambda x:image.pixelColor(round(strip.width()*x*ratio),round(5*ratio)).name().lower()
                self.assertEqual(sample(.2),colors(theme)['transfer_progress_fill'].lower() if percent>=42 else colors(theme)['control'].lower())
                self.assertEqual(sample(.8),colors(theme)['transfer_progress_fill'].lower() if percent==100 else colors(theme)['control'].lower())
            self.shelf.content.transfers[self.ref]=dict(phase='uploading',received=0,total=0)
            popup.update_transfer_status();w.flush()
            self.assertIsNone(popup.transfer_strip.progress_fraction)
            self.assertFalse(popup.strip_rows[0][2].isVisible())

    def test_transfer_strip_remains_visible_in_transfer_category(self):
        popup=self.fixture.overview()
        self.shelf.content.transfers[self.ref]=dict(id='sample',phase='uploading',received=42,total=100)
        popup.update_transfer_status();w.flush()
        popup.set_category('uploading');w.flush()
        self.assertTrue(popup.transfer_strip.isVisible())
        self.assertEqual(popup.strip_rows[0][2].text(),'42%')
        popup.strip_rows[0][1].click();w.flush()
        self.assertEqual(popup.category,'uploading');self.assertTrue(popup.transfer_strip.isVisible())

    def test_unchecked_grid_border_is_quieter_than_hover_in_both_themes(self):
        for theme in ('dark','light'):
            c=colors(theme);background=QColor(c['file_card'])
            self.assertLess(w.contrast_colors(QColor(c['grid_unchecked_border']),background),
                            w.contrast_colors(QColor(c['grid_unchecked_hover_border']),background))

    def test_frequent_upload_updates_keep_controls_clickable_and_coalesce_rebuilds(self):
        popup=self.fixture.overview();zone=self.shelf.zones['permanent']
        state=dict(id='large',phase='uploading',received=0,total=347*1024*1024)
        self.shelf.content.transfers[self.ref]=state
        with patch.object(zone,'refresh',wraps=zone.refresh) as refresh:
            self.shelf.transfers_updated();initial=refresh.call_count
            for index in range(100):
                state['received']=index*state['total']//100
                self.shelf.transfers_updated()
            self.assertEqual(refresh.call_count,initial,'byte progress rebuilt the mini shelf')
            popup.category_buttons['video'].click();w.flush()
            self.assertEqual(popup.category,'video')
            popup.category_buttons['all'].click();w.flush()
            self.assertEqual(popup.category,'all')
            w.until(lambda:popup.strip_rows[0][2].text()=='99%')
            self.assertEqual(refresh.call_count,initial)
        self.shelf.content.transfers.clear();self.shelf.transfers_updated()

    def test_send_separator_is_short_and_subtle_in_both_themes(self):
        popup=self.fixture.overview();popup.select_item(self.ref);preview=popup.preview
        for theme in ('dark','light'):
            self.fixture.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(w.APP,shelf_ui_common.FONT_FAMILY)
            self.shelf._tree_theme=None;self.shelf.refresh();w.APP.sendEvent(preview.send_menu_button,QEvent(QEvent.Leave));w.flush()
            frame=preview.send_control;image=frame.grab().toImage();ratio=image.devicePixelRatio()
            x=preview.send_menu_button.x()
            pixel=lambda dx,y:image.pixelColor(round((x+dx)*ratio),round(y*ratio))
            for y in (4,frame.height()-5):
                self.assertLessEqual(abs(pixel(0,y).lightness()-pixel(4,y).lightness()),2,(theme,y))
            difference=abs(pixel(0,frame.height()//2).lightness()-pixel(4,frame.height()//2).lightness())
            self.assertGreater(difference,2,theme);self.assertLess(difference,45,theme)

    def test_header_stays_fixed_during_expand_collapse_and_reversal(self):
        self.shelf.manager.motion=True;self.shelf.geometry_morph.motion=True
        with patch('native_ui_common.reduced_motion',return_value=False):
            rect=QRect(self.shelf.visual_geometry());rect.moveTo(1400,60);self.shelf.geometry_morph.jump(rect)
            controls=(self.shelf.pin_button,self.shelf.expand_button,self.shelf.menu_button,self.shelf.close_button)
            positions=lambda:[button.mapToGlobal(QPoint()) for button in controls]
            original=positions()
            self.assertEqual(self.shelf._header_offset_px,8)
            self.shelf.show_contents();motion=self.shelf.geometry_morph;motion.pause()
            for frame in (.1,.3,.5,.75,.95):
                motion._frame(frame);self.assertEqual(positions(),original)
            self.shelf.collapse_contents();motion.pause()
            for frame in (.2,.6,.9):motion._frame(frame);self.assertEqual(positions(),original)
            motion.complete();self.assertEqual(self.shelf._header_offset_px,8);self.assertEqual(positions(),original)

    def test_other_transfers_keep_progress_indicators(self):
        for phase in ('copying','downloading','remote','retrying'):
            self.assertTrue(thumbnail_transfer_progress_visible(dict(phase=phase,remote=phase!='copying')))
        for phase in ('queued','preparing','validating','uploading','verifying'):
            self.assertFalse(thumbnail_transfer_progress_visible(dict(phase=phase,remote=False)))
        self.assertTrue(thumbnail_transfer_progress_visible(dict(phase='preparing',remote=True)))


if __name__=='__main__':unittest.main()
