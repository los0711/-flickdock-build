"""Select the packaged native Windows media plugin before importing Qt."""
import os
import sys
if sys.platform == 'win32':
    os.environ['QT_MEDIA_BACKEND']='windows'
elif sys.platform == 'darwin':
    os.environ['QT_MEDIA_BACKEND']='darwin'
