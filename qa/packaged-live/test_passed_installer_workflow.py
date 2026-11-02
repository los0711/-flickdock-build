"""The installer-only lane requires an actual previously passed full receipt."""
import re,unittest
from pathlib import Path
from types import SimpleNamespace as NS


class PassedInstallerWorkflowTests(unittest.TestCase):
    def setUp(self):
        text=(Path(__file__).resolve().parents[2]/'.github/workflows/native-clipboard.yml').read_text()
        self.block=re.search(r'^  passed-installer:\n(.*?)(?=^  [a-z][a-z-]+:\n|\Z)',text,re.M|re.S).group(1)
        self.guard=next(line.strip()[4:] for line in self.block.splitlines() if line.strip().startswith('if: '))

    def allows(self,**changed):
        inputs=dict(installer_acceptance='true',normal_frozen='false',native_checks='false',
                    cloud_contract='false',normal_actions_scope='full',reuse_normal_run='41414776631')
        repository=changed.pop('repository','los0711/-flickdock-build')
        event_name=changed.pop('event_name','workflow_dispatch');inputs.update(changed)
        return eval(self.guard.replace('&&','and'),{'__builtins__':{}},
                    {'github':NS(repository=repository,event_name=event_name,event=NS(inputs=NS(**inputs)))})

    def test_only_explicit_installer_full_reuse_lane_can_run(self):
        self.assertTrue(self.allows())
        for change in ({'installer_acceptance':'false'},{'normal_frozen':'true'},{'native_checks':'true'},
                       {'cloud_contract':'true'},{'reuse_normal_run':''},{'normal_actions_scope':'writer-observation'},
                       {'normal_actions_scope':'recent-observation'},{'normal_actions_scope':'tray-observation'},
                       {'event_name':'push'},{'repository':'different/repository'}):
            with self.subTest(change=change):self.assertFalse(self.allows(**change))

    def test_original_receipt_and_current_frozen_source_are_required_before_real_installer(self):
        steps=('verify_frozen_source.py --before-build','reuse_frozen.py --run $env:REUSE_NORMAL_RUN --require-passed-normal',
               'verify_frozen_source.py --after-build','ci_exact_installer.py --root $PWD')
        indices=[self.block.index(step) for step in steps];self.assertEqual(indices,sorted(indices))
        self.assertIn('--normal-report build/passed-normal-report.json',self.block)
        self.assertNotIn('ci_packaged_windows_live.py',self.block)
        self.assertIn("FD_QA_NATIVE: '1'",self.block)
        self.assertIn('build/exact-installer/',self.block)
        self.assertIn('build/passed-normal-source-verification.json',self.block)
        self.assertGreaterEqual(self.block.count('if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }'),5)


if __name__=='__main__':unittest.main()
