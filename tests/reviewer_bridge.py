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
            'aiUrl': capability['ai_url'], 'coreUrl': capability['core_url'], 'sdkUrl': capability['sdk_url'], 'telemetryUrl': capability['telemetry_url'], 'resolverUrl': capability['resolver_url'],
            'report': build_report(packet),
            'reportDir': str(reviewer.paths(home)['reports']),
            'input': {'cwd': str(home), 'agentDir': str(home / '.omp'), 'sessionDir': str(home / 'data' / 'observability' / 'reviewer' / run['review_run_id'] / 'omp'), 'reviewRunId': run['review_run_id'], 'receipt': {**run, 'omp_version': capability['omp_version']}, 'reviewPrompt': prompt, 'modelSelector': 'openai/reviewer-fixture'},
        }), flush=True)
        reviewer_runner.serve_broker(home, run['review_run_id'], sys.stdin, sys.stdout)
