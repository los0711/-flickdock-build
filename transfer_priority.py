"""Yield bulk payloads while a small clipboard transfer uses the network.

Control messages, signaling and native clipboard access never wait here. A
bounded trickle keeps existing peers alive without changing the wire protocol.
Normal transfers have no rate cap and perform only an Event flag read.
"""
import threading
import time

_bulk_ready=threading.Event()
_bulk_ready.set()
BULK_TRICKLE_INTERVAL_SECONDS=2.0


def set_clipboard_priority(active):
    if active:_bulk_ready.clear()
    else:_bulk_ready.set()


def clipboard_priority_active():
    return not _bulk_ready.is_set()


def bulk_delay(last_payload):
    return max(0.,BULK_TRICKLE_INTERVAL_SECONDS-(time.monotonic()-last_payload)) if clipboard_priority_active() else 0.


def yield_bulk_payload(cancel):
    if _bulk_ready.is_set():return
    deadline=time.monotonic()+BULK_TRICKLE_INTERVAL_SECONDS
    while not _bulk_ready.is_set():
        if cancel.is_set():raise InterruptedError('已取消传输。')
        remaining=deadline-time.monotonic()
        if remaining<=0:return
        _bulk_ready.wait(min(.05,remaining))
