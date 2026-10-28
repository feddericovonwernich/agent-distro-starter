import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import reviewer, reviewer_runner
from support import lab
from test_reviewer import ended_worker, message


def fake_child(path, mode):
    path.write_text('''#!/usr/bin/env python3
import json,sys,uuid
payload=json.loads(sys.stdin.readline())
def rpc(op,args):
 i=str(uuid.uuid4()); print(json.dumps({"id":i,"op":op,"args":args}),flush=True); r=json.loads(sys.stdin.readline());
 if not r.get("ok"): raise SystemExit(3)
 return r["value"]
if "baseUrl" in json.dumps(payload) or "secret" in json.dumps(payload): raise SystemExit(4)
rpc("model",{"id":"fixture/model"}); rpc("session",{"id":"actual-session"})
packet=json.loads(rpc("packet",{}))
MODE=''' + repr(mode) + '''
if MODE=="timeout":
 import subprocess,time,os
 rpc("reserve",{})
 kid=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])
 open(payload["input"]["cwd"]+"/fake-pids","w").write(str(os.getpid())+" "+str(kid.pid))
 time.sleep(30)
 raise SystemExit
report={"schema":1,"summary":"fixture","findings":[]}
if MODE=="finding-mismatch":
 source=packet["excerpts"][0]["source_id"]
 report["findings"]=[{"kind":"new-skill","target":"fixture-workflow","finding":"fixture finding","applicability":"fixture","citations":[source],"counterexamples":[],"uncertainty":"fixture","resolution":"fixture","validation":"fixture","validation_limits":[],"new_skill":{"id":"fixture-skill","draft":"---\\nname: fixture-skill\\ndescription: fixture\\n---\\nfixture\\n","triggers":["fixture"],"scope":"fixture","prerequisites":[]}}]
rpc("reviewer_submit",{"report":report})
r=rpc("result",{})
if MODE in ("mismatch","finding-mismatch"): r={"reportId":"wrong"}
if MODE!="absent": print(json.dumps({"complete":True,"result":r}),flush=True)
if MODE=="wait":
 import time,os
 open(payload["input"]["cwd"]+"/running","w").write("running")
 while not os.path.exists(payload["input"]["cwd"]+"/release"): time.sleep(.02)
 open(payload["input"]["cwd"]+"/exited","w").write("exited")
if MODE=="duplicate": print(json.dumps({"complete":True,"result":r}),flush=True)
''')
    path.chmod(0o700)


def capability(): return {'omp_version':'fake','ai_url':'file:///ai','core_url':'file:///core','sdk_url':'file:///sdk','telemetry_url':'file:///telemetry','resolver_url':'file:///resolver'}


def test_fake_child_happy_binds_actual_session_and_finalizes(monkeypatch):
    with lab() as (home, _):
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')]); agent=home/'agent'; agent.mkdir(); fake=home/'bun'; fake_child(fake,'happy')
        monkeypatch.setattr(reviewer_runner,'assert_runtime_capability',capability); monkeypatch.setattr(reviewer_runner.shutil,'which',lambda _:str(fake))
        result=reviewer_runner.run_native_reviewer(home,reviewer.enqueue_intent(home,kind='manual'),agent_dir=agent)
        assert result['dispatch_state']=='final' and (home/'data/observability/reviewer'/result['review_run_id']/'binding.json').read_text().find('actual-session')>=0


def test_fake_child_bad_completion_is_terminal(monkeypatch):
    with lab() as (home, _):
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')]); agent=home/'agent'; agent.mkdir(); fake=home/'bun'; fake_child(fake,'mismatch')
        monkeypatch.setattr(reviewer_runner,'assert_runtime_capability',capability); monkeypatch.setattr(reviewer_runner.shutil,'which',lambda _:str(fake))
        result=reviewer_runner.run_native_reviewer(home,reviewer.enqueue_intent(home,kind='manual'),agent_dir=agent)
        assert result['dispatch_state']=='final' and result['outcome']=='failed'

def test_fake_child_absent_or_duplicate_completion_is_terminal(monkeypatch):
    for mode in ('absent', 'duplicate'):
        with lab() as (home, _):
            ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')]); agent=home/'agent'; agent.mkdir(); fake=home/'bun'; fake_child(fake,mode)
            monkeypatch.setattr(reviewer_runner,'assert_runtime_capability',capability); monkeypatch.setattr(reviewer_runner.shutil,'which',lambda _:str(fake))
            result=reviewer_runner.run_native_reviewer(home,reviewer.enqueue_intent(home,kind='manual'),agent_dir=agent)
            assert result['dispatch_state']=='final' and result['outcome']=='failed'

def test_fake_child_timeout_leaves_inflight_unknown_and_kills_group(monkeypatch):
    import os
    from agd_lib import store
    with lab() as (home, _):
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')]); agent=home/'agent'; agent.mkdir(); fake=home/'bun'; fake_child(fake,'timeout')
        config=reviewer.read_config(home); config['limits']['max_runtime_seconds']=1; store.atomic_json(reviewer.paths(home)['config'],config)
        monkeypatch.setattr(reviewer_runner,'assert_runtime_capability',capability); monkeypatch.setattr(reviewer_runner.shutil,'which',lambda _:str(fake))
        result=reviewer_runner.run_native_reviewer(home,reviewer.enqueue_intent(home,kind='manual'),agent_dir=agent)
        assert result['outcome']=='unknown' and result['counters']['provider_inflight']==1
        for pid in map(int,(home/'fake-pids').read_text().split()):
            with __import__('pytest').raises(ProcessLookupError): os.kill(pid,0)

def test_run_lease_holds_until_child_exit_and_finalization(monkeypatch):
    import threading, time
    from agd_lib import store
    with lab() as (home, _):
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')]); agent=home/'agent'; agent.mkdir(); fake=home/'bun'; fake_child(fake,'wait')
        monkeypatch.setattr(reviewer_runner,'assert_runtime_capability',capability); monkeypatch.setattr(reviewer_runner.shutil,'which',lambda _:str(fake))
        seen=[]; original=reviewer.finalize_run
        def finalize(*args,**kwargs): seen.append((home/'exited').exists()); return original(*args,**kwargs)
        monkeypatch.setattr(reviewer,'finalize_run',finalize)
        intent=reviewer.enqueue_intent(home,kind='manual'); thread=threading.Thread(target=lambda: reviewer_runner.run_native_reviewer(home,intent,agent_dir=agent)); thread.start()
        deadline=time.time()+5
        while not (home/'running').exists() and time.time()<deadline: time.sleep(.02)
        assert (home/'running').exists()
        with __import__('pytest').raises(store.Refusal):
            with reviewer.run_lease(home): pass
        (home/'release').write_text('go'); thread.join(5)
        assert not thread.is_alive() and seen == [True]

def test_committed_finding_survives_mismatched_completion_with_one_event(monkeypatch):
    with lab() as (home, _):
        ended_worker(home, 'native', str(uuid.uuid4()), [message('evidence')]); agent=home/'agent'; agent.mkdir(); fake=home/'bun'; fake_child(fake,'finding-mismatch')
        monkeypatch.setattr(reviewer_runner,'assert_runtime_capability',capability); monkeypatch.setattr(reviewer_runner.shutil,'which',lambda _:str(fake))
        result=reviewer_runner.run_native_reviewer(home,reviewer.enqueue_intent(home,kind='manual'),agent_dir=agent)
        assert result['outcome']=='failed' and result['report_ids'] and result['proposal_ids']
        event=list((home/'state'/'events').glob('*.json')); assert len(event)==1
        report=reviewer.report_path(home,result['report_ids'][0]).read_bytes(); triage=reviewer.triage_path(home,result['proposal_ids'][0])
        triage_bytes=triage.read_bytes() if triage.exists() else b''
        assert reviewer.close_run(home,result['review_run_id'],'failed')['event_id']==result['event_id']
        assert len(list((home/'state'/'events').glob('*.json')))==1 and report==reviewer.report_path(home,result['report_ids'][0]).read_bytes() and triage_bytes==(triage.read_bytes() if triage.exists() else b'')