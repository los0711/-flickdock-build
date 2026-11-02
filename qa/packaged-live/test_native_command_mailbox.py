"""Portable isolated mailbox checks; never start Qt or native OS input."""
import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('origin_qa',Path(__file__).resolve().parents[2]/'ci_clipboard_origin.py')
qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)


class MailboxTests(unittest.TestCase):
    def sharing(self,code=5):
        error=PermissionError('synthetic Windows sharing contention');error.winerror=code;return error

    def test_one_payload_and_id_survive_transient_native_reader_contention(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'command.json';target.write_text('{"id":4}')
            payload=dict(id=5,action='shortcut',text='synthetic original')
            native=Path.replace;observed=[]
            def replace(source,destination):
                observed.append((source.read_bytes(),target.read_bytes()))
                if len(observed)<3:raise self.sharing()
                return native(source,destination)
            with patch.object(qa.Path,'replace',new=replace),patch.object(qa.time,'sleep'):
                qa.publish_json(target,payload)
            self.assertEqual(json.loads(target.read_text()),payload)
            self.assertEqual(len(observed),3);self.assertEqual(len(set(item[0] for item in observed)),1)
            self.assertTrue(all(item[1]==b'{"id":4}' for item in observed))
            self.assertFalse(target.with_suffix('.tmp').exists())

    def test_persistent_sharing_error_is_bounded_and_preserves_old_mailbox(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'command.json';target.write_text('{"id":4}')
            now=[0.]
            def sleep(seconds):now[0]+=seconds
            with patch.object(qa.Path,'replace',side_effect=self.sharing(32)) as replace, \
                    patch.object(qa.time,'monotonic',side_effect=lambda:now[0]),patch.object(qa.time,'sleep',side_effect=sleep):
                with self.assertRaises(PermissionError):qa.publish_json(target,dict(id=5),timeout=.025)
            self.assertLessEqual(replace.call_count,4)
            self.assertEqual(target.read_text(),'{"id":4}')
            self.assertEqual(json.loads(target.with_suffix('.tmp').read_text()),dict(id=5))

    def test_unrelated_permission_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(qa.Path,'replace',side_effect=PermissionError('read-only')) as replace,patch.object(qa.time,'sleep') as sleep:
                with self.assertRaises(PermissionError):qa.publish_json(Path(folder)/'ack.json',dict(id=5))
                replace.assert_called_once();sleep.assert_not_called()


if __name__=='__main__':unittest.main()
