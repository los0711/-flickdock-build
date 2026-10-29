"""Select the packaged native Windows media plugin before importing Qt."""
import os
import sys
import codecs
# Resolve this codec on the entry thread before any DNS/TLS worker starts.
# Concurrent first lookup can fail in a frozen interpreter despite inclusion.
codecs.lookup('idna')
'flickdock.local'.encode('idna')
if sys.platform == 'win32':
    os.environ['QT_MEDIA_BACKEND']='windows'
elif sys.platform == 'darwin':
    os.environ['QT_MEDIA_BACKEND']='darwin'
