"""Recover an existing benchmark job; never starts Blender or another sandbox."""
import datetime as dt
import json
import time
from pathlib import Path
import render

ROOT=Path(__file__).resolve().parent
SANDBOX='5115d5e6-729d-48e3-8328-c7f95b31aae6'
OUT=ROOT/'recovery_logs'
OUT.mkdir(exist_ok=True)

def say(s):
    print(s,flush=True)
    with (OUT/'recovery_runner.log').open('a',encoding='utf8') as f: f.write(s+'\n')

def retry(fn):
    for attempt in range(6):
        try: return fn()
        except Exception as exc:
            say(f'Transport retry {attempt+1}: {type(exc).__name__}')
            if attempt==5: raise
            time.sleep(8)

def main():
    d=render.client(render.load_env())
    sb=retry(lambda:d.get(SANDBOX))
    if (sb.labels or {}).get(render.RUN_KEY)!=render.run_label():
        raise RuntimeError('Sandbox does not belong to this benchmark run')
    session=retry(lambda:sb.process.get_session('job',request_timeout=30))
    jobs=[c for c in session.commands if 'build_tortilla.py' in c.command]
    if len(jobs)!=1: raise RuntimeError('Expected exactly one original Blender command')
    cmd=jobs[0]
    say(f'Recovering existing command {cmd.id}; no new Blender execution')
    for poll in range(90):
        cmd=retry(lambda:sb.process.get_session_command('job',cmd.id,request_timeout=30))
        if cmd.exit_code is not None: break
        say('Original job still running; waiting for its existing output')
        time.sleep(10)
    else: raise RuntimeError('Original job did not finish during recovery window')
    logpath=OUT/'attempt_02_full_stdout.log'
    retry(lambda:sb.fs.download_file('/tmp/cloud.log',str(logpath),1800))
    say(f'Original Blender exit code: {cmd.exit_code}')
    say(logpath.read_text(encoding='utf8',errors='replace')[-3500:])
    result={'sandbox':SANDBOX,'command_id':cmd.id,'original_command':cmd.command,
            'exit_code':cmd.exit_code,'recovered_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
            'new_blender_executions':0,'full_stdout':logpath.relative_to(ROOT).as_posix(),'files':[]}
    if cmd.exit_code==0:
        for name in ('run_summary.json','simulation.json','tortilla.png','tortilla.blend'):
            rel='iterations/02/'+name
            dest=ROOT/rel
            tmp=dest.with_name(dest.name+'.download')
            retry(lambda:sb.fs.download_file('/workspace/'+rel,str(tmp),1800))
            tmp.replace(dest)
            result['files'].append(rel)
            say(f'Recovered {rel}: {dest.stat().st_size} bytes')
    (OUT/'attempt_02_recovery.json').write_text(json.dumps(result,indent=2)+'\n')
    gone=retry(lambda:render.destroy(d,sb))
    result['sandbox_destroyed_verified']=gone
    (OUT/'attempt_02_recovery.json').write_text(json.dumps(result,indent=2)+'\n')
    say(f'Existing sandbox destroyed and verified: {gone}')
    if cmd.exit_code!=0 or not gone: raise SystemExit(1)

if __name__=='__main__': main()
