"""File artwork and native reveal actions shared by the transfer workbench."""
from pathlib import Path
import os
import subprocess
import sys

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel

from flickdock_icons import document_pixmap

FILE_ART_SIZE_PX = 32


class FileArtwork(QLabel):
    """Use the existing bounded decoder; never decode file bytes on the UI thread."""
    def __init__(self, cache, parent=None):
        super().__init__(parent)
        self.cache = cache
        self.ref = ''
        self.name = ''
        self.folder = False
        self.dark = False
        self.setFixedSize(FILE_ART_SIZE_PX, FILE_ART_SIZE_PX)
        self.setAlignment(Qt.AlignCenter)
        self.cache.ready.connect(self._ready)
        self.set_source('', '文件')

    def set_source(self, ref, name='', *, folder=False):
        self.ref, self.name, self.folder = str(ref or ''), str(name), folder
        self.setAccessibleName(('文件夹：' if folder else '文件：') + (Path(self.name or self.ref).name or '文件'))
        self._refresh()

    def _ready(self, ref):
        if os.path.normcase(ref) == os.path.normcase(self.ref):
            self._refresh()

    def _refresh(self):
        if self.ref and not self.folder and self.cache.request(self.ref, FILE_ART_SIZE_PX * 2):
            artwork = self.cache.pixmap(self.ref, FILE_ART_SIZE_PX, self.devicePixelRatioF())
        else:
            kind = 'folder' if self.folder else Path(self.name or self.ref).suffix.lstrip('.') or 'file'
            artwork = document_pixmap(kind, FILE_ART_SIZE_PX, dpr=self.devicePixelRatioF())
        self.setPixmap(artwork)


def reveal_file(path):
    """Select the actual transferred file in Explorer/Finder, without a shell."""
    source = Path(path)
    if not source.is_absolute() or not source.exists():
        raise FileNotFoundError('文件已移动或删除，无法在文件夹中显示。')
    if sys.platform == 'win32':
        subprocess.Popen(['explorer.exe', '/select,', str(source)], creationflags=subprocess.CREATE_NO_WINDOW)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', str(source)])
    elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(source.parent))):
        raise OSError('无法打开文件所在的文件夹。')
