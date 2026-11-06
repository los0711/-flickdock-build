"""Real loopback TLS payload and account consent metadata, no desktop."""
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
from PySide6.QtCore import QObject,Signal
from test_html_native_workspace import APP,flush,until


class Account(QObject):
    account_changed=Signal(object)
    def __init__(self,group):
        super().__init__();self.account={'id':group};self.device_id=str(uuid4());self.signed_in=True
        self.api=SimpleNamespace(token=uuid4().hex)


class AccountWan(QObject):
    peers_changed=Signal(list);lan_authorization=Signal(dict)
    def __init__(self,group,lan,index,peers):
        super().__init__();self.account_service=Account(group);self.lan_id=lan.device_id
        self.device_id=str(index)*64;self._closing=False;self._connected=True;self.hub=peers
        self._bound_identity=self._account_identity()
    def _account_identity(self):return (self.account_service.device_id,self.account_service.api.token)
    def peers(self):
        return [dict(id=w.device_id,name='Account LAN QA',online=True,lan_id=w.lan_id,account_device=True)
                for w in self.hub if w is not self]
    def _send_signal(self,value):
        assert value['type']=='lan_authorization'
        target=next(w for w in self.hub if w.device_id==value['peer'])
        target.lan_authorization.emit(dict(type='lan_authorization',peer=self.device_id,value=value['value']))
        return True


class LanAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.lans=[];self.wans=[]
        self.addCleanup(self.cleanup);group=str(uuid4())
        for index in range(2):
            lan=LanTransferService(device_name='LAN QA',bind_host='127.0.0.1',discovery=False);self.lans.append(lan);lan.start()
            wan=AccountWan(group,lan,index+1,self.wans);self.wans.append(wan)
        until(lambda:all(lan.port for lan in self.lans))
        self.auth=[LanAccountAuthorizer(lan,wan) for lan,wan in zip(self.lans,self.wans)]
        self.lans[0].remember_peer(self.lans[1].own_peer());self.lans[1].remember_peer(self.lans[0].own_peer())

    def cleanup(self):
        for lan in self.lans:lan.close()
        for lan in self.lans:
            for worker in lan._threads:worker.join(2)
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
        self.wans[0]._send_signal=Mock(return_value=False)
        identifier=self.lans[0].send(self.lans[1].device_id,[source])
        until(lambda:any(row['state']=='error' for row in states),seconds=4)
        self.assertFalse(offers);self.assertEqual(self.lans[0]._transfers[identifier].done,0)
        self.assertIn('尚未就绪',next(row['message'] for row in states if row['state']=='error'))

    def test_ticket_binds_recipient_certificate_manifest_token_and_session(self):
        receiver=self.auth[1];sender=self.wans[0].device_id
        entries=[dict(path='file.txt',kind='file',size=5)]
        ticket=dict(type='lan-transfer',action='prepare',id=uuid4().hex,token='a'*64,digest=manifest_digest(entries),fingerprint='b'*64)
        receiver._receive(dict(peer=sender,value=ticket));self.assertNotIn(ticket['id'],receiver._tickets)
        ticket['fingerprint']=self.lans[1].fingerprint;receiver._receive(dict(peer=sender,value=ticket))
        transfer=_Transfer(ticket['id'],'receive','Untrusted',token=ticket['token'],manifest_hash=ticket['digest'])
        self.assertTrue(receiver.incoming(transfer)['account_device'])
        transfer.token='c'*64;self.assertIsNone(receiver.incoming(transfer));transfer.token=ticket['token']
        transfer.manifest_hash='d'*64;self.assertIsNone(receiver.incoming(transfer));transfer.manifest_hash=ticket['digest']
        self.wans[1].account_service.signed_in=False;self.assertIsNone(receiver.incoming(transfer))


if __name__=='__main__':unittest.main()
