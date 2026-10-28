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
rpc("model",{"id":"fixture/model"}); rpc("session",{"id":"actual-session"}); rpc("packet",{})
rpc("reviewer_submit",{"report":{"schema":1,"summary":"fixture","findings":[]}})
r=rpc("result",{})
MODE=''' + repr(mode) + '''
if MODE=="mismatch": r={"reportId":"wrong"}
if MODE!="absent": print(json.dumps({"complete":True,"result":r}),flush=True)
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