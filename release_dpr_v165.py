"""Check startup/terminal animation boundaries across display scaling."""
from concurrent.futures import ThreadPoolExecutor
import json,os,subprocess,sys
from pathlib import Path

root=Path(__file__).resolve().parent
def check(dpr):
    checks=[]
    env=dict(os.environ,FLICKDOCK_TEST_DPR=dpr)
    for name in ('test_expand_flash_v165.py','test_motion_v163.py'):
        result=subprocess.run([sys.executable,str(root/name)],env=env,capture_output=True,
                              encoding='utf8',errors='replace',timeout=25)
        (root/f'{name}-dpr-{dpr}.log').write_text(result.stdout+result.stderr,encoding='utf8')
        checks.append({'dpr':dpr,'test':name,'exit_code':result.returncode})
    return checks
with ThreadPoolExecutor(max_workers=4) as pool:
    results=[item for group in pool.map(check,('1','1.25','1.5','2')) for item in group]
(root/'release-dpr-v165.json').write_text(json.dumps(results,indent=2),encoding='utf8')
frames=[json.loads((root/f'expand-flash-v165-dpr-{dpr}.json').read_text(encoding='utf8')) for dpr in ('1','1.25','1.5','2')]
(root/'expand-flash-v165.json').write_text(json.dumps({'cases':frames,'scope':'Offscreen natural and controlled pre-animation paints; no DWM measurement'},indent=2),encoding='utf8')
print(json.dumps(results))
raise SystemExit(any(item['exit_code'] for item in results))
