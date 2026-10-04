"""Actual local bytes/lifecycle transitions; no native-execution claim here."""
import json
from pathlib import Path

import pytest
import yaml

from src.extensions.authored_scaffold import scaffold_adapter
from src.extensions.capability_pack import CapabilityPackLifecycle,CapabilityPackLifecycleError,capability_pack_digest,parse_capability_pack_manifest
from src.work_board.authored_packages import AuthoredIntegrityFailure,load_registration

OWNER={"owner_principal_id":"operator-one","session_id":"root-one"}
PACK="local.lifecycle-ledger"
CAP="pack.local.lifecycle-ledger.summarize.v1"


def candidate(tmp_path,version):
    root=tmp_path/("version-"+version)
    scaffold_adapter(root,package_id=PACK,display_name="Lifecycle test ledger")
    manifest=yaml.safe_load((root/"manifest.yaml").read_bytes());manifest["version"]=version
    (root/"manifest.yaml").write_text(yaml.safe_dump(manifest,sort_keys=False))
    return root,parse_capability_pack_manifest((root/"manifest.yaml").read_text())


def approval(store,manifest,digest,action):
    prepared=store.prepare_operator_approval(PACK,action=action,goal_id="goal-one",digest=digest,version=manifest.version,
        content_digest=digest,authority_digest=manifest.authority_digest,**OWNER)
    identity=prepared["approval"]["approval_id"]
    store.resolve_operator_approval(PACK,identity,decision="approved",**OWNER)
    return identity


def install(store,root,manifest,action="activate"):
    review=store.review(manifest,root_path=root,goal_id="goal-one",goal_revision=1,reviewed_by="operator-one",authority_expansion_approved=True)
    digest=capability_pack_digest(root)
    identifier=approval(store,manifest,digest,action)
    result=getattr(store,action)(manifest,root_path=root,goal_id="goal-one",review_id=review["review"]["review_id"],approval_id=identifier,
        content_digest=digest,authority_digest=manifest.authority_digest,**OWNER)
    assert result["pointer"]["digest"]==digest
    return digest


@pytest.mark.parametrize("field,value",[
    ("reviewed_by","another-reviewer"),("authority_expansion_approved",False),
    ("publisher_trust",{}),("dependencies",[]),("authored_contract",{}),
    ("authority_digest","0"*64),("dependencies_digest","0"*64),
])
def test_historical_review_binding_conflict_never_overwrites(tmp_path,field,value):
    store=CapabilityPackLifecycle(tmp_path/"lifecycle.json")
    root,manifest=candidate(tmp_path,"1.0.0")
    request=dict(root_path=root,goal_id="goal-one",goal_revision=1,reviewed_by="operator-one",authority_expansion_approved=True)
    first=store.review(manifest,**request)["review"]
    assert store.review(manifest,**request)["review"]==first
    with store._state_lock():
        state=store._load()
        # Empty dependencies are the genuine original value; inject an
        # unsupported extra dependency rather than mirroring the implementation.
        state["reviews"][first["review_id"]][field]=["unreviewed"] if field=="dependencies" else value
        store._commit(state)
    before=store.state_path.read_bytes()
    with pytest.raises(CapabilityPackLifecycleError,match="historical review binding conflicts"):
        store.review(manifest,**request)
    assert store.state_path.read_bytes()==before


@pytest.mark.parametrize("publish_race",["restored","new-active"])
def test_observed_integrity_survives_restoration_and_new_pointer(tmp_path,publish_race):
    store=CapabilityPackLifecycle(tmp_path/"lifecycle.json")
    r1,m1=candidate(tmp_path,"1.0.0");d1=install(store,r1,m1)
    r2,m2=candidate(tmp_path,"2.0.0");d2=install(store,r2,m2,"update")
    original=(r2/"adapter.py").read_bytes();(r2/"adapter.py").write_bytes(original+b"# changed after review\n")
    # Capture a genuine observation under the original shared stage lock;
    # publication deliberately waits until the bytes or active pointer change.
    with store._state_lock(shared=True):
        with pytest.raises(AuthoredIntegrityFailure) as observed:
            load_registration(CAP,lifecycle=store,state=store._load())
    (r2/"adapter.py").write_bytes(original)
    if publish_race=="new-active":
        r3,m3=candidate(tmp_path,"3.0.0");d3=install(store,r3,m3,"update")
    result=store.quarantine_authored_observation(**observed.value.binding)
    state=store._load();assert d2 in state["revoked"][PACK] and state["versions"][PACK][d2]["revoked"]
    assert result["expected_digest"]==d2 and result["observed_digest"]!=d2
    if publish_race=="new-active":
        assert state["active"][PACK]["digest"]==d3 and state["active"][PACK]["status"]=="active"
        assert load_registration(CAP,lifecycle=store).pin["digest"]==d3
        return
    assert state["active"][PACK]["status"]=="quarantined"
    rollback=approval(store,m1,d1,"rollback")
    safe=store.rollback(PACK,goal_id="goal-one",approval_id=rollback,content_digest=d1,authority_digest=m1.authority_digest,**OWNER)
    assert safe["pointer"]["digest"]==d1 and store._load()["revoked"][PACK]==[d2]
    unsafe=approval(store,m2,d2,"rollback")
    with pytest.raises(CapabilityPackLifecycleError,match="revoked"):
        store.rollback(PACK,goal_id="goal-one",approval_id=unsafe,content_digest=d2,authority_digest=m2.authority_digest,**OWNER)


def test_operator_revoke_and_uninstall_cannot_use_quarantine_rollback(tmp_path):
    store=CapabilityPackLifecycle(tmp_path/"lifecycle.json")
    r1,m1=candidate(tmp_path,"1.0.0");d1=install(store,r1,m1)
    r2,m2=candidate(tmp_path,"2.0.0");d2=install(store,r2,m2,"update")
    revoke=approval(store,m2,d2,"revoke")
    store.revoke(PACK,approval_id=revoke,digest=d2,content_digest=d2,authority_digest=m2.authority_digest,**OWNER)
    with pytest.raises(CapabilityPackLifecycleError,match="active or paused"):
        store.rollback(PACK,goal_id="goal-one",**OWNER)
    uninstall=approval(store,m2,d2,"uninstall")
    store.uninstall(PACK,approval_id=uninstall,content_digest=d2,authority_digest=m2.authority_digest,**OWNER)
    assert set(store._load()["revoked"][PACK])=={d1,d2}
    with pytest.raises(CapabilityPackLifecycleError,match="active or paused"):
        store.rollback(PACK,goal_id="goal-one",**OWNER)


def test_native_reference_reconciliation_preserves_only_exact_metadata(tmp_path):
    # This is lifecycle metadata, not a native execution/output receipt. Actual
    # native success and its mirror are checked in authenticated native tests.
    store=CapabilityPackLifecycle(tmp_path/"lifecycle.json")
    r1,m1=candidate(tmp_path,"1.0.0");install(store,r1,m1)
    pin=load_registration(CAP,lifecycle=store).pin
    identity="authored-json:"+"a"*40
    from datetime import datetime,timezone,timedelta
    contract={"native_job_id":identity,"native_kind":"local_authored_json","input_digest":"b"*64,
        "authority_digest":"c"*64,"deadline_at":(datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat(),"pack_pin":pin}
    store.register_job(PACK,goal_id="goal-one",job_id=identity,request_contract=contract,
        required_tools=["isolated_json_adapter"],required_filesystem=["workspace_read","workspace_write"],**OWNER)
    store._set_local_job_status(identity,status="running")
    before=store._load()["jobs"][identity]
    assert store._native_job_reference_valid(store._load(),identity,before)
    r2,m2=candidate(tmp_path,"2.0.0");install(store,r2,m2,"update")
    assert store.reconcile(PACK,**OWNER)["changes"]==[]
    assert store._load()["jobs"][identity]==before
    assert store.status(PACK,**OWNER)["jobs"][0]["control_authority"]=="canonical_native_job"
    # An altered kind/fingerprint cannot suppress ordinary reconciliation.
    with store._state_lock():
        state=store._load();state["jobs"][identity]["request_contract"]["native_kind"]="invented-native"
        store._commit(state)
    result=store.reconcile(PACK,**OWNER)
    assert result["changes"][0]["status"]=="cancelled"
