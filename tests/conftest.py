"""Release deferred Qt widgets between isolated desktop tests.

processEvents alone does not flush DeferredDelete without a running app.exec
loop. Keeping those old windows alive makes later tests retain event filters,
styles and native handles, and can exhaust Windows GUI resources.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def release_deferred_windows():
    yield
    if "PySide6.QtWidgets" in sys.modules:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
