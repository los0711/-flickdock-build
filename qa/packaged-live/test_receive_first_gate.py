"""No desktop input: the hosted receive-first gate cannot be faked by zeros."""
import unittest
from ci_packaged_windows_live import require_virgin_empty_metadata


class VirginGateTests(unittest.TestCase):
    def state(self):
        return dict(clipboard_modified=False,opened=True,closed=True,sequence_before=0,sequence_after=0,format_count=0)

    def test_actual_successful_empty_observation_is_eligible(self):
        require_virgin_empty_metadata(self.state())

    def test_busy_error_nonempty_positive_counter_and_modified_are_rejected(self):
        for key,value in (('opened',False),('closed',False),('sequence_before',1),('sequence_after',1),
                          ('format_count',1),('clipboard_modified',True),('sequence_after',None),('format_count',False)):
            with self.subTest(key=key,value=value):
                state=self.state();state[key]=value
                with self.assertRaises(AssertionError):require_virgin_empty_metadata(state)

    def test_missing_fields_and_unstructured_values_cannot_qualify(self):
        for key in self.state():
            state=self.state();state.pop(key)
            with self.assertRaises(AssertionError):require_virgin_empty_metadata(state)
        for value in (None,[],0):
            with self.assertRaises(AssertionError):require_virgin_empty_metadata(value)


if __name__=='__main__':unittest.main()
