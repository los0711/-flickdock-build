"""Execute exact source AST gates, without importing hosted/native entry points."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

HERE=Path(__file__).parent


def parsed(name):return ast.parse((HERE/name).read_text(encoding='utf8'))


def expression(node,**values):
    return eval(compile(ast.fix_missing_locations(ast.Expression(copy.deepcopy(node))),'<actual QA gate>','eval'),values)


class ScopeGates(unittest.TestCase):
    def test_installer_actual_predicate_rejects_observation_even_when_ok_and_hash_match(self):
        tree=parsed('ci_exact_installer.py')
        nodes=[n for n in ast.walk(tree) if isinstance(n,ast.If) and 'normal.get' in ast.unparse(n.test)]
        self.assertEqual(len(nodes),1);gate=nodes[0].test
        valid=dict(ok=True,normal_entry=True,exe_sha256='a'*64,verify_switches=False,frozen_executable_patched=False)
        self.assertFalse(expression(gate,normal=valid,expected='a'*64))
        for changed in ({'observation_only':True},{'installer_acceptance':False},
                        {'observation_only':True,'installer_acceptance':False},
                        {'ok':False},{'exe_sha256':'b'*64}):
            with self.subTest(changed=changed):self.assertTrue(expression(gate,normal={**valid,**changed},expected='a'*64))

    def test_no_early_main_return_can_override_cleanup_failure_exit_code(self):
        main=next(n for n in parsed('ci_packaged_windows_live.py').body if isinstance(n,ast.FunctionDef) and n.name=='main')
        outer=next(n for n in main.body if isinstance(n,ast.Try))
        class OwnReturns(ast.NodeVisitor):
            def __init__(self):self.rows=[]
            def visit_FunctionDef(self,node):pass # nested action callbacks may return normally.
            def visit_Return(self,node):self.rows.append(node)
        visitor=OwnReturns();visitor.visit(outer)
        self.assertEqual(visitor.rows,[],'A try-body return freezes exit0 before cleanup can report failure')
        last=main.body[-1];self.assertIsInstance(last,ast.Return)
        self.assertEqual(expression(last.value,report={'ok':True}),0)
        self.assertEqual(expression(last.value,report={'ok':False}),1)
        # Execute the actual cloud cleanup handler with an injected close error,
        # then execute the actual final exit expression. No OS process is used.
        cleanup=next(n for n in outer.finalbody if isinstance(n,ast.If) and ast.unparse(n.test)=='cloud is not None')
        def close():raise RuntimeError('synthetic cleanup failure')
        values={'report':{'ok':True},'cloud':SimpleNamespace(close=close),'phase':lambda label,action:action()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(cleanup)],type_ignores=[])),'<actual cleanup>','exec'),values)
        self.assertFalse(values['report']['ok']);self.assertEqual(expression(last.value,**values),1)

    def test_writer_flag_excludes_each_full_action_argument(self):
        main=next(n for n in parsed('ci_packaged_windows_live.py').body if isinstance(n,ast.FunctionDef) and n.name=='main')
        guard=next(n for n in main.body if isinstance(n,ast.If) and 'args.writer_observation and' in ast.unparse(n.test))
        fields=('actions','extra_actions','batch2_actions','supplement_actions')
        for name in fields:
            data=dict.fromkeys(fields,None);data[name]='synthetic-manifest'
            self.assertTrue(expression(guard.test,args=SimpleNamespace(writer_observation=True,**data)))
            self.assertFalse(expression(guard.test,args=SimpleNamespace(writer_observation=False,**data)))
        self.assertFalse(expression(guard.test,args=SimpleNamespace(writer_observation=True,**dict.fromkeys(fields,None))))

    def test_observation_does_not_claim_full_group_or_publication_eligibility(self):
        main=next(n for n in parsed('ci_packaged_windows_live.py').body if isinstance(n,ast.FunctionDef) and n.name=='main')
        writer=next(n for n in ast.walk(main) if isinstance(n,ast.If) and ast.unparse(n.test)=='args.writer_observation')
        updates=[n for n in ast.walk(writer) if isinstance(n,ast.Call) and ast.unparse(n.func)=='report.update']
        self.assertEqual(len(updates),1)
        values={x.arg:ast.literal_eval(x.value) for x in updates[0].keywords}
        self.assertIs(values['observation_only'],True);self.assertIs(values['installer_acceptance'],False)
        gates=[n for n in ast.walk(main) if isinstance(n,ast.Call) and ast.unparse(n.func)=='dict' and any(x.arg=='implementation_complete' for x in n.keywords)]
        self.assertEqual(len(gates),1)
        values={x.arg:ast.literal_eval(x.value) for x in gates[0].keywords if x.arg in ('implementation_complete','all_requested_changes_complete','publication_authorized')}
        self.assertEqual(values,dict(implementation_complete=False,all_requested_changes_complete=False,publication_authorized=False))
        full=next(n for n in ast.walk(main) if isinstance(n,ast.If) and ast.unparse(n.test)=='not args.writer_observation')
        self.assertIn('compose_actions',ast.unparse(full));self.assertIn('final-real-distinct-notice-stack',ast.unparse(full))

    def test_both_workflow_installer_steps_explicitly_exclude_writer_scope(self):
        workflow=(HERE.parents[1]/'.github/workflows/native-clipboard.yml').read_text(encoding='utf8')
        for name in ('Ensure native installer compiler for same-byte validation','Verify exact production installer with a real old process and relaunch'):
            block=workflow.split('- name: '+name,1)[1].split('- name:',1)[0]
            guard=next(line for line in block.splitlines() if line.strip().startswith('if:'))
            self.assertIn("normal_actions_scope != 'writer-observation'",guard)
            self.assertIn("installer_acceptance == 'true'",guard)
        inputs=workflow.split('      installer_acceptance:',1)[1].split('      reuse_normal_run:',1)[0]
        self.assertIn('type: boolean',inputs);self.assertIn('default: false',inputs)


if __name__=='__main__':unittest.main()
