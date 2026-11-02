"""Unittest runner which honors Qt fixtures' queued object destruction.

The application uses QApplication.exec; a synchronous unittest suite does
not. Drain only events the fixtures already requested, never delete a live
test window or hide a failure to make the next case pass.
"""
import sys
import unittest
from PySide6.QtCore import QCoreApplication,QEvent


def drain_requested_deletes():
    app=QCoreApplication.instance()
    if app is None:return
    app.processEvents()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


class QtTestResult(unittest.TextTestResult):
    def stopTest(self,test):
        queued=[];previous=sys.excepthook
        def capture(kind,error,traceback):
            queued.append((kind,error,traceback))
            previous(kind,error,traceback)
        try:
            sys.excepthook=capture
            try:
                drain_requested_deletes()
            except Exception:
                self.addError(test,sys.exc_info())
            finally:
                sys.excepthook=previous
            # PySide reports exceptions from queued Qt callbacks through the
            # hook rather than raising from processEvents. Retain both that
            # diagnostic and a failing unittest result; never swallow cancel.
            for kind,error,traceback in queued:
                if not isinstance(error,Exception):raise error.with_traceback(traceback)
                self.addError(test,(kind,error,traceback))
        finally:
            super().stopTest(test)


class QtTestRunner(unittest.TextTestRunner):
    resultclass=QtTestResult


if __name__=='__main__':
    unittest.main(module=None,testRunner=QtTestRunner)
