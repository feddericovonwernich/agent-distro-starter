"""Test-only module discovery for native worker/episode protocol suites."""
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse


def capability(home):
    from agd_lib import omp_runtime, store

    entry = Path(omp_runtime.executable(home)).resolve(strict=True)
    root = next((parent for parent in (entry.parent, *entry.parents)
                 if (parent / 'package.json').is_file()
                 and not (parent / 'package.json').is_symlink()
                 and json.loads((parent / 'package.json').read_text()).get('name')
                 == '@oh-my-pi/pi-coding-agent'), None)
    if root is None:
        raise store.DependencyError('installed OMP package root is unavailable')
    manifest = json.loads((root / 'package.json').read_text())
    declared = (root / manifest['bin']['omp']).resolve(strict=True)
    if declared != entry or not manifest.get('version'):
        raise store.DependencyError('installed OMP package root is unavailable')
    binary = shutil.which('bun')
    if not binary:
        raise store.DependencyError('bun is unavailable')
    script = """const agent='@oh-my-pi/pi-coding-agent', modules={ai:'@oh-my-pi/pi-ai',core:'@oh-my-pi/pi-agent-core',sdk:agent+'/sdk',registry:agent+'/config/model-registry',manager:agent+'/session/session-manager',session:agent+'/session/agent-session',telemetry:agent+'/telemetry-export',resolver:agent+'/config/model-resolver'};
const values=await Promise.all(Object.values(modules).map(name=>import(name)));
for(const [name,value] of Object.entries({ai:values[0],core:values[1],sdk:values[2],registry:values[3],manager:values[4],session:values[5],telemetry:values[6],resolver:values[7]})){if(!value)throw new Error(name)}
console.log(JSON.stringify({ai_url:import.meta.resolve(modules.ai),core_url:import.meta.resolve(modules.core),sdk_url:import.meta.resolve(modules.sdk),registry_url:import.meta.resolve(modules.registry),manager_url:import.meta.resolve(modules.manager),session_url:import.meta.resolve(modules.session),telemetry_url:import.meta.resolve(modules.telemetry),resolver_url:import.meta.resolve(modules.resolver),omp_version:process.argv[1]}));"""
    result = subprocess.run([binary, '--no-install', '--no-env-file', '-e', script, manifest['version']],
                            cwd=root, capture_output=True, text=True, timeout=10,
                            env=omp_runtime.environment(home, {'PATH': str(Path(binary).parent)}))
    if result.returncode:
        raise store.DependencyError('native OMP test modules unavailable')
    value = json.loads(result.stdout)
    expected = {'ai_url', 'core_url', 'sdk_url', 'registry_url', 'manager_url',
                'session_url', 'telemetry_url', 'resolver_url', 'omp_version'}
    if set(value) != expected or not all(isinstance(value[key], str) and value[key]
                                         for key in expected):
        raise store.DependencyError('native OMP test capability malformed')
    for key in expected - {'omp_version'}:
        parsed = urlparse(value[key])
        location = Path(unquote(parsed.path)).resolve(strict=True)
        if parsed.scheme != 'file' or root not in location.parents:
            raise store.DependencyError('native OMP test capability escaped package root')
    return value
