"""Real manifest reader and receiver lifecycle using owned in-memory streams."""
import hashlib,io,json,secrets,struct,threading,time,unittest
from unittest.mock import patch
from test_html_native_workspace import APP,until,flush
import lan_transfer as lan


class Stream:
    def __init__(self,request,*,fail_close=False):
        raw=json.dumps(request,separators=(',',':')).encode()
        self.incoming=io.BytesIO(struct.pack('!I',len(raw))+raw);self.output=bytearray();self.fail_close=fail_close
    def settimeout(self,*_):pass
    def recv(self,size):return self.incoming.read(size)
    def recv_into(self,target):
        data=self.incoming.read(len(target));target[:len(data)]=data;return len(data)
    def sendall(self,data):self.output.extend(data)
    def close(self,*_,**__):
        if self.fail_close:raise OSError('fixture close failure')
    def messages(self):
        data=bytes(self.output);messages=[]
        while data:
            size=struct.unpack('!I',data[:4])[0];messages.append(json.loads(data[4:4+size]));data=data[4+size:]
        return messages


class ReceiveLifecycleTests(unittest.TestCase):
    def fixture(self):
        service=lan.LanTransferService(discovery=False)
        identifier=secrets.token_hex(16);token=secrets.token_hex(32)
        entries=[dict(kind='file',path='zero.bin',size=0,sha256=hashlib.sha256(b'').hexdigest())]
        manifest=hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest()
        request=dict(protocol=lan.PROTOCOL,id=identifier,token=token,entries=entries,sender='fixture')
        transfer=lan._Transfer(identifier,'receive','fixture',entries=entries,token=token,manifest_hash=manifest,active=True)
        service._transfers[identifier]=transfer
        return service,request,transfer

    def test_reconnect_waits_for_previous_receiver_then_confirms_completed_bytes(self):
        service,request,transfer=self.fixture();stream=Stream(request);errors=[]
        service._slots.acquire()
        def run():
            try:
                with patch.object(service,'_incoming_stream',return_value=stream):service._incoming(stream,'127.0.0.1')
            except Exception as error:errors.append(error)
        worker=threading.Thread(target=run,daemon=True);worker.start()
        try:
            time.sleep(.06);self.assertTrue(worker.is_alive());self.assertFalse(stream.output)
            with service._lock:
                transfer.finished=True;transfer.active=False;transfer.receiver_idle.set()
            worker.join(2);self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
            self.assertEqual([row['state'] for row in stream.messages()],['ready','complete'])
        finally:service.close();service.deleteLater();flush()

    def test_close_failure_still_releases_slot_socket_and_receiver(self):
        service,request,transfer=self.fixture();transfer.active=False
        stream=Stream(request,fail_close=True);service._slots.acquire();transfer.accepted.set()
        try:
            with patch.object(service,'_incoming_stream',return_value=stream):service._incoming(stream,'127.0.0.1')
            self.assertFalse(transfer.active);self.assertIsNone(transfer.socket);self.assertTrue(transfer.receiver_idle.is_set())
            acquired=0
            while service._slots.acquire(blocking=False):acquired+=1
            self.assertEqual(acquired,4)
            for _ in range(acquired):service._slots.release()
        finally:service.close();service.deleteLater();flush()


if __name__=='__main__':unittest.main()
