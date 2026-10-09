"""Focused SQL projection bounds; no positive Source authority is fabricated."""
from tests.test_home_attention import _mechanics
from tests.test_home_continuation import accounting_db
from tests.test_general_task_planner import forbid_external_inference
from src.db.models import Goal
from src.operator.home_projection import home_projection

async def test_oversized_goal_title_is_null_before_scalar_transfer(accounting_db,monkeypatch,forbid_external_inference):
    client,operator,goal=await _mechanics(accounting_db,monkeypatch,count=0)
    async with accounting_db[2].accounting_sessions() as db:
        row=await db.get(Goal,goal.id)
        row.title='PRIVATE_OVERSIZED_TITLE_SENTINEL'*50000
        db.add(row)
    try:
        async with client:
            response=await client.get('/api/operator/continuation')
            assert response.status_code==200,response.text
            rows=response.json()['active_goals']['items']
            assert len(rows)==1 and rows[0]['goal_id']==goal.id and rows[0]['title'] is None
            assert 'PRIVATE_OVERSIZED_TITLE_SENTINEL' not in response.text
    finally:home_projection.stop()
