"""Large-file recommendation removes associations, never original bytes."""
import threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from test_html_native_workspace import flush,until
import test_html_native_workspace as fixture
from flickdock import ShelfManager
from sync_files import oversized_intake,REMOTE_RECOMMEND_BYTES


class HandoffTests(unittest.TestCase):
    setUp=fixture.WorkspaceTests.setUp
    def tearDown(self):
        notice=getattr(self.manager,'remote_intake_notice',None)
        if notice:notice.hide();notice.deleteLater()
        until(lambda:not getattr(self.manager,'_large_intake_jobs',set()))
        fixture.WorkspaceTests.tearDown(self)
    def prepare(self):
        self.manager.sync=SimpleNamespace(cancel_upload=Mock(return_value=True),
            sync_status_for_ref=lambda _: 'local',sync_statuses_for_refs=lambda refs:{ref:'local' for ref in refs})
        panel=SimpleNamespace(_queued_paths=[])
        panel.queue_paths=lambda paths:panel._queued_paths.extend(str(Path(ref)) for ref in paths if Path(ref).exists())
        self.manager.wan_dialog=SimpleNamespace(panel=panel);self.manager.show_wan_dialog=Mock()
        self.manager._remote_intake=lambda paths:ShelfManager._remote_intake(self.manager,paths)
        self.manager.offer_large_intake=lambda paths:ShelfManager.offer_large_intake(self.manager,paths)
        return panel
    def test_above_200m_recommendation_moves_ref_and_cancels_cloud_without_deleting_original(self):
        self.assertEqual(REMOTE_RECOMMEND_BYTES,200*1024*1024)
        panel=self.prepare();source=self.base/'large-original.bin';source.write_bytes(b'123456789')
        exact=self.base/'at-boundary.bin';exact.write_bytes(b'12345678')
        with patch('sync_files.REMOTE_RECOMMEND_BYTES',8):
            self.assertEqual(oversized_intake([str(exact)],threading.Event()),[])
            self.shelf.import_paths('permanent',[str(source)])
            until(lambda:hasattr(self.manager,'remote_intake_notice') and self.manager.remote_intake_notice.isVisible())
        notice=self.manager.remote_intake_notice
        self.assertIn(str(source),self.shelf.files['permanent']);self.assertNotIn('小时',notice.label.text())
        notice.action.click();flush()
        self.assertNotIn(str(source),self.shelf.files['permanent']);self.assertEqual(panel._queued_paths,[str(source)])
        self.manager.sync.cancel_upload.assert_called_with(str(source),remove=True)
        self.assertEqual(source.read_bytes(),b'123456789')
    def test_copy_mode_handoff_keeps_saved_copy_and_external_original(self):
        panel=self.prepare();root=Path(self.settings.capture_dir);original=self.base/'external-original.bin';original.write_bytes(b'original unchanged')
        captured=root/'imports'/'capture-handoff'/'external-original.bin';captured.parent.mkdir(parents=True);captured.write_bytes(original.read_bytes())
        ref=str(captured);self.shelf.content.metadata[ref]=dict(capture_mode='copy',owned_root=str(root),original_name=original.name)
        self.shelf.add_paths('permanent',[ref]);self.manager._remote_intake([ref]);flush()
        self.assertNotIn(ref,self.shelf.files['permanent']);self.assertTrue(captured.exists());self.assertEqual(panel._queued_paths,[ref])
        self.assertEqual(captured.read_bytes(),original.read_bytes())
        panel._queued_paths.clear();flush()
        self.assertTrue(captured.exists());self.assertEqual(original.read_bytes(),b'original unchanged')
    def test_not_clicking_does_not_cancel_or_remove(self):
        self.prepare();source=self.base/'large.bin';source.write_bytes(b'larger payload')
        with patch('sync_files.REMOTE_RECOMMEND_BYTES',8):
            self.shelf.import_paths('permanent',[str(source)])
            until(lambda:hasattr(self.manager,'remote_intake_notice'))
        self.manager.remote_intake_notice.hide();flush()
        self.assertIn(str(source),self.shelf.files['permanent']);self.manager.sync.cancel_upload.assert_not_called()


if __name__=='__main__':unittest.main()
