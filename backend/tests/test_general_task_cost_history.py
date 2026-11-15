"""Retained genuine V1 financial receipts remain data under the current parser."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.work_board.general_task import digest
from src.workflows.general_task_accounting import entry_for
from src.workflows.inference_accounting import InferenceAccountingError


def original_receipts():
    return json.loads((Path(__file__).parent/'fixtures/historical_general_task_cost_v1.json').read_text())


def test_actual_original_specialist_stop_membership_is_unchanged():
    original=original_receipts()['specialist']
    membership=[]
    for literal in original['rows']:
        row=SimpleNamespace(**literal)
        entry=entry_for(row)
        assert 'preparation' not in entry
        # Existing C1 defaults already formed part of the protected stop seal.
        assert 'delegation_invocation_id' in entry and 'delegation_request_digest' in entry
        membership.append({'operation_id':row.operation_id,'state':row.state,'evidence_digest':digest(entry)})
    assert digest(sorted(membership,key=lambda item:item['operation_id']))==original['original_sealed_membership_digest']


def test_actual_original_c4_source_receipts_do_not_gain_delegation_fields():
    for original in original_receipts()['communication']:
        entry=entry_for(SimpleNamespace(**original['row']))
        assert entry['role']=='communication_preparation'
        assert 'delegation_invocation_id' not in entry and 'delegation_request_digest' not in entry
        assert digest(entry)==original['original_normalized_entry_digest']


@pytest.mark.parametrize('role',['specialist','communication_preparation'])
def test_explicit_original_null_fields_keep_their_presence(role):
    receipts=original_receipts()
    literal=(receipts['specialist']['rows'][1] if role=='specialist' else receipts['communication'][0]['row'])
    row=SimpleNamespace(**literal)
    evidence=json.loads(row.evidence_json)
    entry=next(item for item in evidence if item.get('kind')=='general_task_group_reservation.v1')
    fields=('preparation',) if role=='specialist' else ('delegation_invocation_id','delegation_request_digest')
    entry.update({field:None for field in fields})
    row.evidence_json=json.dumps(evidence)
    normalized=entry_for(row)
    assert all(field in normalized and normalized[field] is None for field in fields)


@pytest.mark.parametrize('change',['wrong_owner','wrong_job','group_digest','role_smuggle'])
def test_historical_binding_drift_is_rejected(change):
    row=SimpleNamespace(**original_receipts()['specialist']['rows'][1])
    if change=='wrong_owner':row.owner_id='operator:other'
    elif change=='wrong_job':row.job_id='inference:foreign'
    else:
        evidence=json.loads(row.evidence_json)
        entry=next(item for item in evidence if item.get('kind')=='general_task_group_reservation.v1')
        if change=='group_digest':entry['group_digest']='0'*64
        else:entry['role']='initial_proposal'
        row.evidence_json=json.dumps(evidence)
    with pytest.raises(InferenceAccountingError):entry_for(row)
