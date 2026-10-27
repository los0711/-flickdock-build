"""Run independent release checks in disposable offscreen profiles."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from release_manifest import MAINTAINED_TESTS

root=Path(__file__).resolve().parent
# The source archive's maintained-test inventory is the release regression
# inventory too. Keep timing-sensitive checks out of the parallel pool.
timing_tests=('test_morph_clock_v164.py','test_gesture_entry_v165.py')
tests=[name for name in MAINTAINED_TESTS if name not in timing_tests]
logs=root/'release-checks-v188';logs.mkdir(exist_ok=True)

def check(name):
    with tempfile.TemporaryDirectory(prefix='flickdock-release-') as profile:
        env=dict(os.environ,QT_QPA_PLATFORM='offscreen',APPDATA=profile)
        try:
            result=subprocess.run([sys.executable,str(root/name)],env=env,cwd=root.parent,
                                  capture_output=True,text=True,encoding='utf8',errors='replace',timeout=90)
            (logs/(name+'.log')).write_text(result.stdout+result.stderr,encoding='utf8')
            return {'test':name,'exit_code':result.returncode}
        except subprocess.TimeoutExpired:
            return {'test':name,'exit_code':124}

with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(check,tests))
# Timing evidence is collected with no other regression process competing for
# CPU; its purpose is to measure the application's own event-loop work.
for name in timing_tests:results.append(check(name))
(logs/'results.json').write_text(json.dumps(results,indent=2),encoding='utf8')
for result in results:print(('PASS' if result['exit_code']==0 else 'FAIL')+' '+result['test'])
raise SystemExit(any(item['exit_code'] for item in results))
