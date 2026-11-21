"""Real loopback TLS payload and account consent metadata, no desktop."""
import hashlib,os,threading,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock,patch
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

    def test_unavailable_account_auth_keeps_tls_transfer_and_requires_real_consent(self):
        source=self.root/'local-only.txt';source.write_text('original')
        destination=self.root/'accepted';destination.mkdir();offers=[];completed=[]
        self.wans[0]._connected=False;self.wans[1]._connected=False
        self.wans[0]._send_signal=Mock(return_value=False)
        def receive(offer):
            offers.append(offer)
            self.assertFalse(offer.get('account_device',False))
            self.assertFalse(list(destination.iterdir()))
            self.lans[1].accept(offer['id'],destination,confirmation=offer['code'])
        self.lans[1].offer_received.connect(receive);self.lans[1].completed.connect(completed.append)
        self.lans[0].send(self.lans[1].device_id,[source])
        until(lambda:bool(completed),seconds=5)
        self.wans[0]._send_signal.assert_not_called();self.assertEqual(len(offers),1)
        self.assertEqual(Path(completed[0]['paths'][0]).read_text(),'original')

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

    def test_same_account_prepare_survives_binding_and_device_list_arriving_later(self):
        from flickdock import ShelfManager
        lan,wan=self.lans[1],self.wans[1]
        source=self.root/'file.txt';source.write_text('exact payload',encoding='utf8')
        destination=self.root/'received';destination.mkdir()
        offers=[];completed=[];authorization=[]
        manager=SimpleNamespace(lan=lan,account=wan.account_service,
            settings=SimpleNamespace(lan_receive_dir=str(destination)),
            lan_receive_notice=SimpleNamespace(begin=Mock()),
            show_lan_dialog=Mock(),lan_dialog=SimpleNamespace(panel=SimpleNamespace(show_offer=Mock())))
        lan.offer_received.connect(offers.append)
        lan.offer_received.connect(lambda offer:ShelfManager._lan_offer(manager,offer))
        lan.completed.connect(completed.append)
        wan.lan_authorization.connect(authorization.append)
        wan._bound_identity=None
        with patch.object(wan,'peers',return_value=[]):
            wan.peers_changed.emit([])
            self.lans[0].send(lan.device_id,[source])
            until(lambda:bool(authorization))
            wan._bound_identity=wan._account_identity()
            wan.peers_changed.emit([])
            self.assertEqual(offers,[])
        wan.peers_changed.emit(wan.peers())
        until(lambda:bool(offers),seconds=7)
        self.assertTrue(offers[0].get('account_device'))
        self.assertEqual(offers[0]['account_id'],wan.account_service.account['id'])
        until(lambda:bool(completed),seconds=5)
        self.assertEqual(Path(completed[0]['paths'][0]).read_bytes(),source.read_bytes())
        self.assertEqual(len(offers),1)
        manager.show_lan_dialog.assert_not_called()
        manager.lan_receive_notice.begin.assert_called_once()

    def test_prepared_offer_is_discarded_when_the_local_session_changes(self):
        sender,wan=self.wans;receiver=self.auth[1]
        entries=[dict(path='file.txt',kind='file',size=5)]
        ticket=dict(type='lan-transfer',action='prepare',id=uuid4().hex,token='a'*64,
                    digest=manifest_digest(entries),fingerprint=self.lans[1].fingerprint)
        transfer=_Transfer(ticket['id'],'receive','LAN device',token=ticket['token'],manifest_hash=ticket['digest'])
        wan._bound_identity=None
        sender._send_signal(dict(type='lan_authorization',peer=wan.device_id,value=ticket))
        wan.account_service.api.token='new-session'
        wan.account_service.account_changed.emit(None)
        wan._bound_identity=wan._account_identity()
        wan.peers_changed.emit(wan.peers())
        self.assertIsNone(receiver.incoming(transfer))

    def test_manual_same_account_send_never_shows_a_confirmation_code(self):
        source=self.root/'manual-same-account.txt'
        source.write_text('verified LAN transfer',encoding='utf8')
        destination=self.root/'manual-inbox';destination.mkdir()
        sender,receiver=self.lans
        sent=[]
        completed=[]
        offers=[]
        dialog=LanDialog(sender,SettingsStore(self.root/'profile'))
        panel=dialog.panel
        def inspect(event):
            if event['state']=='waiting':
                sent.append((event,panel.send_code.isHidden(),panel._rows[event['id']]['detail'].text()))
        def receive(offer):
            offers.append(offer)
            receiver.accept(offer['id'],destination)
        sender.progress.connect(inspect)
        receiver.offer_received.connect(receive)
        receiver.completed.connect(completed.append)
        try:
            panel.queue_paths([source])
            panel.send_button.click()
            until(lambda:bool(completed) and bool(sent),seconds=7)
            event,hidden,label=sent[0]
            self.assertTrue(hidden)
            self.assertTrue(event['account_device'])
            self.assertEqual(event['code'],'')
            self.assertNotIn('确认码',label)
            self.assertTrue(offers[0]['account_device'])
            self.assertEqual(offers[0]['code'],'')
            self.assertEqual(Path(completed[0]['paths'][0]).read_bytes(),source.read_bytes())
        finally:dialog.close();dialog.deleteLater();flush()

    def test_manual_known_peer_waits_for_rebinding_then_sends_without_a_code(self):
        source=self.root/'manual-reconnected.txt';source.write_text('after rebinding')
        destination=self.root/'manual-inbox';destination.mkdir()
        sender,receiver=self.lans;wan=self.wans[0]
        offers=[];completed=[];progress=[]
        receiver.offer_received.connect(offers.append)
        receiver.offer_received.connect(lambda offer:receiver.accept(offer['id'],destination))
        receiver.completed.connect(completed.append)
        sender.progress.connect(progress.append)
        dialog=LanDialog(sender,SettingsStore(self.root/'profile'))
        try:
            wan._bound_identity=None
            with patch.object(wan,'peers',return_value=[]):
                wan.peers_changed.emit([])
                dialog.panel.queue_paths([source]);dialog.panel.send_button.click()
                until(lambda:bool(progress))
                self.assertFalse(offers)
                self.assertTrue(next(iter(sender._transfers.values())).account_only)
            wan._bound_identity=wan._account_identity()
            wan.peers_changed.emit(wan.peers())
            until(lambda:bool(completed),seconds=7)
            self.assertEqual(offers[0]['code'],'')
            self.assertTrue(all(row.get('account_device') for row in progress if row['state']=='waiting'))
            self.assertEqual(Path(completed[0]['paths'][0]).read_bytes(),source.read_bytes())
        finally:dialog.close();dialog.deleteLater();flush()

    def test_manual_known_peer_with_no_ticket_fails_without_an_offer(self):
        source=self.root/'manual-account-unavailable.txt';source.write_text('keep file local')
        sender,receiver=self.lans
        progress=[];offers=[]
        sender.progress.connect(progress.append)
        receiver.offer_received.connect(offers.append)
        dialog=LanDialog(sender,SettingsStore(self.root/'profile'))
        try:
            self.wans[0]._connected=False
            with patch('lan_account.AUTHORIZATION_SECONDS',.25):
                dialog.panel.queue_paths([source]);dialog.panel.send_button.click()
                until(lambda:any(row['state']=='error' for row in progress))
            self.assertFalse(offers)
            self.assertTrue(dialog.panel.send_code.isHidden())
            error=next(row for row in progress if row['state']=='error')
            self.assertIn('同账号验证未完成',error['message'])
        finally:dialog.close();dialog.deleteLater();flush()

    def test_shelf_offer_carries_verified_metadata_and_declines_without_resending(self):
        source=self.root/'shelf-test.bin'
        source.write_bytes(b'fixture for LAN shelf delivery')
        destination=self.root/'shelf-inbox'
        destination.mkdir()
        sender,receiver=self.lans
        intake=dict(id=str(uuid4()),version=1,sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    name='shelf-test.bin',mime_type='application/octet-stream')
        offers=[]
        payloads=[]
        progress=[]

        def receive(offer):
            offers.append(offer)
            if len(offers)==1:receiver.accept(offer['id'],destination)
            else:receiver.decline_shelf_offer(offer['id'])

        receiver.offer_received.connect(receive)
        receiver.completed.connect(payloads.append)
        sender.progress.connect(progress.append)
        sender.send(receiver.device_id,[source],shelf_intake=intake)
        until(lambda:bool(payloads),seconds=7)
        self.assertEqual(offers[0]['shelf_intake'],intake)
        self.assertEqual(offers[0]['peer_id'],self.wans[0].device_id)
        self.assertTrue(offers[0]['account_device'])
        self.assertEqual(Path(payloads[0]['paths'][0]).read_bytes(),source.read_bytes())
        rejected=sender.send(receiver.device_id,[source],shelf_intake=intake)
        until(lambda:any(row['id']==rejected and row['state']=='error' for row in progress),seconds=7)
        self.assertEqual(next(row['phase'] for row in progress
                              if row['id']==rejected and row['state']=='error'),'declined')
        self.assertEqual(len(payloads),1)


if __name__=='__main__':unittest.main()
