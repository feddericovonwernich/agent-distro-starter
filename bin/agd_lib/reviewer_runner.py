"""Reviewer collection lifecycle and the trusted JSONL evidence broker."""
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import observability, reviewer, store


def assert_runtime_capability():
    """Discover the supported installed OMP modules without machine-specific paths."""
    binary = shutil.which('bun')
    if not binary:
        raise store.DependencyError('bun is unavailable')
    script = """const agent='@oh-my-pi/pi-coding-agent', aiName='@oh-my-pi/pi-ai', coreName='@oh-my-pi/pi-agent-core';
const [root,sdk,ai,core,telemetry,resolver]=await Promise.all([import(agent),import(agent+'/sdk'),import(aiName),import(coreName),import(agent+'/telemetry-export'),import(agent+'/config/model-resolver')]);
for(const [name,value] of Object.entries({Settings:sdk.Settings,ModelRegistry:sdk.ModelRegistry,AgentSession:sdk.AgentSession,SessionManager:sdk.SessionManager,Agent:core.Agent,AssistantMessageEventStream:ai.AssistantMessageEventStream,initTelemetryExport:telemetry.initTelemetryExport,resolveModelRoleValue:resolver.resolveModelRoleValue}))if(!value)throw new Error('unsupported OMP export: '+name);
if(typeof root.VERSION!=='string'&&typeof root.version!=='string')throw new Error('unsupported OMP version export');
console.log(JSON.stringify({ai_url:import.meta.resolve(aiName),core_url:import.meta.resolve(coreName),sdk_url:import.meta.resolve(agent+'/sdk'),telemetry_url:import.meta.resolve(agent+'/telemetry-export'),omp_version:root.VERSION||root.version}));"""
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME') if key in os.environ}
    try: result = subprocess.run([binary, '-e', script], capture_output=True, text=True, timeout=10, env=env)
    except subprocess.TimeoutExpired as error: raise store.DependencyError('installed OMP runtime capability timed out') from error
    if result.returncode:
        raise store.DependencyError(f'installed OMP runtime capability unavailable: {result.stderr.strip()}')
    try:
        capability = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise store.DependencyError('installed OMP runtime capability malformed') from error
    if not all(isinstance(capability.get(key), str) and capability[key] for key in ('ai_url', 'core_url', 'sdk_url', 'telemetry_url', 'omp_version')):
        raise store.DependencyError('installed OMP runtime capability malformed')
    return capability


def resolve_model(config, omp_binary):
    """Only an explicit configured safe ID is resolved in Python; null is host-owned."""
    config = reviewer.validate_config(config)
    model = config['review_model']
    if not model:
        raise store.Refusal('review model must be resolved by the native reviewer host')
    if not isinstance(omp_binary, str) or not omp_binary:
        raise store.Refusal('native OMP binary is unavailable')
    return model


def launch(home, intent, *, omp_binary='omp', observed_omp_version):
    """Reserve before collection; native host owns descriptor/default resolution."""
    with reviewer.run_lease(home):
        config = reviewer.read_config(home)
        instructions = Path(reviewer.INSTRUCTION_FILE).read_bytes()
        started = time.time()
        deadline_epoch_ms = int((started + config['limits']['max_runtime_seconds']) * 1000)
        run = reviewer.reserve_run(home, intent, config_snapshot=config, instruction_snapshot=instructions,
                                   started_at=started, deadline_epoch_ms=deadline_epoch_ms)
        packet = reviewer.select_packet(home, cutoff=started, config_snapshot=config,
                                        retrospective_review_run_ids=tuple(intent.get('retrospective_review_run_ids', ())))
        if not packet['excerpts']:
            return run
        model = resolve_model(config, omp_binary)
        return reviewer.begin_run(home, run, packet, model=model, observed_omp_version=observed_omp_version)


def _request_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise store.Refusal('invalid broker request ID') from error


def _broker_text(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _replay_or_reserve_input(home, review_run_id, request_id, op, args, value, limit, *, evidence_limit=None):
    digest = reviewer._digest({'op': op, 'args': args})
    with store.lock(store.home(home)):
        receipt = reviewer._receipt(home, review_run_id)
        replies = receipt.setdefault('broker_replies', {})
        prior = replies.get(request_id)
        if prior:
            if prior.get('digest') != digest: raise store.Refusal('conflicting broker request reuse')
            return prior['value']
        encoded = value if isinstance(value, str) else _broker_text(value)
        counters = receipt.setdefault('counters', {})
        used = counters.get('input_bytes', 0)
        if not isinstance(used, int) or used + len(encoded.encode()) > limit: raise store.Refusal('cumulative broker input limit exceeded')
        if evidence_limit is not None and counters.get('evidence_calls', 0) >= evidence_limit: raise store.Refusal('evidence-call limit exceeded')
        counters['input_bytes'] = used + len(encoded.encode())
        if evidence_limit is not None: counters['evidence_calls'] = counters.get('evidence_calls', 0) + 1
        replies[request_id] = {'digest': digest, 'value': value}
        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
    return value


def _durable_result(home, review_run_id):
    receipt = reviewer.recover_submissions(home, review_run_id)
    results = receipt.get('report_results', {})
    if not results: raise store.Refusal('report is unavailable')
    result = results[sorted(results)[-1]]
    if not isinstance(result, dict) or not isinstance(result.get('report_id'), str): raise store.Refusal('report result is malformed')
    return {'reportId': result['report_id']}


def serve_broker(home, review_run_id, stdin, stdout):
    """Serve only frozen packet evidence and receipt transitions over JSONL."""
    receipt = reviewer._receipt(home, review_run_id)
    if receipt.get('dispatch_state') not in ('begun', 'report-committed'):
        raise store.Refusal('reviewer receipt is not dispatchable')
    packet = reviewer.read_packet(home, receipt['packet_id'])
    config = json.loads((reviewer.paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json").read_text())
    limit = config['limits']['max_input_bytes']
    sources = {item['source_id'] for item in packet['sources']}
    excerpts = [item for item in packet['excerpts'] if item['source_id'] in sources]
    result = None
    for line in stdin:
        response = {'id': None, 'ok': False}
        try:
            request = json.loads(line); request_id = _request_id(request.get('id')); response['id'] = request_id
            op, args = request.get('op'), request.get('args')
            if not isinstance(op, str) or not isinstance(args, dict): raise store.Refusal('invalid broker request')
            if op == 'packet':
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text(packet), limit)
            elif op == 'reviewer_excerpt':
                source_id = args.get('source_id')
                if source_id not in sources: raise store.Refusal('source is outside packet')
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text([item for item in excerpts if item['source_id'] == source_id]), limit, evidence_limit=config['limits']['max_model_calls'])
            elif op == 'reviewer_search':
                query = args.get('query')
                if not isinstance(query, str) or not query or len(query.encode()) > 256: raise store.Refusal('invalid packet search')
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text([item for item in excerpts if query.casefold() in item['text'].casefold()]), limit, evidence_limit=config['limits']['max_model_calls'])
            elif op == 'reviewer_submit':
                report = args.get('report')
                result = reviewer.submit_report(home, review_run_id, request_id, report)
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text({'reportId': result['report_id']}), limit)
            elif op == 'reserve':
                if time.time() * 1000 >= receipt['deadline_epoch_ms']: raise store.Refusal('review deadline exceeded')
                with store.lock(store.home(home)):
                    current = reviewer._receipt(home, review_run_id); counters = current.setdefault('counters', {}); replies = current.setdefault('broker_replies', {})
                    digest = reviewer._digest({'op': op, 'args': args}); prior = replies.get(request_id)
                    if prior:
                        if prior.get('digest') != digest: raise store.Refusal('conflicting broker request reuse')
                        value = prior['value']
                    else:
                        if counters.get('model_calls', 0) >= config['limits']['max_model_calls']: raise store.Refusal('model-call limit exceeded')
                        counters['model_calls'] = counters.get('model_calls', 0) + 1; counters['provider_inflight'] = counters.get('provider_inflight', 0) + 1
                        value = {'model_calls': counters['model_calls']}; replies[request_id] = {'digest': digest, 'value': value}
                        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', current)
            elif op == 'settle':
                with store.lock(store.home(home)):
                    current = reviewer._receipt(home, review_run_id); counters = current.setdefault('counters', {}); replies = current.setdefault('broker_replies', {})
                    digest = reviewer._digest({'op': op, 'args': args}); prior = replies.get(request_id)
                    if prior:
                        if prior.get('digest') != digest: raise store.Refusal('conflicting broker request reuse')
                        value = prior['value']
                    else:
                        counters['provider_inflight'] = max(0, counters.get('provider_inflight', 0) - 1)
                        value = {'provider_inflight': counters['provider_inflight']}; replies[request_id] = {'digest': digest, 'value': value}
                        store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', current)
            elif op == 'session':
                native_session_id = args.get('id')
                value = observability.reviewer_binding(home, review_run_id, native_session_id)
            elif op == 'result':
                value = _durable_result(home, review_run_id)
            elif op == 'finish':
                value = reviewer.finalize_run(home, review_run_id, 'completed')
            else:
                raise store.Refusal('broker operation is denied')
            response.update(ok=True, value=value)
        except (json.JSONDecodeError, store.Refusal, ValueError, TypeError) as error:
            response['error'] = str(error)
        stdout.write(json.dumps(response, sort_keys=True) + '\n'); stdout.flush()
