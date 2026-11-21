"""Real TCP/TLS, WebRTC and cloud synchronization on isolated CI clients."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event, Lock
import time
import traceback

from PySide6.QtWidgets import QApplication

from account_service import AccountService
from ci_shelf_direct_live import MemorySessions, ShelfState
from cloud_events import CloudEvents
from lan_account import LanAccountAuthorizer
from lan_transfer import LanTransferService
from shelf_direct import ShelfDirectDelivery
from shelf_sync import ShelfSync
from sync_scheduler import SyncScheduler
from wan_transfer import WanTransferService


class Exercise:
    def __init__(self, side, root):
        self.side = side
        self.root = root
        self.run_id = os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
        self.report = dict(ok=False, side=side, phase='account')
        self.app = QApplication([])
        self.account = AccountService(root / 'account', store=MemorySessions())
        self.account.device_name = self.name(side, 'starting')
        self.adapter = ShelfState()
        self.scheduler = SyncScheduler(self.app, connected=True)
        self.shelf = ShelfSync(root / 'shelf', self.account, self.adapter, scheduler=self.scheduler)
        self.events = CloudEvents(self.account, self.app)
        self.events.content_changed.connect(self.shelf.request_refresh)
        self.lan = LanTransferService(device_name=self.account.device_name, discovery=side!='C',
                                      discovery_port=46983, diagnostics_base=root)
        self.wan = WanTransferService(root / 'remote', device_name=self.account.device_name)
        self.wan.attach_account(self.account, lan_id=self.lan.device_id, nearby_peers=self.lan.peers)
        self.authorizer = LanAccountAuthorizer(self.lan, self.wan)
        self.direct = ShelfDirectDelivery(self.shelf, self.wan, self.lan)
        self.shelf.direct_delivery = self.direct
        self.lan.offer_received.connect(self.direct.offer_lan)
        self.wan.offer_received.connect(self.direct.offer)
        self.downloads = []
        self.received = {}
        self.failures = []
        self.lan.completed.connect(self.lan_completed)
        self.wan.completed.connect(self.wan_completed)
        self.lan.progress.connect(self.lan_progress)
        self.record_original_downloads()
        self.payload_started = {}
        self.payload_lock = Lock()
        self.both_routes = Event()
        self.testing_parallel = False

    def name(self, side, status):
        return f'Shelf route QA {self.run_id} {side} {status}'

    def fixture(self, scenario):
        return f'Shelf-route-{self.run_id}-{scenario}.bin'

    def payload(self, scenario):
        return hashlib.shake_256(self.fixture(scenario).encode()).digest(6 * 1024 * 1024)

    def wait(self, condition, timeout=75):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.008)
        raise TimeoutError(self.report['phase'])

    def api(self, method, path, **kwargs):
        reply = []
        self.account.api.request(method, path, lambda value, error: reply.append((value, error)), **kwargs)
        self.wait(lambda: bool(reply), 15)
        value, error = reply[0]
        if error:
            raise RuntimeError(getattr(error, 'code', 'api'))
        return value

    def status(self, status):
        self.api('PATCH', '/v1/devices/' + self.account.device_id,
                 data={'name': self.name(self.side, status)})

    def wait_status(self, side, status):
        next_check = 0

        def check():
            nonlocal next_check
            if time.monotonic() < next_check:
                return False
            next_check = time.monotonic() + .75
            return any(device['name'] == self.name(side, status)
                       for device in self.api('GET', '/v1/devices')['devices'])

        self.wait(check, 100)

    def peer(self, side):
        prefix = self.name(side, '')
        return next((peer for peer in self.direct._online_peers().values()
                     if peer['name'].startswith(prefix)), None)

    def entry(self, scenario):
        if not self.shelf.state:
            return None
        return next((row for row in self.shelf.state['entries'].values()
                     if row['name'] == self.fixture(scenario) and not row.get('deleted')
                     and row['version'] == 1 and not row.get('direct_pending')), None)

    def drop(self, scenario):
        source = self.root / self.fixture(scenario)
        source.write_bytes(self.payload(scenario))
        self.direct.mark_drop([str(source)])
        self.adapter.view['permanent'].append(str(source))
        self.shelf.local_changed()
        self.shelf.request_refresh()

    def check_received(self, scenario, route):
        def ready():
            item = self.entry(scenario)
            return item and (route == 'cloud' or item['sha256'] in self.received)

        self.wait(ready, 100)
        row = self.entry(scenario)
        assert Path(row['ref']).read_bytes() == self.payload(scenario)
        assert self.received.get(row['sha256'], 'cloud') == route
        downloads = self.downloads.count(self.fixture(scenario))
        assert downloads == (1 if route == 'cloud' else 0)
        self.report[scenario] = dict(route=route, bytes=row['size'], matching_sha256=True, cloud_downloads=downloads)

    def record_original_downloads(self):
        original = self.shelf._download_record

        def download(record, *args, **kwargs):
            self.downloads.append(record['name'])
            return original(record, *args, **kwargs)

        self.shelf._download_record = download

    def received_on(self, result, route):
        if result['direction'] == 'receive' and len(result['paths']) == 1:
            self.received[hashlib.sha256(Path(result['paths'][0]).read_bytes()).hexdigest()] = route

    def lan_completed(self, result):
        self.received_on(result, 'lan')

    def wan_completed(self, result):
        self.received_on(result, 'p2p')

    def lan_progress(self, result):
        if result['direction'] == 'send' and result['state'] == 'error':
            self.failures.append(result.get('phase'))

    def gate_first_payload(self, route, service):
        original = service._yield_payload

        def wait_for_both(transfer):
            if self.testing_parallel and transfer.direction == 'send':
                with self.payload_lock:
                    self.payload_started.setdefault(route, time.monotonic())
                    if len(self.payload_started) == 2:
                        self.both_routes.set()
                if not self.both_routes.wait(20):
                    raise TimeoutError('LAN and P2P did not start independently')
            original(transfer)

        service._yield_payload = wait_for_both

    def send(self):
        self.report['phase'] = 'discovery'
        self.wait_status('B', 'ready')
        self.wait_status('C', 'ready')
        self.wait(lambda: self.peer('B') and self.peer('C') and not self.shelf.catching_up)
        # The two isolated Windows processes discover each other over UDP.
        peer = self.peer('B')
        self.wait(lambda: self.wan.nearby_peer(peer['id']))
        assert self.wan.nearby_peer(peer['id'])['shelf_drop']
        assert self.wan.nearby_peer(self.peer('C')['id']) is None
        self.report['phase'] = 'parallel'
        self.gate_first_payload('lan', self.lan)
        self.gate_first_payload('p2p', self.wan)
        self.testing_parallel = True
        self.drop('parallel')
        self.wait_status('B', 'received')
        self.wait_status('C', 'offline')
        self.testing_parallel = False
        assert self.both_routes.is_set() and self.entry('parallel')
        self.report['parallel'] = dict(overlapping_payloads=True, cloud_backup=True)

        self.report['phase'] = 'fallback'
        # Use a bound TCP port with no listener to exercise an actual failed
        # connection without affecting UDP discovery or the target's account.
        original = self.lan._connect_peer
        with socket.socket() as unreachable:
            unreachable.bind(('127.0.0.1', 0))
            endpoint = unreachable.getsockname()
            self.lan._connect_peer = lambda row, job, entries: original(
                dict(row, host=endpoint[0], port=endpoint[1]), job, entries)
            self.wait(lambda: self.peer('C') is None)
            self.drop('fallback')
            self.wait_status('B', 'fallback')
        self.lan._connect_peer = original
        assert 'connect' in self.failures and self.entry('fallback')
        self.report['fallback'] = dict(real_tcp_connect_failed=True, cloud_backup=True)
        self.status('backed-up')
        self.report['phase'] = 'offline_cloud_catchup'
        self.wait_status('C', 'restored')
        for scenario in ('parallel','fallback'):
            self.shelf.delete_cloud_copy(self.entry(scenario)['ref'])
        self.wait(lambda: not self.shelf.cloud_copy_operations)

    def receive(self):
        self.status('ready')
        self.report['phase'] = 'parallel'
        self.check_received('parallel', 'lan' if self.side == 'B' else 'p2p')
        if self.side == 'B':
            self.status('received')
            self.report['phase'] = 'fallback'
            self.check_received('fallback', 'p2p')
            self.status('fallback')
        else:
            self.shelf.set_directions(False,False)
            self.wan.set_signaling_allowed(False)
            self.status('offline')
            self.report['phase'] = 'offline_cloud_catchup'
            self.wait_status('A', 'backed-up')
            assert self.entry('fallback') is None
            self.shelf.set_directions(True,True)
            self.wan.set_signaling_allowed(True)
            self.wan.start()
            self.shelf.request_refresh()
            self.check_received('fallback', 'cloud')
            self.status('restored')

    def run(self, credentials):
        errors = []
        sys.excepthook = lambda kind, value, trace: errors.append(kind.__name__)
        try:
            self.account.login(credentials['username'], credentials['password'])
            self.wait(lambda: self.account.signed_in or not self.account.busy)
            assert self.account.signed_in
            self.lan.start()
            self.wan.start()
            self.wait(lambda: self.lan.port and getattr(self.wan,'_shelf_drop_supported',False)
                      and self.direct._authenticated())
            self.send() if self.side == 'A' else self.receive()
            assert not errors
            self.report.update(ok=True, phase='complete')
        finally:
            self.report['callback_errors'] = errors
            self.direct.close()
            self.shelf.close()
            self.lan.close()
            self.wan.close()
            self.events.close()
            self.scheduler.close()
            self.account.close()


if __name__ == '__main__':
    assert os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
    assert os.environ.get('GITHUB_REPOSITORY') == 'los0711/-flickdock-build'
    side, directory = sys.argv[1:]
    assert side in ('A','B','C')
    root = Path(directory).resolve()
    assert root.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve())
    root.mkdir(parents=True, exist_ok=True)
    credentials = json.loads(os.environ.pop('FLICKDOCK_SHELF_QA'))
    assert credentials['username'].startswith('shelf-qa-')
    exercise=Exercise(side, root)
    try:
        exercise.run(credentials)
    except Exception as error:
        exercise.report['error']=type(error).__name__
        exercise.report['failure_frames']=[f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}'
                                           for frame in traceback.extract_tb(error.__traceback__)]
    finally:
        report=json.dumps(exercise.report,indent=2)+'\n'
        (root/'shelf-route-report.json').write_text(report,encoding='utf-8')
        print(report,flush=True)
    raise SystemExit(not exercise.report['ok'])
