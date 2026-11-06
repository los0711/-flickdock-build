"""Cloud-only sync boundaries and the restored manual-transfer contract."""
import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from clipboard_sync import ClipboardSync
from shelf_sync import ShelfSync
from wan_transfer import WanTransferService,RECEIVE_CODE
from clipboard_delivery import ROUTES
from test_clipboard_sync_v1819 import Account,Clipboard,Events,Hub,APP,until

ROOT=Path(__file__).parent
V1817_BRIDGE_SHA256='da29e73a73f4992cf8cf0fc16ee82c759872b57f7c33a1388687079687d007c9'


class CloudOnlyTests(unittest.TestCase):
    def test_only_cloud_sync_is_bundled_and_no_direct_sync_hook_remains(self):
        from release_manifest import RUNTIME_SOURCES
        self.assertEqual(ROUTES,('cloud',))
        self.assertFalse(hasattr(ShelfSync,'attach_peer'))
        forbidden=('peer_sync','peer_clipboard','peer_shelf','peer_rpc','peer_stream',
                   'adaptive_stream','route_policy','nearby_transport','cloud_pipe','tls_stream')
        for name in forbidden:
            self.assertNotIn(name+'.py',RUNTIME_SOURCES)
            self.assertFalse((ROOT/(name+'.py')).exists())
        for name in ('clipboard_sync.py','shelf_sync.py','flickdock.py'):
            tree=ast.parse((ROOT/name).read_text(encoding='utf8'))
            attributes={node.attr for node in ast.walk(tree) if isinstance(node,ast.Attribute)}
            self.assertFalse({'peer_sync','peer_bridge','attach_peer'}&attributes,name)

    def test_manual_worker_is_the_exact_v1817_source(self):
        self.assertEqual(hashlib.sha256((ROOT/'p2p/bridge.mjs').read_bytes()).hexdigest(),V1817_BRIDGE_SHA256)
        self.assertTrue(RECEIVE_CODE.fullmatch('AB23'))
        for value in ('ABCD','2345','AB234567','AB2','AB2I'):
            self.assertIsNone(RECEIVE_CODE.fullmatch(value))

    def test_manual_receive_stream_is_raw_without_lan_upgrade_or_route_records(self):
        service=SimpleNamespace()
        stream=object()
        self.assertIs(WanTransferService._incoming_stream(service,stream),stream)
        self.assertFalse(hasattr(WanTransferService,'_adaptive_stream'))
        self.assertFalse(hasattr(WanTransferService,'_load_sync_ice'))

    def test_cloud_commit_does_not_claim_a_device_received_it(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            hub=Hub();events=Events();hub.events.append(events);clipboard=Clipboard()
            client=ClipboardSync(Account(hub.api('local'),'local'),events,Path(directory),clipboard=clipboard)
            results=[];client.copy_result.connect(lambda ok,context:results.append((ok,context)))
            try:
                clipboard.text('cloud backup is not native receipt')
                until(lambda:bool(results))
                self.assertTrue(results[0][0])
                self.assertTrue(results[0][1]['backed_up'])
                self.assertFalse(results[0][1]['delivered'])
                self.assertFalse(hasattr(client,'peer_bridge'))
                self.assertEqual(set(client.view['deliveries']),{'cloud'})
                self.assertTrue(any(path=='/v1/clipboard' and method=='POST' for method,path,*_ in hub.calls))
            finally:
                client.close();until(lambda:not client._jobs)


if __name__=='__main__':unittest.main()
