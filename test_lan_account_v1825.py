"""Real loopback TLS payload and authenticated native mesh control, no desktop."""
import hashlib,os,threading,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock
from lan_transfer import LanTransferService,_Transfer
from lan_account import LanAccountAuthorizer,manifest_digest
from lan_panel import LanDialog
from app_settings import SettingsStore
from peer_sync import PeerSync
from test_peer_sync_v1824 import Account,Hub,Wan
from test_html_native_workspace import APP,flush,until


class AccountWan(Wan):
    def peers(self):
        rows=super().peers()
        for row in rows:row['lan_id']=next(wan.lan_id for wan in self.hub.wans if wan.device_id==row['id'])
        return rows


class LanAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.lans=[];self.meshes=[]
        self.addCleanup(self.cleanup);self.hub=Hub(self.root);group=str(uuid4())
        for index in range(2):
            lan=LanTransferService(device_name='LAN QA',bind_host='127.0.0.1',discovery=False);self.lans.append(lan);lan.start()
            wan=AccountWan(self.hub,Account(group),index+1);wan.lan_id=lan.device_id;self.hub.wans.append(wan)
        until(lambda:all(lan.port for lan in self.lans))
        for wan in self.hub.wans:self.meshes.append(PeerSync(wan.account,wan))
        for wan in self.hub.wans:wan._connected=True;wan.peers_changed.emit(wan.peers())
        until(lambda:all(len(mesh.available())==1 for mesh in self.meshes),seconds=15)
        self.auth=[LanAccountAuthorizer(lan,mesh) for lan,mesh in zip(self.lans,self.meshes)]
        self.lans[0].remember_peer(self.lans[1].own_peer());self.lans[1].remember_peer(self.lans[0].own_peer())

    def cleanup(self):
        for lan in self.lans:lan.close()
        for mesh in self.meshes:mesh.close()
        for lan in self.lans:
            for worker in lan._threads:worker.join(2)
        for mesh in self.meshes:mesh.deleteLater()
        flush();self.temp.cleanup()

    def test_same_account_send_stays_in_lan_panel_and_delivers_original_via_tls(self):
        source=self.root/'original.bin';body=os.urandom(1024*1024+19);source.write_bytes(body)
        destination=self.root/'received';destination.mkdir();offers=[];completed=[]
        def accept(offer):
            offers.append(offer)
            if offer.get('account_device'):self.lans[1].accept(offer['id'],destination)
        self.lans[1].offer_received.connect(accept);self.lans[1].completed.connect(completed.append)
        store=SettingsStore(self.root/'profile');dialog=LanDialog(self.lans[0],store);modes=[]
        dialog.mode_selected.connect(modes.append)
        try:
            dialog.panel.queue_paths([source]);dialog.panel.send_button.click()
            until(lambda:bool(completed),seconds=12)
            self.assertEqual(modes,[]);self.assertFalse(dialog._wan);self.assertIs(dialog.panel.service,self.lans[0])
            self.assertEqual(dialog.panel._queued_paths,[]);self.assertTrue(offers[0]['account_device'])
            self.assertIsNone(self.lans[0]._transfers[completed[0]['id']].socket)
            self.assertEqual(hashlib.sha256(Path(completed[0]['paths'][0]).read_bytes()).digest(),hashlib.sha256(body).digest())
        finally:dialog.close();dialog.deleteLater();flush()

    def test_unavailable_account_auth_fails_without_switching_mode_or_sending_bytes(self):
        source=self.root/'cannot-auto-send.txt';source.write_text('original');states=[];offers=[]
        self.lans[0].progress.connect(states.append);self.lans[1].offer_received.connect(offers.append)
        self.meshes[0].send_control=Mock(return_value=False)
        identifier=self.lans[0].send(self.lans[1].device_id,[source])
        until(lambda:any(row['state']=='error' for row in states),seconds=4)
        self.assertFalse(offers);self.assertEqual(self.lans[0]._transfers[identifier].done,0)
        self.assertIn('不会改用远程',next(row['message'] for row in states if row['state']=='error'))

    def test_ticket_binds_recipient_certificate_manifest_token_and_session(self):
        receiver=self.auth[1];sender=self.hub.wans[0].device_id
        entries=[dict(path='file.txt',kind='file',size=5)]
        ticket=dict(type='lan-transfer',action='prepare',id=uuid4().hex,token='a'*64,digest=manifest_digest(entries),fingerprint='b'*64)
        receiver._receive(sender,ticket);self.assertNotIn(ticket['id'],receiver._tickets)
        ticket['fingerprint']=self.lans[1].fingerprint;receiver._receive(sender,ticket)
        transfer=_Transfer(ticket['id'],'receive','Untrusted',token=ticket['token'],manifest_hash=ticket['digest'])
        self.assertTrue(receiver.incoming(transfer)['account_device'])
        transfer.token='c'*64;self.assertIsNone(receiver.incoming(transfer));transfer.token=ticket['token']
        transfer.manifest_hash='d'*64;self.assertIsNone(receiver.incoming(transfer));transfer.manifest_hash=ticket['digest']
        self.meshes[1].account.signed_in=False;self.assertIsNone(receiver.incoming(transfer))


if __name__=='__main__':unittest.main()
