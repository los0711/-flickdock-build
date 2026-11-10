"""Dependency directions and real service ownership, with isolated offscreen Qt."""
import ast
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from sync_runtime import CloudSyncRuntime
from transfer_runtime import ManualTransferRuntime

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).parent
VIEW_MODULES = {'flickdock', 'shelf_window', 'overview_ui', 'file_preview',
                'shelf_drop_zone', 'dock_handle', 'shelf_ui_common'}
MANUAL_MODULES = {'transfer_runtime', 'lan_transfer', 'wan_transfer', 'webrtc_stream', 'lan_account'}
SYNC_MODULES = {'sync_runtime', 'shelf_sync', 'clipboard_sync', 'sync_scheduler', 'cloud_events'}


class MemoryShelf:
    def __init__(self):
        self.view = {'permanent': [], 'texts': {}, 'metadata': {}}

    def snapshot(self): return copy.deepcopy(self.view)
    def title(self, ref): return Path(ref).name
    def restore(self, view): self.view = copy.deepcopy(view)
    def cancel_pending(self): pass
    def remove_owned(self, ref): pass


def dependencies(name):
    tree = ast.parse((ROOT / (name+'.py')).read_text(encoding='utf8'))
    return {node.module.split('.')[0] for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module} | {
        alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names}


class ArchitectureTests(unittest.TestCase):
    def test_replication_and_manual_transfer_are_independent_of_views_and_each_other(self):
        for module in SYNC_MODULES:
            self.assertFalse(dependencies(module) & (VIEW_MODULES | MANUAL_MODULES), module)
        for module in MANUAL_MODULES:
            self.assertFalse(dependencies(module) & (VIEW_MODULES | SYNC_MODULES), module)
        for module in VIEW_MODULES - {'flickdock'}:
            self.assertNotIn('flickdock', dependencies(module), module)

    def test_source_delivery_includes_every_new_runtime_boundary(self):
        from release_manifest import RUNTIME_SOURCES, SOURCE_FILES
        for name in VIEW_MODULES | SYNC_MODULES | MANUAL_MODULES | {'sync_contracts', 'shelf_file_tools'}:
            self.assertIn(name+'.py', RUNTIME_SOURCES)
        self.assertIn('ARCHITECTURE.md', SOURCE_FILES)

    def test_cloud_runtime_has_one_scheduler_and_reconnect_reaches_both_registrations(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = CloudSyncRuntime(Path(directory), APP, MemoryShelf(),
                cocoa_smoke=True, clipboard_enabled=False)
            try:
                self.assertIs(runtime.shelf.scheduler, runtime.clipboard.scheduler)
                self.assertEqual(set(runtime.scheduler.registrations), {'shelf', 'clipboard'})
                runtime.events.socket.connected.emit()
                self.assertTrue(runtime.scheduler.connected)
                runtime.events.socket.disconnected.emit()
                self.assertFalse(runtime.scheduler.connected)
                self.assertFalse(runtime.scheduler.timer.isActive())
                self.assertFalse(runtime.clipboard.enabled)
            finally:
                runtime.close()
                APP.processEvents()

    def test_closing_cloud_twice_releases_each_owner_once_and_stops_late_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = CloudSyncRuntime(Path(directory), APP, MemoryShelf(),
                cocoa_smoke=True, clipboard_enabled=False)
            components = (runtime.shelf, runtime.clipboard, runtime.events, runtime.scheduler, runtime.account)
            from contextlib import ExitStack
            with ExitStack() as stack:
                calls = [stack.enter_context(patch.object(item, 'close', wraps=item.close)) for item in components]
                runtime.close()
                runtime.close()
                runtime.events.content_changed.emit()
                runtime.events.clipboard_changed.emit()
                APP.processEvents()
                self.assertTrue(all(call.call_count == 1 for call in calls))
                self.assertTrue(runtime.scheduler.closed)
                self.assertFalse(runtime.scheduler.timer.isActive())

    def test_manual_runtime_offscreen_start_and_shutdown_do_not_activate_lan_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            cloud = CloudSyncRuntime(Path(directory), APP, MemoryShelf(), cocoa_smoke=True, clipboard_enabled=False)
            manual = ManualTransferRuntime(Path(directory), APP, cloud.account, enabled=False)
            try:
                self.assertIs(manual.lan.account_authorizer, manual.authorizer)
                with patch.object(manual.lan, 'start', wraps=manual.lan.start) as lan_start, \
                     patch.object(manual.wan, 'start', wraps=manual.wan.start) as wan_start:
                    manual.start();manual.start()
                    lan_start.assert_not_called()
                    self.assertEqual(wan_start.call_count, 1)
                with patch.object(manual.lan, 'close', wraps=manual.lan.close) as lan_close, \
                     patch.object(manual.wan, 'close', wraps=manual.wan.close) as wan_close:
                    manual.close();manual.close()
                    self.assertEqual((lan_close.call_count, wan_close.call_count), (1, 1))
            finally:
                manual.close();cloud.close();APP.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
