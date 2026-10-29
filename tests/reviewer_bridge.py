import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from agd_lib import observability, omp_runtime, reviewer, reviewer_runner
from support import lab
from test_reviewer import build_report, produced_run

with lab() as (home, _):
    with reviewer.run_lease(home):
        run, packet = produced_run(home)
        capability = reviewer_runner.assert_runtime_capability(int((__import__('time').time() + 10) * 1000))
        prompt = json.loads((reviewer.paths(home)['manifests'] / f"{run['instruction_snapshot_ref']}.json").read_text())['text']
        runtime = reviewer_runner._runtime_boundary(home, run['review_run_id'])
        print(json.dumps({
            'aiUrl': capability['ai_url'], 'coreUrl': capability['core_url'], 'sdkUrl': capability['sdk_url'], 'registryUrl': capability['registry_url'], 'managerUrl': capability['manager_url'], 'sessionUrl': capability['session_url'], 'telemetryUrl': capability['telemetry_url'], 'resolverUrl': capability['resolver_url'],
            'report': build_report(packet),
            'reportDir': str(reviewer.paths(home)['reports']),
            'distroHome': str(home),
            'projectPlugins': json.loads(
                (home/'.omp/plugins/installed_plugins.json').read_text()),
            'reviewerPlugins': json.loads(
                (runtime/'.omp/plugins/installed_plugins.json').read_text()),
            'input': {'cwd': str(runtime), 'agentDir': str(omp_runtime.agent_dir(home)), 'sessionDir': observability.reviewer_session_dir(home, run['review_run_id']), 'reviewRunId': run['review_run_id'], 'receipt': {**run, 'omp_version': capability['omp_version']}, 'reviewPrompt': prompt, 'modelSelector': 'openai/gpt-5.2-codex'},
        }), flush=True)
        reviewer_runner.serve_broker(home, run['review_run_id'], sys.stdin, sys.stdout)
