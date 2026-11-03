"""Signaling replay must not restart a current peer or create two readers."""
import threading
import unittest
from unittest.mock import Mock,patch
import test_wan_transfer_v1816 as fixture


class GenerationTests(unittest.TestCase):
    setUp=fixture.WanRecovery.setUp
    tearDown=fixture.WanRecovery.tearDown
    state=fixture.WanRecovery.state
    message=fixture.WanRecovery.message

    def bridge(self,*,opened=False):
        bridge=Mock();bridge.opened=threading.Event();bridge.closed=False;bridge.route='direct'
        if opened:bridge.opened.set()
        return bridge

    def test_duplicate_begin_while_connecting_keeps_same_native_connection(self):
        state=self.state();state['generation']=0
        bridge=self.bridge()
        with patch('wan_transfer.WebRTCStream',return_value=bridge) as create:
            for _ in range(2):self.message(type='begin',generation=1,iceServers=[],relayAllowed=False)
        self.assertEqual(create.call_count,1)
        bridge.shutdown.assert_not_called()
        self.assertIs(state['bridge'],bridge)

    def test_delayed_begin_cannot_shutdown_newer_open_connection(self):
        state=self.state();bridge=self.bridge(opened=True)
        state.update(generation=3,bridge=bridge);state['ready'].set()
        with patch('wan_transfer.WebRTCStream') as create:
            self.message(type='begin',generation=2,iceServers=[],relayAllowed=False)
        create.assert_not_called();bridge.shutdown.assert_not_called()
        self.assertEqual(state['generation'],3);self.assertTrue(state['ready'].is_set())

    def test_same_generation_after_failure_requires_explicit_new_retry_generation(self):
        state=self.state();old=self.bridge();old.closed=True;state['bridge']=old
        new=self.bridge()
        with patch('wan_transfer.WebRTCStream',return_value=new) as create:
            self.message(type='begin',generation=1,iceServers=[],relayAllowed=False)
            create.assert_not_called()
            self.message(type='begin',generation=2,iceServers=[],relayAllowed=False)
        create.assert_called_once();old.shutdown.assert_called_once()
        self.assertIs(state['bridge'],new)

    def test_repeated_open_event_does_not_spawn_competing_receive_workers(self):
        state=self.state('receive');bridge=self.bridge(opened=True);state['bridge']=bridge
        self.service._slots=Mock();self.service._slots.acquire.return_value=True
        self.service._thread=Mock()
        for _ in range(2):self.service._handle_native(dict(id=self.identifier,generation=1,event=dict(type='open',route='direct')))
        self.service._slots.acquire.assert_called_once()
        self.service._thread.assert_called_once_with(self.service._incoming,bridge,state['peer']['id'])


if __name__=='__main__':unittest.main()
