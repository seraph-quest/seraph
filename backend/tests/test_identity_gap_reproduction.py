import pytest
from sqlalchemy import select
from config.settings import settings
from src.db.models import Goal
from src.api.auth import _reset_login_throttle_for_tests

@pytest.mark.asyncio
async def test_fresh_login_persists_but_hides_previous_goal(client, async_db, monkeypatch):
    monkeypatch.setattr(settings, 'operator_auth_secret', 'identity-gap-test')
    monkeypatch.setattr(settings, 'operator_auth_secret_hash', '')
    monkeypatch.setattr(settings, 'operator_auth_allowed_hosts', 'test')
    monkeypatch.setattr(settings, 'operator_auth_allowed_origins', 'http://localhost:3001')
    monkeypatch.setattr(settings, 'operator_auth_cookie_secure', False)
    _reset_login_throttle_for_tests()
    headers = {'origin': 'http://localhost:3001'}
    first = await client.post('/api/auth/login', json={'password': 'identity-gap-test'}, headers=headers)
    assert first.status_code == 200
    created = await client.post('/api/goals', json={'title': 'Persistent historical goal', 'level': 'daily', 'domain': 'productivity'}, headers=headers)
    assert created.status_code == 200, created.text
    identifier = created.json()['id']
    assert (await client.post('/api/auth/logout', headers=headers)).status_code == 204
    second = await client.post('/api/auth/login', json={'password': 'identity-gap-test'}, headers=headers)
    assert second.status_code == 200
    assert first.json()['principal_id'] != second.json()['principal_id']
    assert first.json()['session_id'] != second.json()['session_id']
    assert identifier not in {row['id'] for row in (await client.get('/api/goals')).json()}
    async with async_db() as db:
        goal = (await db.execute(select(Goal).where(Goal.id == identifier))).scalar_one()
        assert goal.owner_session_id == first.json()['session_id']
