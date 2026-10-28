"""Reviewer collection lifecycle; native OMP dispatch is deliberately owned by the host bridge."""
import time
from pathlib import Path

from . import reviewer, store


def resolve_model(config, omp_binary):
    """Return the configured concrete model; native default resolution belongs to the host bridge."""
    config = reviewer.validate_config(config)
    model = config['review_model']
    if not model:
        raise store.Refusal('review model must be resolved by the native reviewer host')
    if not isinstance(omp_binary, str) or not omp_binary:
        raise store.Refusal('native OMP binary is unavailable')
    return model


def launch(home, intent, *, omp_binary='omp'):
    """Atomically reserve, collect, and mark one receipt ready for native dispatch."""
    with reviewer.run_lease(home):
        config = reviewer.read_config(home)
        instructions = Path(reviewer.INSTRUCTION_FILE).read_bytes()
        started = time.time()
        deadline_epoch_ms = int((started + config['limits']['max_runtime_seconds']) * 1000)
        run = reviewer.reserve_run(home, intent, config_snapshot=config, instruction_snapshot=instructions,
                                   started_at=started, deadline_epoch_ms=deadline_epoch_ms)
        model = resolve_model(config, omp_binary)
        packet = reviewer.select_packet(home, cutoff=started, config_snapshot=config,
                                        retrospective_review_run_ids=tuple(intent.get('retrospective_review_run_ids', ())))
        return reviewer.begin_run(home, run, packet, model=model)
