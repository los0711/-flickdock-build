"""Real shelf entry points with isolated clipboards; never reads the OS clipboard."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QMimeData,QPoint,QUrl,Qt
from PySide6.QtGui import QImage,QColor
from PySide6.QtTest import QTest
import test_html_native_workspace as workspace
from test_clipboard_sync_v1819 import Account,Clipboard,Events,Hub
from clipboard_sync import ClipboardSync
from clipboard_preview import ClipboardPresenter,ClipboardPreview
from flickdock import ShelfManager


class PreviewTests(unittest.TestCase):
    def test_empty_preview_tracks_live_connections_without_copying_or_cloud_requests(self):
        from uuid import uuid4
        first,second=str(uuid4()),str(uuid4());before=list(self.hub.calls)
        self.manager.show_clipboard_preview();dialog=self.manager.clipboard_preview_dialog
        self.client.deliveries.set_connections({first:dict(route='lan'),second:dict(route='p2p')})
        workspace.flush()
        self.assertIsNone(self.model.state['value'])
        self.assertEqual(dialog.route_badges['lan'].text(),'局域网 0/1')
        self.assertEqual(dialog.route_badges['p2p'].text(),'P2P 0/1')
        self.client.deliveries.set_connections({first:dict(route='lan'),second:dict(route='disconnected')})
        workspace.flush()
        self.assertEqual(dialog.route_badges['lan'].text(),'局域网 0/1')
        self.assertEqual(dialog.route_badges['p2p'].text(),'P2P 0/0')
        self.assertEqual(self.hub.calls,before)

    def test_route_badges_show_success_over_targets_without_recopying_or_redecoding(self):
        self.clip.text('receipt metadata never changes the original clipboard')
        workspace.until(lambda:self.model.state.get('phase')=='synced')
        self.shelf.zones['permanent'].clipboard_button.click();workspace.flush()
        dialog=self.manager.clipboard_preview_dialog;before=list(self.hub.calls);revision=self.model.state['revision']
        counts={'lan':{'success':1,'total':1},'p2p':{'success':1,'total':2},'cloud':{'success':0,'total':1},'relay':{'success':0,'total':0}}
        with patch.object(self.model,'_decode') as decode:
            self.model._update(dict(self.model.state,deliveries=counts));workspace.flush();decode.assert_not_called()
        self.assertEqual(dialog.route_badges['lan'].text(),'局域网 1/1')
        self.assertEqual(dialog.route_badges['p2p'].text(),'P2P 1/2')
        self.assertEqual(dialog.route_badges['cloud'].text(),'R2 0/1')
        self.assertFalse(dialog.route_badges['relay'].isVisible())
        self.assertEqual(self.shelf.zones['permanent'].clipboard_button.accessibleDescription(),'本次内容已同步成功 2 台设备')
        self.assertEqual(self.model.state['revision'],revision);self.assertEqual(self.hub.calls,before)
        self.assertEqual(self.clip.mimeData().text(),'receipt metadata never changes the original clipboard')

    def setUp(self):
        workspace.WorkspaceTests.setUp(self)
        self.patches.enter_context(patch.object(workspace.Screen,'geometry',workspace.Screen.availableGeometry,create=True))
        self.patches.enter_context(patch.object(workspace.Screen,'devicePixelRatio',return_value=1,create=True))
        self.hub=Hub();self.clip=Clipboard();self.event=Events();self.hub.events.append(self.event)
        account=Account(self.hub.api('local'),'local')
        self.client=ClipboardSync(account,self.event,self.base/'clipboard',clipboard=self.clip)
        self.model=ClipboardPresenter(self.client)
        self.manager.clipboard_presenter=self.model;self.manager.clipboard_preview_dialog=None
        self.manager.show_clipboard_preview=lambda:ShelfManager.show_clipboard_preview(self.manager)
        for zone in self.shelf.zones.values():zone.clipboard_button.refresh()
        workspace.flush()

    def tearDown(self):
        dialog=self.manager.clipboard_preview_dialog
        if dialog is not None:dialog.close();dialog.deleteLater()
        self.model.close();self.client.close()
        workspace.until(lambda:not self.model.jobs and not self.client._jobs)
        self.model.deleteLater();workspace.flush()
        workspace.WorkspaceTests.tearDown(self)

    overview=workspace.WorkspaceTests.overview

    def test_both_buttons_preview_latest_text_and_never_copy_or_upload_again(self):
        self.clip.text('original 中文\nhttps://example.invalid')
        workspace.until(lambda:self.model.state.get('phase')=='synced')
        popup=self.overview();small=self.shelf.zones['permanent'].clipboard_button
        self.assertIs(popup.clipboard_button.presenter,small.presenter)
        # A publish acknowledgement can still schedule its normal head GET.
        # Previewing must not write/upload again, independent of that read.
        writes=lambda:[call for call in self.hub.calls if call[0]!='GET']
        before=writes();small.click();workspace.flush()
        dialog=self.manager.clipboard_preview_dialog
        self.assertTrue(dialog.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertEqual(dialog.windowModality(),Qt.NonModal)
        self.assertTrue(dialog.isVisible());self.assertIs(dialog.content.currentWidget(),dialog.text)
        self.assertEqual(dialog.text.toPlainText(),self.clip.mimeData().text())
        self.assertEqual(dialog.badge.status,'synced')
        popup.clipboard_button.click();workspace.flush()
        self.assertIs(self.manager.clipboard_preview_dialog,dialog)
        self.assertFalse(dialog.isVisible())
        popup.clipboard_button.click();workspace.flush()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(writes(),before)
        self.assertEqual(self.clip.mimeData().text(),'original 中文\nhttps://example.invalid')
        self.clip.text('new value');workspace.until(lambda:dialog.text.toPlainText()=='new value')

    def test_compact_and_overview_buttons_toggle_the_same_dialog(self):
        small=self.shelf.zones['permanent'].clipboard_button
        for _ in range(3):
            small.click();workspace.flush();dialog=self.manager.clipboard_preview_dialog
            self.assertTrue(dialog.isVisible())
            small.click();workspace.flush();self.assertFalse(dialog.isVisible())
            self.assertFalse(self.model._large_requested)
        popup=self.overview()
        for _ in range(3):
            popup.clipboard_button.click();workspace.flush();self.assertTrue(dialog.isVisible())
            popup.clipboard_button.click();workspace.flush();self.assertFalse(dialog.isVisible())
        self.assertIs(self.manager.clipboard_preview_dialog,dialog)

    def test_owner_close_and_switch_close_clipboard_preview(self):
        self.manager.show_clipboard_preview();dialog=self.manager.clipboard_preview_dialog
        self.assertTrue(dialog.isVisible())
        self.shelf.show_contents();workspace.flush();self.assertFalse(dialog.isVisible())
        self.manager.show_clipboard_preview();self.shelf.collapse_contents();workspace.flush()
        self.assertFalse(dialog.isVisible());self.assertFalse(self.model._large_requested)
        self.manager.show_clipboard_preview();self.shelf.hide();workspace.flush()
        self.assertFalse(dialog.isVisible());self.assertIsNone(dialog._window_fade.pending)

    def test_click_outside_keeps_the_preview_open(self):
        popup=self.overview()
        self.manager.show_clipboard_preview();dialog=self.manager.clipboard_preview_dialog
        QTest.mouseClick(popup.search_edit,Qt.LeftButton,pos=QPoint(4,4));workspace.flush()
        self.assertTrue(dialog.isVisible())
        dialog.close_button.click();workspace.until(lambda:not dialog.isVisible())

    def test_preview_contents_stay_open_and_explicit_close_preserves_clipboard(self):
        self.clip.text('original text')
        workspace.until(lambda:self.model.state.get('phase')=='synced')
        self.manager.show_clipboard_preview();dialog=self.manager.clipboard_preview_dialog
        targets=(dialog.title,dialog.badge,dialog.text.viewport(),dialog.status,dialog.surface,dialog)
        for target in targets:
            if not dialog.isVisible():self.manager.show_clipboard_preview()
            workspace.flush();QTest.mouseClick(target,Qt.LeftButton,pos=QPoint(2,2));workspace.flush()
            self.assertTrue(dialog.isVisible(),target.objectName())
            self.assertEqual(self.clip.mimeData().text(),'original text')
        image=QImage(80,60,QImage.Format_RGB32);image.fill(QColor('#4267a1'))
        mime=QMimeData();mime.setImageData(image);self.clip.setMimeData(mime)
        workspace.until(lambda:self.model.state.get('phase')=='synced')
        workspace.until(lambda:not self.model.large.isNull())
        QTest.mouseClick(dialog.image,Qt.LeftButton,pos=QPoint(10,10));workspace.flush()
        self.assertTrue(dialog.isVisible())
        self.clip.clear();workspace.until(lambda:self.model.state.get('value') is None)
        workspace.flush()
        QTest.mouseClick(dialog.info,Qt.LeftButton,pos=QPoint(2,2));workspace.flush()
        self.assertTrue(dialog.isVisible());self.assertIsNone(self.model.state.get('value'))
        self.assertFalse(dialog.shelf_button.isEnabled())
        dialog.close_button.click();workspace.until(lambda:not dialog.isVisible())

    def test_move_to_shelf_preserves_text_and_copies_files_outside_clipboard_cache(self):
        from pathlib import Path
        self.clip.text('保留原始文字\nhttps://example.invalid')
        workspace.until(lambda:self.model.state.get('phase')=='synced')
        self.manager.show_clipboard_preview();dialog=self.manager.clipboard_preview_dialog
        before=set(self.shelf.files['permanent']);original_mime=self.clip.mimeData();dialog.shelf_button.click()
        workspace.until(lambda:len(set(self.shelf.files['permanent'])-before)==1)
        ref=next(iter(set(self.shelf.files['permanent'])-before))
        self.assertEqual(self.shelf.content.text(ref),'保留原始文字\nhttps://example.invalid')
        self.assertIs(self.clip.mimeData(),original_mime);self.assertFalse(dialog.shelf_button.isEnabled())
        source=self.base/'clipboard-file.bin';source.write_bytes(b'original clipboard file bytes')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clip.setMimeData(mime)
        workspace.until(lambda:self.model.state.get('phase')=='synced' and dialog.shelf_button.isEnabled())
        before=set(self.shelf.files['permanent']);dialog.shelf_button.click()
        workspace.until(lambda:not dialog._moving and not self.shelf.copy_jobs)
        copied=Path(next(iter(set(self.shelf.files['permanent'])-before)))
        self.assertNotEqual(copied,source);self.assertEqual(copied.read_bytes(),source.read_bytes())
        self.assertTrue(copied.is_relative_to(self.settings.capture_dir));self.assertEqual(source.read_bytes(),b'original clipboard file bytes')
        self.assertIs(self.clip.mimeData(),mime);self.assertTrue(dialog.isVisible())

    def test_active_transfer_moves_clipboard_above_strip_and_entire_button_clicks(self):
        popup=self.overview();button=popup.clipboard_button
        before=button.mapTo(popup.middle,QPoint()).y()
        ref='flickdock:transfer:upload-one'
        self.shelf.content.pending[ref]={'name':'large.bin','kind':'file'}
        self.shelf.content.transfers[ref]={'id':'upload-one','phase':'uploading','total':500000000,'received':500}
        self.shelf.transfers_updated();workspace.flush()
        self.assertTrue(popup.transfer_strip.isVisible())
        top=button.mapTo(popup.middle,QPoint()).y()
        self.assertLess(top,before)
        self.assertLess(top+button.height(),popup.transfer_strip.mapTo(popup.middle,QPoint()).y())
        QTest.mouseClick(button,Qt.LeftButton,pos=QPoint(2,2))
        self.assertTrue(self.manager.clipboard_preview_dialog.isVisible())

    def test_remote_file_refreshes_both_open_cards_and_enlarged_preview_without_reopen(self):
        popup=self.overview();small=self.shelf.zones['permanent'].clipboard_button
        self.manager.show_clipboard_preview();dialog=self.manager.clipboard_preview_dialog
        files=list(self.shelf.files['permanent']);sender_clip=Clipboard();sender_events=Events()
        self.hub.events.append(sender_events)
        sender=ClipboardSync(Account(self.hub.api('other-device'),'other-device'),sender_events,self.base/'remote-clipboard',clipboard=sender_clip)
        try:
            image=QImage(120,80,QImage.Format_RGB32);image.fill(QColor('#2679ab'))
            path=self.base/'remote-original.png';image.save(str(path),'PNG');original=path.read_bytes()
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);sender_clip.setMimeData(mime)
            workspace.until(lambda:self.model.state.get('phase')=='synced' and self.model.state.get('value') and
                            self.model.state['value'][0]=='files' and not self.model.thumbnail.isNull() and not self.model.large.isNull())
            self.assertIs(small.presenter,self.model);self.assertIs(popup.clipboard_button.presenter,self.model)
            self.assertEqual(self.model.state['id'],self.hub.head['id']);self.assertEqual(dialog.badge.status,'synced')
            self.assertIs(dialog.content.currentWidget(),dialog.image)
            received=self.model.state['value'][1][0];self.assertEqual(__import__('pathlib').Path(received).read_bytes(),original)
            path=self.base/'remote-second.txt';path.write_text('remote original file contents',encoding='utf8')
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);sender_clip.setMimeData(mime)
            workspace.until(lambda:'remote-second.txt' in dialog.info.text() and self.model.state.get('phase')=='synced')
            self.assertTrue(self.model.thumbnail.isNull());self.assertIs(dialog.content.currentWidget(),dialog.info)
            self.assertIs(self.manager.clipboard_preview_dialog,dialog);self.assertTrue(dialog.isVisible())
            self.assertEqual(self.shelf.files['permanent'],files);self.assertTrue(self.shelf.expanded)
            self.assertEqual(sum(method=='PUT' for method,_ in self.hub.calls),2)
        finally:
            sender.close();workspace.until(lambda:not sender._jobs)

    def test_image_enlarges_without_mutating_original_and_status_does_not_redecode(self):
        image=QImage(640,480,QImage.Format_RGB32);image.fill(QColor('#4267a1'))
        path=self.base/'original.png';image.save(str(path),'PNG');original=path.read_bytes()
        mime=QMimeData();mime.setImageData(image);mime.setData('image/png',original);self.clip.setMimeData(mime)
        workspace.until(lambda:not self.model.thumbnail.isNull() and self.model.state.get('phase')=='synced')
        self.manager.show_clipboard_preview();workspace.until(lambda:not self.model.large.isNull())
        dialog=self.manager.clipboard_preview_dialog
        self.assertIs(dialog.content.currentWidget(),dialog.image)
        self.assertEqual(dialog.image.image.pixelColor(30,30),image.pixelColor(30,30))
        with patch.object(self.model,'_decode',wraps=self.model._decode) as decode:
            self.client._set_view(self.client.view['value'],'error',self.client.view['id']);workspace.flush()
            self.assertEqual(dialog.badge.status,'error');self.assertEqual(decode.call_count,0)
        self.assertEqual(bytes(self.clip.mimeData().data('image/png')),original)
        self.assertEqual(path.read_bytes(),original)

    def test_file_empty_and_repeated_enlarge_are_bounded(self):
        path=self.base/'copied.txt';path.write_text('kept original',encoding='utf8')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);self.clip.setMimeData(mime)
        workspace.until(lambda:self.model.state.get('phase')=='synced' and not self.model.jobs)
        with patch.object(self.model.pool,'start',wraps=self.model.pool.start) as start:
            for _ in range(20):self.model.request_large()
            self.assertEqual(start.call_count,1)
        self.manager.show_clipboard_preview();workspace.until(lambda:not self.model.jobs)
        dialog=self.manager.clipboard_preview_dialog
        self.assertIn('copied.txt',dialog.info.text());self.assertEqual(path.read_text(encoding='utf8'),'kept original')
        self.clip.clear();workspace.until(lambda:self.model.state.get('value') is None)
        self.assertIs(dialog.content.currentWidget(),dialog.info)
        self.assertIn('暂无',dialog.info.text())

    def test_each_failed_retry_has_error_state_but_notice_remains_deduplicated(self):
        results=[];self.client.copy_result.connect(lambda ok,context:results.append(ok))
        value=('text',{'text':'not uploaded','html':''});pending={'id':'one','feedback':{}}
        self.client._set_view(value,'uploading','one')
        self.client._copy_result(pending,False)
        self.client._set_view(value,'uploading','one')
        self.client._copy_result(pending,False)
        self.assertEqual(self.model.state['phase'],'error')
        self.assertEqual(results,[False])


if __name__=='__main__':unittest.main(verbosity=2)
