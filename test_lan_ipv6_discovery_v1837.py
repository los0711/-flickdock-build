"""No-route link-local discovery uses interface broadcast and IPv6 scope."""
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtNetwork import QNetworkInterface
from lan_transfer import LanTransferService,PROTOCOL,_broadcast_addresses
from test_lan_transfer_v181 import APP


class LinkLocalDiscoveryTests(unittest.TestCase):
    def test_ipv4_link_local_broadcast_is_used_without_a_default_route(self):
        address=SimpleNamespace(toString=lambda:'169.254.255.255')
        entry=SimpleNamespace(broadcast=lambda:address)
        interface=SimpleNamespace(addressEntries=lambda:[entry])
        with patch.object(QNetworkInterface,'allInterfaces',return_value=[interface]):
            self.assertIn('169.254.255.255',_broadcast_addresses())

    def test_ipv6_multicast_advertises_and_keeps_incoming_scope(self):
        service=LanTransferService(APP,device_name='local',discovery=False)
        service.port=12345;service.fingerprint='b'*64
        flags=QNetworkInterface.InterfaceFlag.IsUp|QNetworkInterface.InterfaceFlag.CanMulticast
        interface=SimpleNamespace(flags=lambda:flags,index=lambda:7)
        payload=json.dumps(dict(id='a'*32,name='peer',port=23456,fingerprint='c'*64,protocol=PROTOCOL)).encode()
        class Datagram:
            def __init__(self):self.options=[];self.sent=[]
            def setsockopt(self,*args):self.options.append(args)
            def bind(self,*_):pass
            def settimeout(self,*_):pass
            def sendto(self,data,address):self.sent.append((data,address))
            def recvfrom(self,_):service._stopped.set();return payload,('fe80::1234',45875,0,7)
            def close(self):pass
        datagram=Datagram()
        with patch.object(QNetworkInterface,'allInterfaces',return_value=[interface]),patch('lan_transfer.socket.socket',return_value=datagram):
            service._discovery_v6()
        self.assertTrue(any(option[1]==socket.IPV6_JOIN_GROUP for option in datagram.options))
        self.assertEqual(datagram.sent[0][1],('ff02::1',45875,0,7))
        self.assertEqual(service.peers()[0]['host'],'fe80::1234%7')


if __name__=='__main__':unittest.main()
