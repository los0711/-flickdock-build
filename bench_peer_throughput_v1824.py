"""Same-host bytes plus injected provider delay; never a WAN speed claim."""
import hashlib
import json
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4
from test_peer_sync_v1824 import Account,Wan,Hub
from test_html_native_workspace import flush,until
from peer_sync import PeerSync


def main():
    report=dict(scope='same Windows host; actual native DTLS or pinned TLS; injected provider delay, not physical WAN RTT',samples=[])
    with tempfile.TemporaryDirectory(prefix='flickdock-peer-bench-') as temporary:
        root=Path(temporary);payload=os.urandom(16*1024*1024);source=root/'original.bin';source.write_bytes(payload)
        record=dict(id=str(uuid4()),size=len(payload),sha256=hashlib.sha256(payload).hexdigest())
        for route in ('lan','p2p'):
            hub=Hub();clients=[];mode=patch('peer_sync.try_nearby',side_effect=lambda raw,*_:raw) if route=='p2p' else patch('peer_sync.try_nearby',__import__('nearby_transport').try_nearby)
            with mode:
                try:
                    group=str(uuid4())
                    for index in range(2):
                        account=Account(group);wan=Wan(hub,account,index+1);hub.wans.append(wan);clients.append(PeerSync(account,wan))
                    for wan in hub.wans:wan._connected=True;wan.peers_changed.emit(wan.peers())
                    until(lambda:all(len(client.available())==1 for client in clients),seconds=20)
                    assert clients[1].available()[0]['route']==route
                    original=clients[0]._provide;delay=[0.]
                    def provide(*args):
                        if delay[0]:time.sleep(delay[0])
                        return original(*args)
                    clients[0].available()[0]['rpc'].provider=provide
                    clients[0].publish('shelf',record,source)
                    until(lambda:bool(clients[1]._sources))
                    for milliseconds in (0,40):
                        delay[0]=milliseconds/1000
                        for repeat in range(3):
                            print(route,milliseconds,repeat,flush=True)
                            destination=root/f'{route}-{milliseconds}-{repeat}.bin';done=[];start=time.perf_counter()
                            clients[1].fetch('shelf',record,destination,lambda path,error:done.append((path,error)))
                            until(lambda:bool(done),seconds=45);elapsed=time.perf_counter()-start
                            assert done[0][1] is None,str(done[0][1]);assert destination.read_bytes()==payload
                            report['samples'].append(dict(route=route,provider_delay_ms=milliseconds,bytes=len(payload),seconds=elapsed,mib_per_second=len(payload)/1024**2/elapsed))
                finally:
                    for client in clients:client.close()
                    until(lambda:all(not client._fetches for client in clients));flush()
    target=Path(sys.argv[1]);target.write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
