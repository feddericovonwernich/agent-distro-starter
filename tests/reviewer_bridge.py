import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import reviewer, reviewer_runner
from support import lab
from test_reviewer import build_report, produced_run

with lab() as (home, _):
    with reviewer.run_lease(home):
        run, packet = produced_run(home)
        capability = reviewer_runner.assert_runtime_capability()
        prompt = json.loads((reviewer.paths(home)['manifests'] / f"{run['instruction_snapshot_ref']}.json").read_text())['text']
        print(json.dumps({
            'aiUrl': capability['ai_url'], 'coreUrl': capability['core_url'], 'sdkUrl': capability['sdk_url'], 'telemetryUrl': capability['telemetry_url'],
            'report': build_report(packet),
            'reportDir': str(reviewer.paths(home)['reports']),
            'input': {'cwd': str(home), 'agentDir': str(home / '.omp'), 'sessionDir': str(home / 'data' / 'observability' / 'reviewer' / run['review_run_id'] / 'omp'), 'reviewRunId': run['review_run_id'], 'receipt': run, 'reviewPrompt': prompt,
                      'model': {'provider': 'openai', 'api': 'openai-completions', 'id': 'reviewer-fixture', 'name': 'Reviewer fixture', 'baseUrl': 'http://127.0.0.1:1', 'reasoning': False, 'input': ['text'], 'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}, 'contextWindow': 8192, 'maxTokens': 1024}},
        }), flush=True)
        reviewer_runner.serve_broker(home, run['review_run_id'], sys.stdin, sys.stdout)
