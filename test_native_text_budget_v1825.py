"""Original text analysis retains bytes, not an out-of-policy decoded copy."""
import hashlib
import struct
import tracemalloc
import unittest
from win_clipboard_native import _text_parts

TEST_LIMIT_BYTES=4*1024*1024
CHUNK_BYTES=262144
TRANSIENT_BUDGET_BYTES=8*CHUNK_BYTES


def expected_fingerprint(text,html=b''):
    return hashlib.sha256(struct.pack('<QQ',len(text),len(html))+
        hashlib.sha256(text).digest()+hashlib.sha256(html).digest()).hexdigest()


class NativeTextBudgetTests(unittest.TestCase):
    def test_provably_oversized_utf16_does_not_retain_a_limit_sized_decoded_copy(self):
        raw=b'x\0'*(TEST_LIMIT_BYTES+1)+b'\0\0'
        tracemalloc.start()
        try:
            value,size,fingerprint,end=_text_parts(raw,None,TEST_LIMIT_BYTES)
            peak=tracemalloc.get_traced_memory()[1]
        finally:tracemalloc.stop()
        text=b'x'*(TEST_LIMIT_BYTES+1)
        self.assertIsNone(value);self.assertEqual(size,len(text));self.assertEqual(end,len(raw))
        self.assertEqual(fingerprint,expected_fingerprint(text))
        self.assertLess(peak,TRANSIENT_BUDGET_BYTES,
            'The already ineligible original must be analyzed in bounded chunks without retaining a full decoded copy')

    def test_exact_budget_still_preserves_complete_inline_text(self):
        text='原件\U0001f642'+('x'*4096)
        encoded=text.encode('utf8');raw=text.encode('utf-16-le')+b'\0\0'
        value,size,fingerprint,end=_text_parts(raw,None,len(encoded))
        self.assertEqual(value,dict(text=text,html=''));self.assertEqual(size,len(encoded))
        self.assertEqual(end,len(raw));self.assertEqual(fingerprint,expected_fingerprint(encoded))

    def test_oversized_input_still_validates_all_unicode(self):
        # Even after the lower bound proves ineligibility, malformed original
        # bytes must not acquire a valid fingerprint or cache descriptor.
        raw=b'x\0'*(TEST_LIMIT_BYTES+1)+b'\0\xd8'+b'\0\0'
        with self.assertRaises(UnicodeError):_text_parts(raw,None,TEST_LIMIT_BYTES)

    def test_utf8_and_html_still_contribute_complete_logical_identity(self):
        text=('同一原件\U0001f642'*40000).encode('utf8');html=b'<p>full formatted original</p>'
        header=b'StartHTML:0000000000\r\nEndHTML:0000000000\r\n'
        header=f'StartHTML:{len(header):010d}\r\nEndHTML:{len(header)+len(html):010d}\r\n'.encode('ascii')
        value,size,fingerprint,end=_text_parts(text,header+html,1024,encoding='utf-8')
        self.assertIsNone(value);self.assertEqual(size,len(text)+len(html));self.assertEqual(end,len(text))
        self.assertEqual(fingerprint,expected_fingerprint(text,html))


if __name__=='__main__':unittest.main()
