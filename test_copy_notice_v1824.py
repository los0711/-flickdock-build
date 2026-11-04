"""The copy-notice switch suppresses both outcomes, including delayed replies."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,flush
from flickdock import ShelfManager
from intake_notice import ClipboardNotice


class CopyNoticeTests(unittest.TestCase):
    def test_disabled_manager_never_constructs_a_card_for_either_result(self):
        manager=SimpleNamespace(_quitting=False,_clipboard_runtime_enabled=lambda:True,
                                settings=SimpleNamespace(clipboard_success_notice=False))
        for ok in (True,False):ShelfManager._clipboard_copy_result(manager,ok,{'id':'copy'})
        self.assertFalse(hasattr(manager,'clipboard_notice'))

    def test_toggle_off_suppresses_queued_and_direct_results_of_both_kinds(self):
        settings=SimpleNamespace(theme='dark',clipboard_success_notice=True)
        card=ClipboardNotice(SimpleNamespace(settings=settings))
        try:
            with patch('native_ui_common.reduced_motion',return_value=True):
                for ok in (True,False):
                    settings.clipboard_success_notice=True
                    card.schedule_result(ok,dict(id='delayed',value=('text',{'text':'copy'})))
                    settings.clipboard_success_notice=False
                    QTest.qWait(180);flush();self.assertFalse(card.isVisible())
                    card.show_result(ok,{});self.assertFalse(card.isVisible())
                settings.clipboard_success_notice=True;card.show_result(False,{})
                self.assertTrue(card.isVisible())
                settings.clipboard_success_notice=False;card.supersede()
                self.assertFalse(card.isVisible());self.assertIsNone(card._pending_result)
                self.assertFalse(card._result_timer.isActive())
        finally:card.hide();card.deleteLater();flush()


if __name__=='__main__':unittest.main(verbosity=2)
