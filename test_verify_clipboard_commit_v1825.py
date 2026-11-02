"""A prior upload or received cloud head cannot satisfy a new Copy check."""
import gc
import unittest
import weakref
from PySide6.QtCore import QObject,Signal
from verify_clipboard import LocalCopyReceipt


class Client(QObject):
    copy_started=Signal()
    view_changed=Signal(object)
    copy_result=Signal(bool,dict)

    def __init__(self):
        super().__init__();self._generation=4;self._seen='old';self._pending=None;self._local_retry=None

    def start(self,identity):
        self._generation+=1;self.copy_started.emit()
        self.view_changed.emit(dict(phase='uploading',id=identity))

    def commit(self,identity):
        self._seen=identity;self.copy_result.emit(True,dict(id=identity))


class VerifyClipboardCommitTests(unittest.TestCase):
    def setUp(self):
        self.client=Client();self.receipt=LocalCopyReceipt(self.client)
        self.addCleanup(self.receipt.close)

    def test_received_old_cloud_head_is_not_a_local_commit(self):
        self.client._seen='remote-head'
        self.assertFalse(self.receipt.complete())

    def test_late_old_ack_before_copy_debounce_cannot_complete_new_copy(self):
        self.client.commit('old-inflight')
        self.assertFalse(self.receipt.complete())
        self.client.start('new-copy');self.client.commit('old-inflight')
        self.assertFalse(self.receipt.complete())
        self.client.commit('new-copy');self.assertTrue(self.receipt.complete())

    def test_exact_commit_still_waits_for_pending_and_retry_terminal(self):
        self.client.start('new-copy');self.client._pending=object();self.client.commit('new-copy')
        self.assertFalse(self.receipt.complete())
        self.client._pending=None;self.client._local_retry=object()
        self.assertFalse(self.receipt.complete())
        self.client._local_retry=None;self.assertTrue(self.receipt.complete())

    def test_multiple_local_intents_do_not_merge_into_one_success(self):
        self.client.start('first');self.client.start('second');self.client.commit('second')
        self.assertFalse(self.receipt.complete())

    def test_close_retires_only_owned_subscriptions(self):
        receipt=LocalCopyReceipt(self.client);reference=weakref.ref(receipt)
        receipt.close();receipt.close();self.client.start('later');self.client.commit('later')
        self.assertFalse(receipt.complete())
        del receipt;gc.collect();self.assertIsNone(reference())


if __name__=='__main__':unittest.main()
