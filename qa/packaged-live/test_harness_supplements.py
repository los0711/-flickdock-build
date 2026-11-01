"""Supplement ordering and isolated worker deadlines, no desktop input."""
import importlib.util,json,unittest
from pathlib import Path
from unittest.mock import Mock
folder=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('supplement_controller',folder/'ci_packaged_windows_live.py')
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)

class SupplementalTests(unittest.TestCase):
    def test_original_routes_remain_in_order_and_supplements_precede_destructive_cleanup(self):
        plans=[json.loads((folder/name).read_text(encoding='utf8')) for name in
            ('ui-actions.json','ui-extra-actions.json','ui-batch2-actions.json')]
        supplements=json.loads((folder/'ui-supplement-actions.json').read_text(encoding='utf8'))
        original=qa.compose_actions(*plans);all_actions=qa.compose_actions(*plans,supplements)
        self.assertEqual([r for r in all_actions if r not in supplements],original)
        self.assertEqual(all_actions[-1]['recipe'],'empty-zones-last')
        self.assertEqual(all_actions[-len(supplements)-1:-1],supplements)
        self.assertEqual(len({a['id'] for a in all_actions}),len(all_actions))
        self.assertEqual({r['recipe'] for r in supplements},{'tray-menu-only','recent-device-delete','open-original-text','external-file-shake'})
        self.assertTrue(all('passed' not in row for row in supplements))

    def test_supplements_cannot_run_without_the_original_fixture_cleanup_contract(self):
        with self.assertRaises(AssertionError):qa.compose_actions([],[],[],[dict(kind='batch3')])

    def test_new_input_routes_use_the_existing_killable_worker_with_finite_budgets(self):
        driver=qa.Driver.__new__(qa.Driver);driver._rpc=Mock(return_value={'events':[]})
        driver.batch3('tray-menu-only')
        driver._rpc.assert_called_with('batch3',seconds=55,recipe='tray-menu-only')
        driver.shake('external-file-shake')
        driver._rpc.assert_called_with('shake',seconds=45,recipe='external-file-shake')

if __name__=='__main__':unittest.main()
