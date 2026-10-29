"""Yield bulk payloads while a small clipboard transfer uses the network.

Control messages, signaling and native clipboard access never wait here. A
bounded trickle keeps existing peers alive without changing the wire protocol.
Normal transfers have no rate cap and perform only an Event flag read.
"""
import threading
import time

_bulk_ready=threading.Event()
_bulk_ready.set()


def set_clipboard_priority(active):
    if active:_bulk_ready.clear()
    else:_bulk_ready.set()


def yield_bulk_payload(cancel):
    if _bulk_ready.is_set():return
    deadline=time.monotonic()+2
    while not _bulk_ready.is_set():
        if cancel.is_set():raise InterruptedError('已取消传输。')
        remaining=deadline-time.monotonic()
        if remaining<=0:return
        _bulk_ready.wait(min(.05,remaining))
