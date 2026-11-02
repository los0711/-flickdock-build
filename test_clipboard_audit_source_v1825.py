"""Known write provenance must never inspect a replacement clipboard provider."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import QMimeData
import clipboard_audit as subject


class Clipboard:
    def __init__(self, fail=False):self.reads=0;self.writes=[];self.fail=fail
    def mimeData(self):
        self.reads+=1
        raise AssertionError('Diagnostic read entered foreign provider')
    def write(self,value):
        self.writes.append(value)
        if self.fail:raise RuntimeError('original writer failure')
    def setText(self,value):self.write(value)
    def setMimeData(self,value):self.write(value)
    def clear(self):self.write(None)


class AuditSourceTests(unittest.TestCase):
    def test_diagnostic_failure_is_counted_without_changing_writer_result(self):
        with tempfile.TemporaryDirectory() as directory:
            audit=subject.ClipboardAudit(directory);clip=Clipboard()
            context=dict(module='test',function='copy',trigger='button',origin='internal')
            with patch.object(subject,'_audit',audit),patch.object(audit,'record',side_effect=RuntimeError('diagnostic bug')):
                subject.set_text(clip,'synthetic',**context)
            self.assertEqual(audit.dropped,1)
            self.assertEqual(clip.writes,['synthetic'])
            self.assertEqual(clip.reads,0)
            audit.close()

    def test_known_success_and_failure_never_read_back_external_provider(self):
        context=dict(module='test',function='copy',trigger='button',origin='internal')
        for fail in (False,True):
            for kind in ('text','mime','clear'):
                with self.subTest(fail=fail,kind=kind),tempfile.TemporaryDirectory() as directory:
                    audit=subject.ClipboardAudit(directory);clip=Clipboard(fail)
                    mime=QMimeData();mime.setText('private synthetic content')
                    action=lambda:subject.clear(clip,**context)
                    if kind=='text':action=lambda:subject.set_text(clip,'private synthetic content',**context)
                    elif kind=='mime':action=lambda:subject.set_mime(clip,mime,**context)
                    with patch.object(subject,'_audit',audit):
                        if fail:
                            with self.assertRaisesRegex(RuntimeError,'original writer failure'):action()
                        else:action()
                    audit.close(2)
                    self.assertEqual(clip.reads,0)
                    self.assertEqual(len(clip.writes),1)
                    rows=[json.loads(row) for row in audit.path.read_text().splitlines()]
                    self.assertEqual(len(rows),1)
                    self.assertEqual(rows[0]['result'],'failed' if fail else 'ok')
                    self.assertEqual(rows[0]['content_type'],'empty' if kind=='clear' else 'text')
                    self.assertNotIn('private synthetic',audit.path.read_text())

    def test_shared_signature_is_reused_without_describing_owned_mime(self):
        with tempfile.TemporaryDirectory() as directory:
            audit=subject.ClipboardAudit(directory);clip=Clipboard()
            with patch.object(subject,'_audit',audit),patch.object(audit,'describe',side_effect=AssertionError('duplicate hash')):
                subject.set_mime(clip,object(),module='test',function='apply',trigger='receive',origin='cloud',content_type='files',fingerprint_data=('files','synthetic-digest'))
            audit.close(2)
            self.assertEqual(clip.reads,0)
            self.assertEqual(len(audit.path.read_text().splitlines()),1)


if __name__=='__main__':unittest.main()
