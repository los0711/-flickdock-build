import json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_wan_account_v1819 import APP,Account
from wan_transfer import WanTransferService


def until(test):
    end=time.monotonic()+3
    while not test() and time.monotonic()<end:APP.processEvents();time.sleep(.002)
    if not test():raise AssertionError('Automatic receive did not settle')


class AutoAccount(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.service=WanTransferService(self.root/'wan',enabled=False);self.account=Account()
        self.service.attach_account(self.account);self.service._bound_identity=self.service._account_identity()
        self.service._account_nonce='a'*32;self.service._account_ack=True
        self.service.enabled=True;self.service._connected=True
        self.service.automatic_receive_directory=lambda:self.root
        self.sent=[];self.offers=[];self.service._send_signal=lambda row:self.sent.append(row) or True
        self.service.offer_received.connect(self.offers.append)
    def tearDown(self):
        self.service.close();self.service._accept_pool.waitForDone(3000);APP.processEvents();self.tmp.cleanup()
    def offer(self,**change):
        value=dict(schema_version=1,type='request',id='b'*32,peer=dict(id='c'*64,name='Mac',account_device=True),
            token='d'*64,digest='e'*64,total='12',count=1,names=['original.txt'],
            account_authorization=dict(receiver_device='fixture-device',nonce='a'*32))
        value.update(change);self.service._message(json.dumps(value));return value
    def test_authenticated_offer_validates_destination_off_gui_and_accepts_once(self):
        native=Path.resolve;threads=[];gui=threading.get_ident()
        def resolve(path,*args,**kwargs):
            threads.append(threading.get_ident());return native(path,*args,**kwargs)
        with patch('wan_transfer.Path.resolve',new=resolve):
            value=self.offer();until(lambda:bool(self.offers));self.service._message(json.dumps(value));APP.processEvents()
        self.assertEqual(self.sent,[dict(type='accept',id='b'*32,auto_account=True)])
        self.assertTrue(self.offers[0]['automatic']);self.assertEqual(len(self.offers),1)
        self.assertTrue(threads);self.assertNotIn(gui,threads)
    def test_peer_flag_wrong_nonce_or_wrong_device_never_bypasses_manual_confirmation(self):
        for auth in (None,{},dict(receiver_device='other',nonce='a'*32),dict(receiver_device='fixture-device',nonce='f'*32)):
            with self.subTest(auth=auth):
                self.service._pending.clear();self.offers.clear();self.offer(account_authorization=auth)
                self.assertEqual(len(self.offers),1);self.assertNotIn('automatic',self.offers[0]);self.assertEqual(self.sent,[])
    def test_not_logged_in_or_pending_bind_cannot_accept(self):
        self.service._bound_identity=None;self.offer()
        self.assertEqual(self.sent,[]);self.assertNotIn('automatic',self.offers[0])
    def test_logout_during_background_validation_discards_late_accept(self):
        entered=threading.Event();release=threading.Event();native=Path.resolve
        def slow(path,*args,**kwargs):
            entered.set();release.wait(2);return native(path,*args,**kwargs)
        with patch('wan_transfer.Path.resolve',new=slow):
            self.offer();until(entered.is_set)
            self.account.signed_in=False;self.account.account_changed.emit(None)
            release.set();until(lambda:not self.service._accept_jobs)
        self.assertFalse(any(row.get('type')=='accept' for row in self.sent));self.assertEqual(self.offers,[])
    def test_invalid_destination_preserves_manual_request(self):
        self.service.automatic_receive_directory=lambda:self.root/'missing';self.offer();until(lambda:bool(self.offers))
        self.assertEqual(self.sent,[]);self.assertNotIn('automatic',self.offers[0])


if __name__=='__main__':unittest.main()
