"""Reviewer collection lifecycle and the trusted JSONL evidence broker."""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from urllib.parse import unquote, urlparse

from pathlib import Path

from . import observability, omp_runtime, reviewer, store


def assert_runtime_capability(deadline_epoch_ms, *, home=None, omp_binary='omp'):
    """Discover public installed OMP modules from the selected OMP executable."""
    remaining = reviewer.deadline_remaining_seconds(deadline_epoch_ms)
    if remaining <= 0: raise store.DependencyError('reviewer deadline exceeded')
    omp = shutil.which(omp_binary)
    if not omp: raise store.DependencyError('omp is unavailable')
    try:
        entry = Path(omp).resolve(strict=True)
        if not entry.is_file(): raise ValueError
        root = next((parent for parent in (entry.parent, *entry.parents)
                     if (parent / 'package.json').is_file() and not (parent / 'package.json').is_symlink()
                     and json.loads((parent / 'package.json').read_text()).get('name') == '@oh-my-pi/pi-coding-agent'), None)
        if root is None: raise ValueError
        manifest = json.loads((root / 'package.json').read_text())
        declared = (root / manifest['bin']['omp']).resolve(strict=True)
        if declared != entry or root not in entry.parents or not manifest.get('version'): raise ValueError
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        raise store.DependencyError('installed OMP package root is unavailable')
    binary = shutil.which('bun')
    if not binary: raise store.DependencyError('bun is unavailable')
    script = """const agent='@oh-my-pi/pi-coding-agent', modules={ai:'@oh-my-pi/pi-ai',core:'@oh-my-pi/pi-agent-core',sdk:agent+'/sdk',registry:agent+'/config/model-registry',manager:agent+'/session/session-manager',session:agent+'/session/agent-session',telemetry:agent+'/telemetry-export',resolver:agent+'/config/model-resolver'};
const [ai,core,sdk,registry,manager,session,telemetry,resolver]=await Promise.all(Object.values(modules).map(name=>import(name)));
for(const [name,value] of Object.entries({Agent:core.Agent,streamSimple:ai.streamSimple,AssistantMessageEventStream:ai.AssistantMessageEventStream,Settings:sdk.Settings,discoverAuthStorage:sdk.discoverAuthStorage,ModelRegistry:registry.ModelRegistry,SessionManager:manager.SessionManager,AgentSession:session.AgentSession,initTelemetryExport:telemetry.initTelemetryExport,isTelemetryExportEnabled:telemetry.isTelemetryExportEnabled,createTelemetryExportConfig:telemetry.createTelemetryExportConfig,flushTelemetryExport:telemetry.flushTelemetryExport,getModelMatchPreferences:resolver.getModelMatchPreferences,resolveAllowedModels:resolver.resolveAllowedModels,resolveModelRoleValue:resolver.resolveModelRoleValue,resolveCliModel:resolver.resolveCliModel,formatModelString:resolver.formatModelString}))if(typeof value!=='function')throw new Error('unsupported OMP export: '+name);
console.log(JSON.stringify({ai_url:import.meta.resolve(modules.ai),core_url:import.meta.resolve(modules.core),sdk_url:import.meta.resolve(modules.sdk),registry_url:import.meta.resolve(modules.registry),manager_url:import.meta.resolve(modules.manager),session_url:import.meta.resolve(modules.session),telemetry_url:import.meta.resolve(modules.telemetry),resolver_url:import.meta.resolve(modules.resolver),omp_version:process.argv[1]}));"""
    base_env = {key: os.environ[key] for key in ('PATH', 'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME') if key in os.environ}
    env = omp_runtime.environment(home, base_env) if home is not None else base_env
    try: result = subprocess.run([binary, '--no-install', '--no-env-file', '-e', script, manifest['version']], capture_output=True, text=True, cwd=root, timeout=min(10, remaining), env=env)
    except subprocess.TimeoutExpired as error: raise store.DependencyError('installed OMP runtime capability timed out') from error
    if result.returncode: raise store.DependencyError(f'installed OMP runtime capability unavailable: {result.stderr.strip()}')
    try: capability = json.loads(result.stdout)
    except json.JSONDecodeError as error: raise store.DependencyError('installed OMP runtime capability malformed') from error
    expected = {'ai_url', 'core_url', 'sdk_url', 'registry_url', 'manager_url', 'session_url', 'telemetry_url', 'resolver_url', 'omp_version'}
    try:
        if set(capability) != expected or not isinstance(capability['omp_version'], str) or not capability['omp_version']: raise ValueError
        for key in expected - {'omp_version'}:
            parsed = urlparse(capability[key])
            location = Path(unquote(parsed.path)).resolve(strict=True)
            if parsed.scheme != 'file' or root.parents[1] not in location.parents: raise ValueError
    except (OSError, TypeError, ValueError):
        raise store.DependencyError('installed OMP runtime capability malformed')
    return capability



def _runtime_boundary(home, review_run_id):
    runtime = reviewer.paths(home)['runtime'] / review_run_id
    plugins = runtime / '.omp' / 'plugins'
    reviewer._private(plugins)
    reviewer.publish_immutable(
        plugins / 'installed_plugins.json', {'version': 2, 'plugins': {}})
    reviewer.publish_immutable(
        plugins / 'omp-plugins.lock.json', {'plugins': {}, 'settings': {}})
    return runtime


def run_native_reviewer(home, intent, *, omp_binary='omp'):
    """Run one selector-only native child while retaining the private lease."""
    with reviewer.run_lease(home):
        config, started = reviewer.read_config(home), time.time()
        run = reviewer.reserve_run(home, intent, config_snapshot=config, instruction_snapshot=Path(reviewer.INSTRUCTION_FILE).read_bytes(),
                                   started_at=started, deadline_epoch_ms=int((started + config['limits']['max_runtime_seconds']) * 1000))
        try:
            packet = reviewer.select_packet(home, cutoff=started, config_snapshot=config,
                                            retrospective_review_run_ids=tuple(intent.get('retrospective_review_run_ids', ())),
                                            deadline_epoch_ms=run['deadline_epoch_ms'])
        except store.Refusal:
            if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0:
                return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='timeout')
            raise
        if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0:
            return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='timeout', packet=packet)
        if not packet['excerpts']:
            return reviewer.close_run(home, run['review_run_id'], 'no-work', packet=packet)
        directory = omp_runtime.agent_dir(home)
        if directory.is_symlink() or not directory.is_dir(): return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='agent-dir', packet=packet)
        try: capability = assert_runtime_capability(run['deadline_epoch_ms'], home=home, omp_binary=omp_binary)
        except Exception:
            return reviewer.close_run(home, run['review_run_id'], 'failed',
                                      error_code='timeout' if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0 else 'capability',
                                      packet=packet)
        if reviewer.deadline_remaining_seconds(run['deadline_epoch_ms']) <= 0:
            return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='timeout', packet=packet)
        try:
            with store.lock(store.home(home)):
                receipt = reviewer._receipt(home, run['review_run_id'])
                receipt.update(packet_id=packet['packet_id'], omp_version=capability['omp_version'])
                store.atomic_json(reviewer.paths(home)['receipts'] / f"{run['review_run_id']}.json", receipt)
            env, _, _ = observability.reviewer_environment(home, run['review_run_id'])
            runtime = _runtime_boundary(home, run['review_run_id'])
            payload = {'urls': {key: capability[f'{key}_url'] for key in ('ai', 'core', 'sdk', 'registry', 'manager', 'session', 'telemetry', 'resolver')},
                       'input': {'cwd': str(runtime), 'agentDir': str(directory.resolve()), 'sessionDir': observability.reviewer_session_dir(home, run['review_run_id']),
                                 'reviewRunId': run['review_run_id'], 'receipt': {'deadline_epoch_ms': run['deadline_epoch_ms'], 'omp_version': capability['omp_version']},
                                 'modelSelector': config['review_model'], 'reviewPrompt': reviewer._read_json(reviewer.paths(home)['manifests'] / f"{run['instruction_snapshot_ref']}.json")['text']}}
        except Exception:
            return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='preparation', packet=packet)
        bun = shutil.which('bun')
        if not bun: return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='bun', packet=packet)
        stderr = bytearray()
        try:
            process = subprocess.Popen([bun, '--no-install', '--no-env-file', str(Path(__file__).resolve().parents[2] / 'lib' / 'agd-reviewer-host.ts')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=runtime, start_new_session=True)
        except OSError: return reviewer.close_run(home, run['review_run_id'], 'failed', error_code='spawn', packet=packet)
        def drain():
            for chunk in iter(lambda: process.stderr.read(256), ''):
                if len(stderr) < 4096: stderr.extend(chunk.encode(errors='replace')[:4096-len(stderr)])
        thread = threading.Thread(target=drain, daemon=True); thread.start()
        timed_out, completion, code = threading.Event(), None, None
        def expire():
            timed_out.set()
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        timer = threading.Timer(max(.001, run['deadline_epoch_ms']/1000-time.time()), expire); timer.start()
        try:
            process.stdin.write(json.dumps(payload) + '\n'); process.stdin.flush()
            completion = serve_broker(home, run['review_run_id'], process.stdout, process.stdin)
            code = process.wait()
        except Exception as error:
            failure = error
        else:
            failure = None
        finally:
            timer.cancel()
            if process.poll() is None: os.killpg(process.pid, signal.SIGKILL); process.wait()
            process.stdout.close(); process.stdin.close(); thread.join(timeout=1); process.stderr.close()
        receipt = reviewer._receipt(home, run['review_run_id'])
        outcome = 'unknown' if receipt['counters'].get('provider_inflight') else 'failed'
        if timed_out.is_set() or failure or code or not isinstance(completion, dict):
            return reviewer.close_run(home, run['review_run_id'], outcome, error_code='timeout' if timed_out.is_set() else ('nonzero' if code else 'ipc'), diagnostic=bytes(stderr), packet=packet)
        try: result = _durable_result(home, run['review_run_id'])
        except Exception: return reviewer.close_run(home, run['review_run_id'], outcome, error_code='completion', diagnostic=bytes(stderr), packet=packet)
        if completion.get('reportId') != result['reportId']:
            return reviewer.close_run(home, run['review_run_id'], outcome, error_code='completion', diagnostic=bytes(stderr), packet=packet)
        return reviewer.finalize_run(home, run['review_run_id'], 'exited')

def launch_timer(home, *, now=None):
    intent = reviewer.timer_intent(home, now=now)
    return None if intent is None else run_native_reviewer(home, intent)


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
    reviewer.recover_submissions(home, review_run_id)
    with store.lock(store.home(home)):
        receipt = reviewer._receipt(home, review_run_id)
        chosen = receipt.get('broker_result')
        if chosen is None:
            results = receipt.get('report_results', {})
            if len(results) != 1: raise store.Refusal('report result is ambiguous')
            request_id, result = next(iter(results.items()))
            if not isinstance(result, dict) or not isinstance(result.get('report_id'), str): raise store.Refusal('report result is malformed')
            chosen = {'request_id': request_id, 'report_id': result['report_id']}
            receipt['broker_result'] = chosen
            store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
        if not isinstance(chosen, dict) or not isinstance(chosen.get('request_id'), str) or not isinstance(chosen.get('report_id'), str): raise store.Refusal('report result is malformed')
        return {'reportId': chosen['report_id']}


def serve_broker(home, review_run_id, stdin, stdout):
    """Serve only frozen packet evidence and receipt transitions over JSONL."""
    receipt = reviewer._receipt(home, review_run_id)
    try: config = reviewer.validate_config(reviewer._read_json(reviewer.paths(home)['manifests'] / f"{receipt['config_snapshot_ref']}.json"))
    except (OSError, ValueError, json.JSONDecodeError, store.Refusal) as error: raise store.Refusal('invalid frozen config manifest') from error
    if reviewer._digest(config) != receipt['config_snapshot_ref']: raise store.Refusal('frozen config manifest digest mismatch')
    limit, packet, sources, excerpts = config['limits']['max_input_bytes'], None, set(), []
    if receipt.get('dispatch_state') in ('begun', 'report-committed'):
        packet = reviewer.read_packet(home, receipt['packet_id']); sources = {item['source_id'] for item in packet['sources']}; excerpts = [item for item in packet['excerpts'] if item['source_id'] in sources]
    completion = None
    for line in stdin:
        if not line.strip(): continue
        if completion is not None: raise store.Refusal('reviewer protocol after completion')
        response = {'id': None, 'ok': False}
        try:
            request = json.loads(line)
            if set(request) == {'complete', 'result'}:
                result = request['result']; receipt = reviewer._receipt(home, review_run_id)
                if receipt.get('dispatch_state') != 'report-committed' or request['complete'] is not True or not isinstance(result, dict) or set(result) != {'reportId'} or not isinstance(result['reportId'], str) or result != _durable_result(home, review_run_id):
                    raise store.Refusal('invalid reviewer completion')
                completion = result
                continue
            request_id = _request_id(request.get('id')); response['id'] = request_id
            op, args = request.get('op'), request.get('args')
            receipt = reviewer._receipt(home, review_run_id)
            if not isinstance(op, str) or not isinstance(args, dict): raise store.Refusal('invalid broker request')
            if receipt.get('dispatch_state') == 'reserved' and op != 'model': raise store.Refusal('reviewer model is not persisted')
            if receipt.get('dispatch_state') not in ('reserved', 'begun', 'report-committed'): raise store.Refusal('reviewer receipt is not dispatchable')
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
                with store.lock(store.home(home)):
                    receipt = reviewer._receipt(home, review_run_id)
                    receipt['broker_result'] = {'request_id': request_id, 'report_id': result['report_id']}
                    store.atomic_json(reviewer.paths(home)['receipts'] / f'{review_run_id}.json', receipt)
                value = _replay_or_reserve_input(home, review_run_id, request_id, op, args, _broker_text({'reportId': result['report_id']}), limit)
            elif op == 'reserve':
                if receipt.get('dispatch_state') not in ('begun', 'report-committed') or not receipt.get('model'): raise store.Refusal('reviewer model is not persisted')
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
            elif op == 'model':
                model = args.get('id')
                if not isinstance(model, str) or not __import__('re').fullmatch(r'[A-Za-z0-9._-]+/[A-Za-z0-9._-]+', model):
                    raise store.Refusal('invalid resolved reviewer model')
                receipt = reviewer._receipt(home, review_run_id)
                if receipt.get('dispatch_state') == 'reserved':
                    packet = reviewer.read_packet(home, receipt['packet_id']); sources = {item['source_id'] for item in packet['sources']}; excerpts = [item for item in packet['excerpts'] if item['source_id'] in sources]
                    value = reviewer.begin_run(home, receipt, packet, model=model, observed_omp_version=receipt.get('omp_version'))
                elif receipt.get('dispatch_state') in ('begun', 'report-committed') and receipt.get('model') == model:
                    value = receipt
                else:
                    raise store.Refusal('reviewer model transition is unavailable')
            elif op == 'session':
                native_session_id = args.get('id')
                value = observability.reviewer_binding(home, review_run_id, native_session_id)
            elif op == 'result':
                value = _durable_result(home, review_run_id)
            else:
                raise store.Refusal('broker operation is denied')
            response.update(ok=True, value=value)
        except (json.JSONDecodeError, store.Refusal, ValueError, TypeError) as error:
            response['error'] = str(error)
        stdout.write(json.dumps(response, sort_keys=True) + '\n'); stdout.flush()
    return completion
