"""Serialize OpenSSL calls; wait for socket readiness outside the TLS lock."""
import select
import socket
import ssl
import threading
import time


class TlsStream:
    def __init__(self,stream):
        self._stream=stream;self._timeout=stream.gettimeout();self._lock=threading.Lock()
        stream.setblocking(False)

    def settimeout(self,value):self._timeout=value

    def _deadline(self):return None if self._timeout is None else time.monotonic()+self._timeout

    def _perform(self,method,args,deadline):
        while True:
            try:
                # SSL_read and SSL_write both mutate the same OpenSSL object.
                # A nonblocking call releases this lock before waiting for IO.
                with self._lock:return getattr(self._stream,method)(*args)
            except ssl.SSLWantReadError:read,write=[self._stream],[]
            except ssl.SSLWantWriteError:read,write=[],[self._stream]
            remaining=None if deadline is None else deadline-time.monotonic()
            if remaining is not None and remaining<=0:raise socket.timeout('TLS operation timed out')
            try:ready=select.select(read,write,[],remaining)
            except ValueError as error:raise ConnectionError('TLS stream closed') from error
            if not any(ready):raise socket.timeout('TLS operation timed out')

    def sendall(self,data):
        view=memoryview(data);position=0;deadline=self._deadline()
        while position<len(view):
            count=self._perform('send',(view[position:],),deadline)
            if not count:raise ConnectionError('TLS stream closed while writing')
            position+=count

    def recv(self,size):return self._perform('recv',(size,),self._deadline())
    def recv_into(self,target):return self._perform('recv_into',(target,),self._deadline())

    def version(self):
        with self._lock:return self._stream.version()

    def close(self):
        with self._lock:self._stream.close()
