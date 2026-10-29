import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from test_projects import command, run



PLUGIN_SPECS = {
    'ponytail@ponytail': ('ponytail', 'DietrichGebert/ponytail',
                          '@dietrichgebert/ponytail', '5.3.3'),
    'superpowers@superpowers-dev': ('superpowers-dev', 'obra/superpowers',
                                    'superpowers', '7.0.1'),
}

DEFAULT_PLUGIN_SKILLS = {
    'ponytail@ponytail': {
        name: {'SKILL.md': f'---\nname: {name}\ndescription: Fixture {name}\n---\n'}
        for name in (
            'ponytail', 'ponytail-audit', 'ponytail-debt', 'ponytail-gain',
            'ponytail-help', 'ponytail-review')
    },
    'superpowers@superpowers-dev': {
        name: {'SKILL.md': f'---\nname: {name}\ndescription: Fixture {name}\n---\n'}
        for name in (
            'systematic-debugging', 'test-driven-development',
            'verification-before-completion')
    },
}


def write_native_plugins(home, plugin_skills=None):
    """Write deterministic native OMP plugin state and payloads under home."""
    home = Path(home)
    plugin_skills = DEFAULT_PLUGIN_SKILLS if plugin_skills is None else plugin_skills
    registry = {'version': 2, 'plugins': {}}
    lock = {'plugins': {}, 'settings': {}}
    marketplaces = {'version': 1, 'marketplaces': []}
    config = home / 'data/omp/home/.omp'
    project_plugins = home / '.omp/plugins'
    project_plugins.mkdir(parents=True, exist_ok=True)
    for plugin_id, (market, publisher, package, version) in PLUGIN_SPECS.items():
        payload = config / 'plugins/cache/plugins' / f'{market}___fixture___{version}'
        if payload.exists():
            shutil.rmtree(payload)
        skill_root = payload / 'skills'
        skill_root.mkdir(parents=True)
        for skill_name, files in plugin_skills.get(plugin_id, {}).items():
            skill = skill_root / skill_name
            skill.mkdir()
            for relative, content in files.items():
                target = skill / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content if isinstance(content, bytes) else content.encode())
        (payload / 'package.json').write_text(json.dumps({
            'name': package, 'version': version, 'pi': {'skills': ['./skills']},
        }))
        registry['plugins'][plugin_id] = [{
            'scope': 'project', 'installPath': str(payload), 'version': version,
            'installedAt': '2026-01-01T00:00:00Z',
            'lastUpdated': '2026-01-01T00:00:00Z',
        }]
        lock['plugins'][package] = {
            'version': version, 'enabledFeatures': None, 'enabled': True,
        }
        catalog = config / 'plugins/cache/marketplaces' / market / 'marketplace.json'
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text('{}')
        marketplaces['marketplaces'].append({
            'name': market, 'sourceType': 'github', 'sourceUri': publisher,
            'catalogPath': str(catalog), 'addedAt': '2026-01-01T00:00:00Z',
            'updatedAt': '2026-01-01T00:00:00Z',
        })
    registry_path = project_plugins / 'installed_plugins.json'
    registry_path.write_text(json.dumps(registry))
    (project_plugins / 'omp-plugins.lock.json').write_text(json.dumps(lock))
    (config / 'plugins/project_registries.json').write_text(json.dumps({
        'version': 1, 'registries': [str(registry_path)],
    }))
    (config / 'marketplaces.json').write_text(json.dumps(marketplaces))
    return registry

@contextmanager
def lab():
    with tempfile.TemporaryDirectory(prefix='agd-lab-') as tmp:
        root = Path(tmp)
        seed, origin, home = root/'seed', root/'origin.git', root/'home'
        run(['git', 'init', '-b', 'main', seed])
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=seed)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=seed)
        (seed/'value.txt').write_text('base\n')
        run(['git', 'add', 'value.txt'], cwd=seed)
        run(['git', 'commit', '-m', 'initial'], cwd=seed)
        run(['git', 'clone', '--bare', seed, origin])
        command(home, 'init', '--backend', 'tmux')
        command(home, 'project', 'add', 'sample', '--source', str(origin))
        write_native_plugins(home)
        project = home/'projects'/'sample'
        run(['git', 'config', 'user.name', 'Distro Test'], cwd=project)
        run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=project)
        yield home, project
