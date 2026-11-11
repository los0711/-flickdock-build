import copy
import importlib.util
from pathlib import Path
import unittest

path=Path(__file__).resolve().parents[1]/'scripts/release_downloads.py'
spec=importlib.util.spec_from_file_location('retention',path);retention=importlib.util.module_from_spec(spec);spec.loader.exec_module(retention)


def fixture():
    return dict(schema=1,keepVersions=3,releases=[dict(version='1.8.'+str(patch),r2Removed=False,
        assets=[dict(name=name,bytes=100,sha256='a'*64,archive='https://gitee.com/rooogerluo/flick-dock-windows-releases-2/releases/download/v1.8.'+str(patch)+'/'+name)
                for name in sorted(retention.expected_names('1.8.'+str(patch)))]) for patch in (30,31,32,33,34)])


class RetentionTests(unittest.TestCase):
    def test_keep_three_verify_all_archives_before_deleting_and_never_touch_other_keys(self):
        value=fixture();events=[];saved=[]
        result=retention.prune(value,check_redirect=lambda row,asset:events.append(('redirect',row['version'])),
            check_archive=lambda asset:events.append(('hash',asset['name'])),delete=lambda key:events.append(('delete',key)),save=lambda data:saved.append(copy.deepcopy(data)))
        self.assertEqual(result['retained'],['1.8.34','1.8.33','1.8.32']);self.assertEqual(len(result['removed']),8)
        first=next(i for i,row in enumerate(events) if row[0]=='delete');self.assertEqual(first,16)
        self.assertTrue(all(key.startswith(('v1.8.30/','v1.8.31/')) for key in result['removed']));self.assertEqual(len(saved),2)
        self.assertEqual(retention.plan(value)[1],[])

    def test_missing_redirect_or_wrong_archive_prevents_every_delete(self):
        for stage in ('redirect','hash'):
            with self.subTest(stage=stage):
                events=[]
                def fail(*_):raise ValueError('unverified archive')
                with self.assertRaises(ValueError):retention.prune(fixture(),check_redirect=fail if stage=='redirect' else lambda *_:None,
                    check_archive=fail if stage=='hash' else lambda *_:None,delete=events.append,save=lambda *_:None)
                self.assertEqual(events,[])

    def test_registry_rejects_incomplete_foreign_or_changed_published_installers(self):
        value=fixture();row=value['releases'][-1]
        groups=[dict(assets=[dict(name=a['name'],bytes=a['bytes'],sha256=a['sha256'],url=a['archive']) for a in row['assets']])]
        self.assertEqual(retention.register(value,groups),value)
        with self.assertRaises(ValueError):retention.register(value,[dict(assets=groups[0]['assets'][:1])])
        changed=copy.deepcopy(groups);changed[0]['assets'][0]['sha256']='b'*64
        with self.assertRaises(ValueError):retention.register(value,changed)
        bad=fixture();bad['releases'][0]['assets'][0]['archive']='https://evil.invalid/a'
        with self.assertRaises(ValueError):retention.plan(bad)

    def test_numeric_order_and_current_two_versions_need_no_deletion(self):
        value=fixture();value['releases']=value['releases'][:2]
        self.assertEqual([row['version'] for row in retention.plan(value)[0]],['1.8.31','1.8.30'])
        self.assertEqual(retention.plan(value)[1],[])


if __name__=='__main__':unittest.main()
