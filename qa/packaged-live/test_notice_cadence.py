"""Portable controller closure checks; no native clipboard/notice success claims."""
import ast,hashlib,os,types,unittest
from pathlib import Path
from unittest.mock import Mock

CONTROLLER=Path(__file__).with_name('ci_packaged_windows_live.py')

class NoticeCadenceTests(unittest.TestCase):
    def fixture(self,second_duration=.1,*,extra_post=False,wrong_latest=False,wrong_native=False):
        source=ast.parse(CONTROLLER.read_text(encoding='utf8'))
        node=next(n for n in ast.walk(source) if isinstance(n,ast.FunctionDef) and n.name=='real_notice_stack')
        events=[];copies=[];now=[0.];heads=[];rows=[]
        def sleep(seconds):events.append(('sleep',seconds));now[0]+=seconds
        clock=types.SimpleNamespace(monotonic=lambda:now[0],sleep=sleep)
        def copied(text,*,already_focused=False):
            events.append(('copy',len(copies)));copies.append((text,already_focused))
            now[0]+=.1 if len(copies)==1 else second_duration
            head=dict(id='synthetic-head-'+str(len(copies)),sha256=hashlib.sha256(text.encode()).hexdigest())
            heads.append(head);rows.append(dict(method='POST',path='/v1/clipboard'))
            if extra_post and len(copies)==2:rows.append(dict(method='POST',path='/v1/clipboard'))
            return 0,head
        driver=types.SimpleNamespace(click=Mock(),tree=Mock(side_effect=AssertionError('UIA tree blocks real copy cadence')),
            inspect=lambda:dict(text='wrong native text' if wrong_native else copies[-1][0]))
        cloud=types.SimpleNamespace(origin='http://local-fixture.invalid',session={'token':'synthetic'},
                                    stats=lambda:dict(rows=list(rows)))
        def request(*args,**kwargs):return dict(clipboard=dict(id='wrong') if wrong_latest else heads[-1])
        namespace=dict(driver=driver,producer=types.SimpleNamespace(pid=77),cloud=cloud,
                       copy_and_committed=copied,time=clock,os=os,json_request=request,
                       wait=lambda action,**kwargs:action())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(CONTROLLER),'exec'),namespace)
        return namespace['real_notice_stack'],events,copies,driver

    def test_no_uia_roundtrip_between_real_distinct_copies_and_gap_is_recorded(self):
        run,events,copies,driver=self.fixture();result=run()
        self.assertEqual(events,[('copy',0),('sleep',.28),('copy',1),('sleep',3.1)])
        self.assertEqual(len(copies),2);self.assertNotEqual(copies[0][0],copies[1][0])
        self.assertTrue(all(already for _,already in copies));driver.tree.assert_not_called()
        driver.click.assert_called_once_with('Synthetic external editor',pid=77)
        self.assertAlmostEqual(result['inter_commit_seconds'],.38)
        self.assertEqual(result['post_delta'],2);self.assertFalse(result['observer_generated_notice'])
        self.assertEqual(result['manual_overlap_and_fade_review'],'required')
        self.assertNotIn('actual_notice_bounds',result['commits'][0])

    def test_slow_commit_cadence_fails_instead_of_claiming_stack_coverage(self):
        run,events,_,driver=self.fixture(second_duration=1.5)
        with self.assertRaisesRegex(AssertionError,'HARNESS_NOTICE_COPY_CADENCE'):run()
        driver.tree.assert_not_called();self.assertNotIn(('sleep',3.1),events)

    def test_original_post_latest_head_and_native_clipboard_checks_are_kept(self):
        for name,options,reason in (
            ('extra-post',dict(extra_post=True),'extra upload tasks'),
            ('wrong-head',dict(wrong_latest=True),'latest real head'),
            ('wrong-native',dict(wrong_native=True),'Native clipboard changed')):
            with self.subTest(name=name):
                run,_,_,_=self.fixture(**options)
                with self.assertRaisesRegex(AssertionError,reason):run()

    def test_native_copy_helper_still_uses_exact_committed_payload_and_os_copy(self):
        source=ast.parse(CONTROLLER.read_text(encoding='utf8'))
        node=next(n for n in ast.walk(source) if isinstance(n,ast.FunctionDef) and n.name=='copy_and_committed')
        text=ast.unparse(node)
        self.assertIn('exact_ready_text_head(head, text, previous_id)',text)
        self.assertIn("driver.command('text', text=text)",text)
        self.assertIn("driver.hotkey('^a', pid=producer.pid)",text)
        self.assertIn("driver.hotkey('^c', pid=producer.pid)",text)
        self.assertNotIn('show_result',text);self.assertNotIn('schedule_result',text)

if __name__=='__main__':unittest.main()
