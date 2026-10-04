"""Representation negatives use actual signed fixture bootstrap, not effects."""
import json
from pathlib import Path
import html
import pytest
from src.browser.forgejo_bootstrap import validate_document, bootstrap_digest
from src.browser.forgejo_issue_title import ForgejoError


def source():
    actual = json.loads((Path(__file__).parent / 'fixtures/forgejo_signed_bootstrap_v15.json').read_text())
    return actual['scripts']


def document(scripts):
    return ('<!doctype html><html><head>' + ''.join(
        '<script' + ''.join(' '+name+'="'+html.escape(value,quote=True)+'"' for name,value in s['attributes'].items())
        + '>' + s['text'] + '</script>' for s in scripts) + '</head><body></body></html>').encode()


def test_actual_signed_provider_bootstrap_tokens_with_literal_account_data():
    items = source()
    result = validate_document(document(items), 'seraph_fixture925')
    assert result['script_count'] == 5 and result['literal_participants_only'] is True
    # Changing whitespace between existing tokens is equivalent. Joining
    # separate executable identifiers is not equivalent.
    original = items[0]['text']
    assert bootstrap_digest(original.replace('\t','  '), 'seraph_fixture925') == result['bootstrap_sha256']
    with pytest.raises(ForgejoError):
        validate_document(document([{**items[0], 'text': original.replace('new Set(', 'newSet(')}, *items[1:]]), 'seraph_fixture925')


@pytest.mark.parametrize('mode',['new_script','new_field','foreign_avatar','foreign_account',
                               'injected_participant','new_handler','changed_origin','changed_version',
                               'unapproved_locale','changed_asset','storage_initializer'])
def test_bootstrap_or_inline_layout_drift_rejects_before_browser_execution(mode):
    items = source()
    if mode == 'new_script': items.append({'attributes':{},'text':'fetch("https://evil.example/")'})
    if mode == 'new_field': items[0]['text']=items[0]['text'].replace('appUrl:', 'token: "hidden", appUrl:')
    if mode == 'foreign_avatar': items[0]['text']=items[0]['text'].replace('codeberg.org\\/avatars','evil.example\\/avatars')
    if mode == 'foreign_account': items[0]['text']=items[0]['text'].replace("name: 'seraph_fixture925'", "name: 'other'")
    if mode == 'injected_participant': items[0]['text']=items[0]['text'].replace("fullname: ''", "fullname: runCode()")
    if mode == 'new_handler': items[1]['attributes']['onload']='fetch("/mutate")'
    if mode == 'changed_origin': items[0]['text']=items[0]['text'].replace('codeberg.org','evil.example',1)
    if mode == 'changed_version': items[0]['text']=items[0]['text'].replace('15.0.9','16.0.0')
    if mode == 'unapproved_locale': items[0]['text']=items[0]['text'].replace('Copied!', 'Copie!')
    if mode == 'changed_asset': items[1]['attributes']['src']+='/unexpected'
    if mode == 'storage_initializer': items[2]['text'] += '; localStorage.setItem("secret", "x")'
    with pytest.raises(ForgejoError): validate_document(document(items),'seraph_fixture925')
