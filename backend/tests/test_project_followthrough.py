"""C4.3: issue → reviewed task → local tested patch → exact approved ready PR.

One offline local GitHub fixture serves the selected repository's issues and
its Git Data/PR API; the local repository is real Git. The planner and the
repository iteration are scripted at their own transports, and external
inference sockets are denied (``forbid_external_inference``). The lineage is
one navigable chain, and a changed source revision, repository, branch or
patch invalidates the exact publication approval before any write.
"""
from datetime import datetime, timezone
import json
import subprocess
import uuid

from cryptography.fernet import Fernet
import httpx
import pytest

from config.settings import settings
from src.approval.repository import approval_repository, approval_decision_digest
from src.extensions.github_consent import GitHubConsentRequest, PUBLICATION_ACTIONS
from src.extensions.github_followthrough import GitHubFollowthroughService
from src.execution.repo_publication import PublicationError
from src.integrations.project_source import ProjectSourceRef, ProjectSourceScope, ProjectSourceService
from src.vault.repository import vault_repository
from src.work_board.contracts import WorkBoardActionRequest, WorkBoardStatus
from src.work_board.general_task_planner import GeneralTaskPlanner
from src.workflows.repo_publication import PrepareRequest, RepoPublicationService
from tests.repo_publication_support import GitDataTransport
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_project_source import REPOSITORY, IssuesFixture, github_item
from tests.test_repo_work_task_publication import _actual_source_callback_journey
from tests.test_repository_work_natural_intent import _scripted_planner

BUG = "calculator.add(1, 2) returns -1 instead of 3; please fix calculator.py."


class ProjectGitHub:
    """Issues reads plus the Git Data/PR object store of the same repository."""

    def __init__(self, issues):
        self.issues, self.git, self.methods = issues, None, []

    def handler(self, request):
        self.methods.append((request.method, request.url.path))
        if request.url.path.startswith(f"/repos/{REPOSITORY}/issues"):
            return self.issues.handler(request)
        return self.git.handler(request)

    @property
    def posts(self):
        return [path for method, path in self.methods if method == "POST"]


async def _issue_task(factory, owner, service, literal, monkeypatch, github, state):
    """Operator flow: connect, sync, propose the finding, review and accept it."""
    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr("src.vault.crypto._fernet", None)
    await vault_repository.store("project-fixture", "fixture-token", owner_principal_id=owner.principal_id)
    async def resolver(_host, _port):
        return ["93.184.216.34"]
    adapter = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(github.handler))
    connection = await adapter.put_connection(owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, repository=REPOSITORY, vault_key="project-fixture", mode="active",
        expected_revision=0, consent=GitHubConsentRequest(acknowledged=True, duration_seconds=900,
            actions=sorted(PUBLICATION_ACTIONS | {"github_comment_write"})))
    source = ProjectSourceService(adapter=adapter, clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc))
    synced = await source.sync(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
        scope=ProjectSourceScope(connection_id=connection["id"], repository_id=REPOSITORY))
    ref = ProjectSourceRef.model_validate(synced["items"][0]["source_ref"])
    work = literal.plan.steps[0]
    with monkeypatch.context() as planning:
        _scripted_planner(planning, {"schema_version": 1, "revision": 1, "steps": [{"step_id": "repair",
            "tool_id": "repository_work", "input": work.input, "depends_on": [],
            "output_contract": work.output_contract}]}, [])
        service.planner = GeneralTaskPlanner()
        finding = await source.propose_task(owner=owner, general_task_service=service, source_ref=ref,
            goal_id="goal:fixture", goal_revision=1, kind="change", idempotency_key="issue-42",
            requested_output=literal.input.requested_output, limits=literal.input.limits,
            task_sessions=factory.accounting_sessions)
    task_id = finding.task_proposal["task_id"]
    async with factory.accounting_sessions() as db:
        task = await service.repository.get_task(db, owner, task_id)
        assert task.status == WorkBoardStatus.triage
        stage = await service.validate_acceptance(db, owner, task_id, task.task_revision)
        accepted = await service.repository.action_task(db, owner, task_id,
            WorkBoardActionRequest(action="promote", expected_revision=task.task_revision),
            accepted_method_stage=stage)
    state.update(adapter=adapter, source=source, connection=connection, ref=ref, task_id=task_id)
    return accepted


async def approved_publication(accounting_db, monkeypatch):
    state = {}
    github = ProjectGitHub(IssuesFixture([github_item(42, body=BUG, labels=("bug",))]))
    async def create(factory, owner, service, literal):
        return await _issue_task(factory, owner, service, literal, monkeypatch, github, state)
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch, False, "test_python",
        publication_profile=True, create=create)
    github.git = GitDataTransport(flow["repository"], base_branch="develop")
    owner, jobs = flow["owner"], flow["jobs"]
    monkeypatch.setattr("src.workflows.repo_publication.jobs", jobs)
    publisher = RepoPublicationService(adapter=state["adapter"], repository_source_service=flow["service"].repository_source_service)
    root = await jobs.get_job(flow["root_id"])
    connection = await state["adapter"].get_connection(owner.principal_id, owner.session_id)
    request = PrepareRequest(repair_job_id=root["job_id"], expected_repair_revision=root["revision"],
        proposal_id=flow["proposal"]["proposal_id"], expected_proposal_revision=flow["proposal"]["revision"],
        expected_connection_revision=connection["revision"], base_branch="develop",
        expected_base_commit=github.git.base, branch_name="feat/issue-42-fix",
        commit_message="Fix calculator addition (#42)", title="Fix calculator addition",
        body="Closes #42 with the locally tested patch.", idempotency_key=str(uuid.uuid4()))
    prepared = await publisher.prepare(request, owner.principal_id, owner.session_id)
    assert prepared["status"] == "awaiting_approval" and github.posts == []
    approval = await approval_repository.get(prepared["approval_id"])
    await approval_repository.resolve_exact(approval.id, "approved", expected_digest=approval_decision_digest(approval),
        owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
    return {**state, "flow": flow, "github": github, "publisher": publisher, "prepared": prepared, "request": request}


@pytest.mark.asyncio
async def test_issue_to_local_patch_to_exact_approved_pr_is_one_navigable_lineage(
        accounting_db, monkeypatch, repository_admission_signer):
    case = await approved_publication(accounting_db, monkeypatch)
    flow, owner, github = case["flow"], case["flow"]["owner"], case["github"]
    result = await case["publisher"].execute(case["prepared"]["job_id"], owner.principal_id, owner.session_id)
    assert result["status"] == "succeeded", result
    assert result["result"]["pr_number"] == 1 and github.posts.count(f"/repos/{REPOSITORY}/pulls") == 1
    # The publication re-read the exact issue before acting.
    assert ("GET", f"/repos/{REPOSITORY}/issues/42") in github.methods
    lineage = await case["source"].lineage(owner_principal_id=owner.principal_id, task_id=case["task_id"],
        owner_session_id=owner.session_id)
    ref = case["ref"]
    assert lineage["source"]["number"] == 42 and lineage["source"]["digest"] == ref.digest
    assert lineage["task_id"] == case["task_id"]
    (root,) = lineage["repository_roots"]
    assert root["job_id"] == flow["root_id"] and root["status"] == "succeeded"
    (iteration,) = root["iterations"]
    assert iteration["status"] == "succeeded" and len(iteration["patch_digest"]) == 64
    (publication,) = lineage["publications"]
    assert publication["job_id"] == case["prepared"]["job_id"] and publication["status"] == "succeeded"
    assert publication["repair_job_id"] == flow["root_id"]
    # The approved PR is exactly the reviewed iteration patch, as tested.
    assert publication["approved_input_patch_digest"] == iteration["patch_digest"]
    assert publication["project_source"]["accepted_digest"] == ref.digest
    assert publication["pull_request"] == {"number": 1, "browser_url": f"https://github.com/{REPOSITORY}/pull/1"}
    assert {"kind": "publication", "target_id": publication["job_id"]} in lineage["links"]
    assert {"kind": "task", "target_id": case["task_id"]} in lineage["links"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source_revision", "source_closed", "repository", "branch", "patch"])
async def test_changed_source_repository_branch_or_patch_invalidates_the_publication_approval(
        accounting_db, monkeypatch, repository_admission_signer, change):
    case = await approved_publication(accounting_db, monkeypatch)
    flow, owner, github = case["flow"], case["flow"]["owner"], case["github"]
    if change in {"source_revision", "source_closed"}:
        github.issues.items = [github_item(42, body=BUG + " Also handle floats." if change == "source_revision" else BUG,
            labels=("bug",), state="closed" if change == "source_closed" else "open",
            updated="2026-10-16T09:00:00+00:00")]
        code = "project_source_changed_decision_required"
    elif change == "repository":
        connection = await case["adapter"].get_connection(owner.principal_id, owner.session_id)
        await case["adapter"].put_connection(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            repository="acme/other", vault_key="project-fixture", mode="active",
            expected_revision=connection["revision"], consent=GitHubConsentRequest(acknowledged=True,
                duration_seconds=900, actions=sorted(PUBLICATION_ACTIONS)))
        code = None
    elif change == "branch":
        repository = flow["repository"]
        (repository / "README.md").write_text("Moved base\n")
        for args in (("add", "README.md"), ("-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "moved")):
            subprocess.run(["/usr/bin/git", *args], cwd=repository, check=True, capture_output=True)
        code = None  # The Source owner refuses first: "The selected Git base changed".
    else:
        patch = flow["workspace"] / case["prepared"]["preview"]["repair_binding"]["patch_artifact_id"]
        patch.write_bytes(patch.read_bytes() + b"\n")
        code = None
    from src.workflows.repo_repair import RepoRepairError
    with pytest.raises((PublicationError, RepoRepairError)) as invalid:
        await case["publisher"].execute(case["prepared"]["job_id"], owner.principal_id, owner.session_id)
    if code is not None:
        assert invalid.value.code == code
    assert github.posts == []  # Nothing was written to GitHub.
    current = await flow["jobs"].get_job(case["prepared"]["job_id"])
    assert current["status"] == "awaiting_approval"  # The unstarted action stops for a decision.
    root = await flow["jobs"].get_job(flow["root_id"])
    assert root["status"] == "succeeded"  # Completed local work is never cancelled.


@pytest.mark.asyncio
async def test_operator_can_accept_a_changed_source_revision_with_a_fresh_approval(
        accounting_db, monkeypatch, repository_admission_signer):
    case = await approved_publication(accounting_db, monkeypatch)
    flow, owner, github = case["flow"], case["flow"]["owner"], case["github"]
    github.issues.items = [github_item(42, body=BUG + " Also handle floats.", labels=("bug",),
        updated="2026-10-16T09:00:00+00:00")]
    from src.integrations.project_source import project_item
    changed = project_item(REPOSITORY, github.issues.items[0])["digest"]
    request = case["request"].model_copy(update={"idempotency_key": str(uuid.uuid4()),
        "acknowledged_project_source_digest": changed})
    prepared = await case["publisher"].prepare(request, owner.principal_id, owner.session_id)
    assert prepared["status"] == "awaiting_approval" and prepared["approval_id"] != case["prepared"]["approval_id"]
    binding = prepared["preview"]["repair_binding"]["project_source"]
    assert binding["accepted_digest"] == changed and binding["task_bound_digest"] == case["ref"].digest
    assert binding["operator_accepted_new_revision"] is True and github.posts == []


@pytest.mark.asyncio
async def test_comment_activity_on_the_issue_does_not_invalidate_the_publication_approval(
        accounting_db, monkeypatch, repository_admission_signer):
    """Seraph's own prepared response (or any comment) only moves ``updated_at``."""
    case = await approved_publication(accounting_db, monkeypatch)
    owner, github = case["flow"]["owner"], case["github"]
    (original,) = github.issues.items
    github.issues.items = [{**original, "updated_at": "2026-10-16T09:30:00+00:00", "comments": 1}]
    result = await case["publisher"].execute(case["prepared"]["job_id"], owner.principal_id, owner.session_id)
    assert result["status"] == "succeeded", result
    assert github.posts.count(f"/repos/{REPOSITORY}/pulls") == 1


@pytest.mark.asyncio
async def test_the_item_is_read_again_immediately_before_the_first_github_write(
        accounting_db, monkeypatch, repository_admission_signer):
    case = await approved_publication(accounting_db, monkeypatch)
    flow, owner, github = case["flow"], case["flow"]["owner"], case["github"]
    # Reads: 1 at prepare, 2 when the approval resumes, 3 just before the first POST.
    assert github.issues.item_reads == 1
    def edit_before_the_write(count):
        if count == 3:
            github.issues.items = [github_item(42, body=BUG + " Scope changed.", labels=("bug",),
                updated="2026-10-16T09:00:00+00:00")]
    github.issues.on_item_read = edit_before_the_write
    # The claimed attempt refuses before any write: blocked, not unknown.
    result = await case["publisher"].execute(case["prepared"]["job_id"], owner.principal_id, owner.session_id)
    assert result["status"] == "blocked", result
    assert github.issues.item_reads == 3 and github.posts == []
    assert [path for method, path in github.methods if method == "GET" and "/issues/" in path] == [
        f"/repos/{REPOSITORY}/issues/42"] * 3
    current = await flow["jobs"].get_job(case["prepared"]["job_id"])
    assert current["status"] == "blocked" and "project_source_changed_decision_required" in json.dumps(current)
    root = await flow["jobs"].get_job(flow["root_id"])
    assert root["status"] == "succeeded"  # Completed local work is kept.
