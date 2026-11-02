"""Fresh metadata authority for virgin Windows reception; owned providers only."""
import unittest
import time
from dataclasses import replace
from uuid import uuid4
from unittest.mock import patch
import clipboard_sync as sync
import test_clipboard_empty_bootstrap_v1825 as bootstrap_fixture
from test_html_native_workspace import flush, until


class CurrentEmptyReceiveTests(unittest.TestCase):
    setUp = bootstrap_fixture.EmptyBootstrapFlowTests.setUp
    tearDown = bootstrap_fixture.EmptyBootstrapFlowTests.tearDown
    start = bootstrap_fixture.EmptyBootstrapFlowTests.start
    posts = bootstrap_fixture.EmptyBootstrapFlowTests.posts

    def head(self, text='peer first copy', version=1):
        payload = sync.inline_text(('text', dict(text=text, html='')))
        return dict(id=str(uuid4()), version=version, ready=True,
                    kind='text', size=payload['size'], sha256=payload['sha256'],
                    inline=payload['inline']['body'], copied_at=1)

    def test_virgin_receive_does_not_require_copy_or_fabricate_zero_sample(self):
        client = self.start()
        self.peer.head = self.head()
        notices = []
        client.copy_result.connect(lambda *args: notices.append(args))
        client._pushed(dict(clipboard=self.peer.head))
        until(lambda: self.clip.writes == 1)
        self.assertEqual(self.clip.value[1]['text'], 'peer first copy')
        self.assertGreater(client._native_sample['sequence'], 0)
        self.assertEqual(self.clip.reads, 0)
        self.assertEqual(self.provider.reads, 0)
        self.assertEqual(self.posts(), 0)
        self.assertEqual(notices, [])
        for unused in range(3):
            client.refresh()
            flush()
        self.assertEqual(self.clip.writes, 1)

    def test_metadata_unknown_nonempty_busy_and_errors_do_not_authorize_write(self):
        client = self.start()
        for mode in ('busy', 'nonempty', 'count-error', 'close-error'):
            with self.subTest(mode=mode):
                self.provider.busy = mode == 'busy'
                self.provider.force_count = 1 if mode == 'nonempty' else None
                self.provider.count_error = OSError(5, 'owned count') if mode == 'count-error' else None
                self.provider.close_error = OSError(5, 'owned close') if mode == 'close-error' else None
                self.peer.head = self.head()
                client._pushed(dict(clipboard=self.peer.head))
                flush()
                self.assertEqual(self.clip.writes, 0)
                self.assertIsNone(client._native_sample)

    def test_current_authority_is_not_a_historical_bootstrap_or_copied_dataclass(self):
        client = self.start()
        head = self.head()
        self.assertFalse(client._consume_current_empty_receive(client._empty_bootstrap, head['id']))
        authority = client._current_empty_receive(head)
        self.assertIsNotNone(authority)
        self.assertFalse(client._consume_current_empty_receive(replace(authority), head['id']))
        self.assertTrue(client._consume_current_empty_receive(authority, head['id']))
        self.assertFalse(client._consume_current_empty_receive(authority, head['id']))

    def test_copy_between_count_close_and_issue_rejects_current_authority(self):
        client = self.start()
        self.provider.after_close = lambda: self.clip.copy('new local copy')
        self.assertIsNone(client._current_empty_receive(self.head()))
        self.assertEqual(self.clip.writes, 0)
        self.assertEqual(self.clip.value[1]['text'], 'new local copy')

    def test_fresh_authority_rejects_changed_local_or_session_state(self):
        for boundary in ('copy', 'notice', 'token', 'device', 'epoch', 'generation', 'off', 'closed'):
            with self.subTest(boundary=boundary):
                client = self.start()
                head = self.head()
                authority = client._current_empty_receive(head)
                self.assertIsNotNone(authority)
                if boundary == 'copy': self.clip.copy('local wins')
                elif boundary == 'notice': client._notification_serial = getattr(client, '_notification_serial', 0) + 1
                elif boundary == 'token': self.peer.token += '-new'
                elif boundary == 'device': self.peer.device_id = str(uuid4())
                elif boundary == 'epoch': client._epoch += 1
                elif boundary == 'generation': client._generation += 1
                elif boundary == 'off': client.enabled = False
                else: client.closed = True
                self.assertFalse(client._consume_current_empty_receive(authority, head['id']))
                self.assertEqual(self.clip.writes, 0)
                client.closed = False
                client.close()
                until(lambda: not client._jobs)
                self.client = None
                self.clip.sequence = 0
                self.clip.value = None
                self.clip.marker = b''
                self.clip.owned = False
                self.reader.active.clear()

    def test_late_cancelled_zero_reader_cannot_clear_received_positive_sample(self):
        client = self.start()
        client._ensure_native(lambda: None, 'refresh')
        token = client._native_read_token
        callback = self.reader.active[token][0]
        self.peer.head = self.head('https://example.test/owned-link')
        client._pushed(dict(clipboard=self.peer.head))
        until(lambda: self.clip.writes == 1)
        sample = client._native_sample
        callback(dict(status='unknown', valid=False, sequence_before=0, sequence_after=0), None)
        self.assertIs(client._native_sample, sample)
        self.assertEqual(self.posts(), 0)
        self.assertEqual(self.clip.writes, 1)

    def test_authority_is_bound_to_exact_head_and_new_metadata_invalidates_old(self):
        client = self.start()
        first, second = self.head(), self.head(version=2)
        authority = client._current_empty_receive(first)
        self.assertFalse(client._consume_current_empty_receive(authority, second['id']))
        authority = client._current_empty_receive(first)
        newer = client._current_empty_receive(second)
        self.assertFalse(client._consume_current_empty_receive(authority, first['id']))
        self.assertTrue(client._consume_current_empty_receive(newer, second['id']))

    def test_forged_zero_metadata_cannot_authorize_fetch_or_apply(self):
        client = self.start()
        with patch.object(sync, '_windows_clipboard_metadata', return_value=dict(
                sequence_before=0, sequence_after=0, format_count=0, empty_proof={'sequence': 0})):
            self.assertIsNone(client._current_empty_receive())
            self.assertIsNone(client._current_empty_receive(self.head()))
        self.assertIsNone(client._native_sample)
        self.assertEqual(self.clip.writes, 0)

    def test_copy_during_retiring_zero_reader_wins_without_failed_success_notice(self):
        client = self.start()
        self.peer.head = self.head()
        notices = []
        client.copy_result.connect(lambda *args: notices.append(args))
        cancel = client._cancel_native_read
        def copy_before_write():
            cancel()
            self.clip.copy('local copy at final write boundary')
        with patch.object(client, '_cancel_native_read', side_effect=copy_before_write):
            client._pushed(dict(clipboard=self.peer.head))
            flush()
        self.assertEqual(self.clip.writes, 0)
        self.assertEqual(self.clip.value[1]['text'], 'local copy at final write boundary')
        self.assertEqual(notices, [])

    def test_expired_cache_cleanup_request_survives_zero_reader_retirement(self):
        client = self.start()
        expired = client._directory()
        (expired / 'owned-obsolete.bin').write_bytes(b'owned expired cache')
        client._directories = [(expired, time.time() - 2 * 86400)]
        client._cleanup()
        self.assertIn('cleanup', client._native_waiters)
        token = client._native_read_token
        old_callback = self.reader.active[token][0]
        self.peer.head = self.head()
        client._pushed(dict(clipboard=self.peer.head))
        until(lambda: self.clip.writes == 1 and not expired.exists() and not client._jobs)
        self.assertNotIn('cleanup', client._native_waiters)
        sample = client._native_sample
        old_callback(dict(status='unknown', valid=False, sequence_before=0, sequence_after=0), None)
        self.assertIs(client._native_sample, sample)
        self.assertEqual(self.posts(), 0)


if __name__ == '__main__':
    unittest.main()
