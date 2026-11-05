"""Static pre-cutover schema-1 records for history and prepared recovery tests."""
import uuid

from agd_lib import reviewer, store


def historical_prepared(home):
    run_id, request, proposal_id = (str(uuid.uuid4()) for _ in range(3))
    config = reviewer.read_config(home)
    config_ref = reviewer._digest(config)
    source = '1' * 64
    followup = '2' * 64
    packet = {'schema': 1, 'sources': [{'source_id': source}],
              'excerpts': [{'source_id': source}], 'catalog': [],
              'coverage': {'followup_id': followup}}
    packet_id = reviewer._digest(packet)
    finding = {'kind': 'new-skill', 'target': 'historical-skill', 'finding': 'Historical finding.',
               'applicability': 'Historical workflow.', 'citations': [source], 'counterexamples': [],
               'uncertainty': 'Historical evidence only.', 'resolution': 'Historical draft.',
               'validation': 'Human review.', 'validation_limits': [],
               'new_skill': {'id': 'historical-skill',
                             'draft': '---\nname: historical-skill\ndescription: Historical fixture\n---\n',
                             'triggers': ['history'], 'scope': 'history', 'prerequisites': []}}
    report = {'schema': 1, 'summary': 'Historical fixture.', 'findings': [finding]}
    report_id = reviewer._digest({'review_run_id': run_id, 'request_id': request, 'report': report})
    revision_id = '4' * 64
    revision = {'schema': 1, 'proposal_id': proposal_id, 'revision_hash': revision_id,
                'report_id': report_id, 'packet_id': packet_id, 'basis_source_ids': [source],
                'proposed_change': finding, 'observer': {'model': 'historical/model',
                'omp_version': 'historical', 'instruction_snapshot_ref': '3' * 64}, 'resurfaced': False}
    proposal = {'schema': 1, 'proposal_id': proposal_id, 'fingerprint': '5' * 64,
                'head_revision': revision_id, 'revisions': [revision], 'uncertain_distinct': False}
    record = {'schema': 1, 'review_run_id': run_id, 'request_id': request, 'report': report,
              'packet_id': packet_id, 'proposal_ids': [proposal_id], 'revision_ids': [revision_id],
              'observer': {'model': 'historical/model', 'omp_version': 'historical',
                           'instruction_snapshot_ref': '3' * 64, 'catalog_versions': []}}
    result = {'report_id': report_id, 'proposal_ids': [proposal_id]}
    prepared = {'schema': 1, 'run_id': run_id, 'request_id': request, 'input_digest': reviewer._digest(report),
                'result': result, 'record': record, 'output_bytes': len(reviewer._json_bytes(record)),
                'revisions': [revision], 'proposals': [proposal]}
    receipt = {'schema': 1, 'review_run_id': run_id, 'packet_id': packet_id,
               'config_snapshot_ref': config_ref, 'instruction_snapshot_ref': '3' * 64,
               'dispatch_state': 'begun', 'model': 'historical/model', 'omp_version': 'historical'}
    reviewer.publish_immutable(reviewer.paths(home)['manifests'] / f'{config_ref}.json', config)
    reviewer.publish_immutable(reviewer.paths(home)['packets'] / f'{packet_id}.json', packet)
    reviewer.publish_immutable(reviewer.paths(home)['receipts'] / f'{run_id}.json', receipt)
    reviewer.publish_immutable(reviewer._prepared_path(home, run_id, request), prepared)
    store.atomic_json(reviewer.paths(home)['followups'] / f'{followup}.json',
                      {'schema': 1, 'material_basis': followup, 'state': 'pending', 'candidates': []})
    return receipt, request, report, result


def historical_final(home):
    receipt, request, report, result = historical_prepared(home)
    reviewer.recover_submissions(home, receipt['review_run_id'])
    reviewer.finalize_run(home, receipt['review_run_id'], 'exited')
    return receipt, result
