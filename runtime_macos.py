"""Select Qt's AVFoundation backend before QtMultimedia is imported."""
import os
import sys
if sys.platform == 'darwin':
    os.environ['QT_MEDIA_BACKEND'] = 'darwin'
    os.environ['QT_MAC_SET_RAISE_PROCESS'] = '0'
