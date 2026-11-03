"""Requested Qt deletion is retired without suppressing testcase failures."""
import io
import sys
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QEvent,QTimer,QThread
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid
from test_html_native_workspace import APP,flush
import qt_unittest as subject
import test_html_native_workspace as workspace


class QtRunnerTests(unittest.TestCase):
    def run_case(self,action,runner=subject.QtTestRunner):
        return runner(stream=io.StringIO()).run(unittest.TestSuite([unittest.FunctionTestCase(action)]))

    def test_plain_unittest_leaves_requested_delete_but_qt_runner_retires_it(self):
        pending=QWidget();kept=QWidget()
        try:
            def request():pending.deleteLater();APP.processEvents()
            before=self.run_case(request,unittest.TextTestRunner)
            self.assertTrue(before.wasSuccessful());self.assertTrue(isValid(pending))
            after=self.run_case(lambda:None)
            self.assertTrue(after.wasSuccessful());self.assertFalse(isValid(pending))
            self.assertTrue(isValid(kept),'A retained window must not be deleted by the test runner')
        finally:kept.deleteLater();flush()

    def test_queued_callbacks_are_delivered_instead_of_discarded(self):
        delivered=[]
        result=self.run_case(lambda:QTimer.singleShot(0,lambda:delivered.append('actual queued callback')))
        self.assertTrue(result.wasSuccessful());self.assertEqual(delivered,['actual queued callback'])

    def test_original_failure_remains_failure_and_requested_cleanup_still_runs(self):
        pending=QWidget()
        def fails():
            pending.deleteLater()
            self.assertEqual('real failure','not success')
        result=self.run_case(fails)
        self.assertFalse(result.wasSuccessful());self.assertEqual(len(result.failures),1)
        self.assertEqual(result.testsRun,1);self.assertFalse(isValid(pending))

    def test_cleanup_error_is_reported_and_not_hidden(self):
        with patch.object(subject,'drain_requested_deletes',side_effect=RuntimeError('Owned cleanup boundary failure')):
            result=self.run_case(lambda:None)
        self.assertFalse(result.wasSuccessful());self.assertEqual(len(result.errors),1)
        self.assertIn('Owned cleanup boundary failure',result.errors[0][1]);self.assertEqual(result.testsRun,1)

    def test_real_queued_callback_error_marks_failure_and_keeps_previous_hook(self):
        seen=[]
        def boom():raise RuntimeError('Owned queued callback failure')
        previous=lambda *args:seen.append(args)
        with patch.object(sys,'excepthook',previous):
            result=self.run_case(lambda:QTimer.singleShot(0,boom))
            self.assertIs(sys.excepthook,previous)
        self.assertEqual(len(seen),1);self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors),1);self.assertIn('Owned queued callback failure',result.errors[0][1])

    def test_queued_and_direct_interruptions_propagate_and_restore_hook(self):
        for error in (KeyboardInterrupt('Owned cancel'),SystemExit(17)):
            for queued in (False,True):
                with self.subTest(kind=type(error).__name__,queued=queued):
                    previous=lambda *args:None
                    def interrupt():
                        if queued:sys.excepthook(type(error),error,None)
                        else:raise error
                    with patch.object(sys,'excepthook',previous),patch.object(subject,'drain_requested_deletes',side_effect=interrupt):
                        with self.assertRaises(type(error)):self.run_case(lambda:None)
                        self.assertIs(sys.excepthook,previous)

    def test_no_application_case_keeps_standard_unittest_behavior(self):
        with patch.object(QCoreApplication,'instance',return_value=None):
            result=self.run_case(lambda:None)
        self.assertTrue(result.wasSuccessful());self.assertEqual(result.testsRun,1)


class NativeFixtureWaitTests(unittest.TestCase):
    def test_wait_runs_a_real_qt_loop_like_the_application_not_a_busy_test_pump(self):
        before=QThread.currentThread().loopLevel();observed=[]
        QTimer.singleShot(0,lambda:observed.append(QThread.currentThread().loopLevel()))
        workspace.until(lambda:bool(observed))
        self.assertGreater(observed[0],before,'Worker budgets must be exercised under a real Qt event loop')

    def test_already_ready_does_not_pump_unrelated_pending_events(self):
        delivered=[];QTimer.singleShot(0,lambda:delivered.append(True))
        workspace.until(lambda:True)
        self.assertEqual(delivered,[]);flush();self.assertEqual(delivered,[True])

    def test_actual_pending_work_and_original_timeout_are_both_observed(self):
        import time
        delivered=[];QTimer.singleShot(15,lambda:delivered.append(True))
        workspace.until(lambda:bool(delivered),seconds=.3);self.assertEqual(delivered,[True])
        started=time.monotonic()
        with self.assertRaisesRegex(AssertionError,'Native workspace fixture did not settle'):
            workspace.until(lambda:False,seconds=.02)
        self.assertGreaterEqual(time.monotonic()-started,.015)

    def test_predicate_errors_and_interruptions_propagate_and_stop_only_owned_wait(self):
        for error in (TypeError('Owned predicate failure'),KeyboardInterrupt('Owned interruption')):
            calls=[];outside=[];kept=QTimer();kept.setInterval(5);kept.timeout.connect(lambda:outside.append(True));kept.start()
            def predicate():
                calls.append(True)
                if len(calls)>1:raise error
                return False
            try:
                with self.assertRaises(type(error)):workspace.until(predicate)
                count=len(calls);before=len(outside)
                from PySide6.QtTest import QTest
                QTest.qWait(20)
                self.assertEqual(len(calls),count);self.assertGreater(len(outside),before)
            finally:kept.stop()


if __name__=='__main__':unittest.main()
