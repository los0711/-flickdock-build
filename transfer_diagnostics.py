"""Opt-in, payload-free measurements for a single manual transfer."""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import threading
import time

_write_lock = threading.Lock()


class TransferDiagnostics:
    def __init__(self, base, protocol, direction):
        self.base = Path(base)
        self.protocol, self.direction = protocol, direction
        self.started = time.perf_counter()
        self.lock = threading.Lock()
        self.route = 'direct' if protocol == 'lan' else None
        self.relay_transport = None
        self.files = None
        self.total_bytes = None
        self.payload_bytes = 0
        self._payload_started = None
        self._payload_last_progress = None
        self.fsync_total_ms = 0.0 if direction == 'receive' else None
        self.fsync_count = 0 if direction == 'receive' else None
        self.file_times = {}
        self.file_sizes = {}
        self._file_started = {}
        self.ack_wait_ms_total = 0.0 if direction == 'send' else None
        self.reconnect_count = 0
        self.socket_timeout_count = 0
        self.blocked_by_clipboard_priority_ms = 0.0
        self.rtt_ms = None
        self.buffered_amount_max = None
        self.unacked_max = None
        self.stats_samples = 0
        self._wire_generation = None
        self._wire_previous = None
        self._wire_bytes = 0
        self._wire_seconds = 0.0
        self._finished = False

    def manifest(self, files, total):
        with self.lock:
            self.files=files if type(files) is int and 0<=files<=100000 else None
            self.total_bytes=total if type(total) is int and 0<=total<=2**63-1 else None

    def add(self, name, value):
        with self.lock:
            if name == 'payload_bytes' and value > 0:
                now=time.perf_counter()
                if self._payload_started is None:self._payload_started=now
                self._payload_last_progress=now
            setattr(self, name, getattr(self, name) + value)

    def begin_payload(self):
        """Start after consent/ready, rather than including user response time."""
        with self.lock:
            if self._payload_started is None:self._payload_started=time.perf_counter()

    def begin_file(self, ordinal, size=None):
        with self.lock:
            self._file_started.setdefault(ordinal,time.perf_counter())
            if type(size) is int and size>=0:self.file_sizes.setdefault(ordinal,size)

    def file_finished(self, ordinal):
        with self.lock:
            started=self._file_started.pop(ordinal,None)
            if started is not None:self.file_times[ordinal]=self.file_times.get(ordinal,0)+(time.perf_counter()-started)*1000

    def route_changed(self, route, relay_transport=None):
        with self.lock:
            if route in ('direct', 'relay') and route != self.route:
                self.route = route
                self.relay_transport = None
            if relay_transport in ('udp', 'tcp', 'tls'):self.relay_transport = relay_transport

    def sample(self, event, generation):
        with self.lock:
            self.stats_samples += 1
            for source, peak, target in (('rttMs',None,'rtt_ms'),('buffered','bufferedMax','buffered_amount_max'),
                                         ('unacked','unackedMax','unacked_max')):
                value = event.get(peak) if peak and type(event.get(peak)) is int else event.get(source)
                if type(value) is int and 0 <= value <= 2**53:
                    previous = getattr(self, target)
                    setattr(self, target, value if target == 'rtt_ms' or previous is None else max(previous, value))
            value = event.get('sent' if self.direction == 'send' else 'received')
            if type(value) is int and 0 <= value <= 2**53:
                if self._wire_generation != generation:
                    self._wire_generation = generation
                    self._wire_previous = None
                now=time.perf_counter()
                if self._wire_previous is not None:
                    previous,previous_at=self._wire_previous
                    if value>=previous:
                        self._wire_bytes+=value-previous
                        self._wire_seconds+=now-previous_at
                self._wire_previous=(value,now)

    def finish(self, state):
        with self.lock:
            if self._finished:return
            self._finished = True
            seconds = max(0.000001, time.perf_counter() - self.started)
            payload_seconds=(self._payload_last_progress-self._payload_started
                             if self._payload_started is not None and self._payload_last_progress is not None else None)
            durations = sorted(self.file_times.values())
            def percentile(fraction):
                return round(durations[max(0, math.ceil(len(durations)*fraction)-1)], 3) if durations else None
            row = dict(protocol=self.protocol, direction=self.direction, state=state, route=self.route,
                relay_transport=self.relay_transport if self.route == 'relay' else None,
                files=self.files, total_bytes=self.total_bytes, seconds=round(seconds, 3),
                wire_bytes_per_second=round(self._wire_bytes/self._wire_seconds) if self._wire_seconds>0 else None,
                wire_measurement='webrtc_sample_deltas' if self._wire_seconds>0 else None,
                wire_sample_seconds=round(self._wire_seconds,3) if self._wire_seconds>0 else None,
                payload_bytes_per_second=round(self.payload_bytes/seconds),
                payload_transfer_seconds=round(payload_seconds,3) if payload_seconds is not None else None,
                payload_transfer_bytes_per_second=round(self.payload_bytes/payload_seconds) if payload_seconds and payload_seconds>0 else None,
                fsync_total_ms=round(self.fsync_total_ms,3) if self.fsync_total_ms is not None else None,
                fsync_count=self.fsync_count, per_file_ms_p50=percentile(.5), per_file_ms_p95=percentile(.95),
                slowest_files=[dict(ordinal=ordinal,size_bytes=self.file_sizes.get(ordinal),ms=round(ms,3)) for ordinal,ms in sorted(self.file_times.items(),key=lambda item:item[1],reverse=True)[:3]],
                per_file_timing='send_until_submitted' if self.direction=='send' else 'receive_through_sha256',
                ack_wait_ms_total=round(self.ack_wait_ms_total,3) if self.ack_wait_ms_total is not None else None,
                reconnect_count=self.reconnect_count, socket_timeout_count=self.socket_timeout_count,
                blocked_by_clipboard_priority_ms=round(self.blocked_by_clipboard_priority_ms,3),
                rtt_ms=self.rtt_ms, buffered_amount_max=self.buffered_amount_max, unacked_max=self.unacked_max,
                stats_samples=self.stats_samples)
        root=self.base/'diagnostics'
        with _write_lock:
            if root.is_symlink():raise OSError('diagnostics directory is a symlink')
            root.mkdir(parents=True,exist_ok=True)
            path=root/('transfers-'+datetime.now().strftime('%Y-%m-%d')+'.jsonl')
            if path.is_symlink():raise OSError('diagnostics file is a symlink')
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600),'a',encoding='utf8') as output:
                output.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')


def create(base, protocol, direction):
    return TransferDiagnostics(base,protocol,direction) if base is not None and os.environ.get('FLICKDOCK_TRANSFER_DIAG') == '1' else None
