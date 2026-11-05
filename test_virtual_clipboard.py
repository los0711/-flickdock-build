"""Synthetic Windows descriptors; never opens the user's native clipboard."""
import hashlib
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from PySide6.QtCore import QMimeData
from virtual_clipboard import DESCRIPTOR_MIME,CONTENT_MIME,MAX_FILE_BYTES,describe,materialize,VirtualClipboardError


def fixture(name,body,*,size=None):
    raw=bytearray(596);struct.pack_into('<II',raw,0,1,0x40)
    size=len(body) if size is None else size
    struct.pack_into('<II',raw,68,size>>32,size&0xffffffff)
    encoded=name.encode('utf-16-le');raw[76:76+len(encoded)]=encoded
    mime=QMimeData();mime.setData(DESCRIPTOR_MIME,raw);mime.setData(CONTENT_MIME,body)
    return mime


class VirtualClipboardTests(unittest.TestCase):
    def test_virtual_file_bytes_and_unicode_name_materialize_without_a_local_source_path(self):
        body=b'actual virtual file bytes\0'*8192;mime=fixture('跨设备文件.zip',body);descriptor=describe(mime)
        with TemporaryDirectory() as directory:
            result=materialize(mime,directory,dict(sequence=10,descriptor=descriptor.sha256),lambda:10)
            self.assertEqual((Path(directory)/'跨设备文件.zip').read_bytes(),body)
            self.assertEqual(result['sha256'],hashlib.sha256(body).hexdigest())
            self.assertFalse(mime.hasUrls())

    def test_oversize_descriptor_is_rejected_before_requesting_file_contents(self):
        mime=fixture('too-large.bin',b'',size=MAX_FILE_BYTES)
        with patch.object(mime,'data',wraps=mime.data) as read:
            with self.assertRaisesRegex(VirtualClipboardError,'SIZE_LIMIT'):describe(mime)
            self.assertEqual([call.args[0] for call in read.call_args_list],[DESCRIPTOR_MIME])

    def test_replacement_during_delayed_rendering_never_publishes_the_previous_file(self):
        mime=fixture('file.bin',b'old');descriptor=describe(mime);sequences=iter((10,11))
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VirtualClipboardError,'REPLACED'):
                materialize(mime,directory,dict(sequence=10,descriptor=descriptor.sha256),lambda:next(sequences))
            self.assertFalse(list(Path(directory).iterdir()))

    def test_invalid_paths_and_wrong_lengths_are_rejected_without_partial_output(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VirtualClipboardError,'INVALID_NAME'):describe(fixture('../outside.bin',b'bad'))
            mime=fixture('short.bin',b'bad',size=10);descriptor=describe(mime)
            with self.assertRaisesRegex(VirtualClipboardError,'INCOMPLETE'):
                materialize(mime,directory,dict(sequence=10,descriptor=descriptor.sha256),lambda:10)
            self.assertFalse(list(Path(directory).iterdir()))


if __name__=='__main__':unittest.main()
