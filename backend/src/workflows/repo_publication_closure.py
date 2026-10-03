"""Server-owned immutable tested inputs for finite publication GET closure.

Capture the private producer once under its guard. Each adapter seal compares
the original intent to these bytes; no caller supplies expected payload proof.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from types import MappingProxyType

from config.settings import settings
from src.execution.repo_publication import (PublicationError, digest, equivalent,
    file_manifest, object_id, oid)
from src.execution.repo_publication_supervisor import terminal
from src.workspace import canonical_workspace_root

_INPUT_ORIGIN = object()
_BOUNDARY_ORIGIN = object()


@dataclass(frozen=True)
class _PrBoundary:
    input_identity: str
    actual_get_set_sha256: str
    _origin: object = field(repr=False)


@dataclass(frozen=True)
class _PublicationClosureInputs:
    job_id: str
    job_revision: int
    preview: dict
    produced: dict
    contents: dict[str, bytes]
    blob_ids: dict[str, str]
    blobs: dict[str, bytes]
    changed_paths: tuple[str, ...]
    remote_commit: str | None
    pr_number: int | None
    _origin: object = field(repr=False)
    _identity: str = field(repr=False)

    def identity(self):
        return digest({"job": self.job_id, "revision": self.job_revision,
            "preview": self.preview, "produced": self.produced,
            "files": dict(self.blob_ids),
            "changed": self.changed_paths, "remote_commit": self.remote_commit,
            "pr_number": self.pr_number})

    def valid(self, current):
        if self._origin is not _INPUT_ORIGIN or self.identity() != self._identity or current["job_id"] != self.job_id or current["revision"] != self.job_revision or current["declared_authority"]["preview_digest"] != digest(self.preview):
            raise PublicationError("publication_closure_inputs_changed")

    def expected(self, prior):
        prefix = f"/repos/{self.preview['repository']}"
        kind = prior["effect_type"]
        if kind.startswith("repo_publication_blob_"):
            path = next((path for path in self.changed_paths if path in self.contents and kind == "repo_publication_blob_" + hashlib.sha256(path.encode()).hexdigest()[:16]), None)
            if path is None:
                raise PublicationError("publication_closure_blob_intent_unproved")
            body = {"content": base64.b64encode(self.contents[path]).decode(), "encoding": "base64"}
            return prefix + "/git/blobs", body, prefix + "/git/blobs/" + self.blob_ids[path]
        if kind == "repo_publication_tree":
            modes = {item["path"]: item["mode"] for item in self.produced["files"]}
            body = {"base_tree": self.preview["base_tree"], "tree": [
                {"path": path, "mode": modes.get(path, "100644"), "type": "blob",
                 "sha": self.blob_ids[path] if path in self.contents else None}
                for path in self.changed_paths]}
            return prefix + "/git/trees", body, prefix + "/git/trees/" + self.produced["tree"] + "?recursive=1"
        if kind == "repo_publication_commit":
            person = {"name": "Seraph", "email": "seraph@localhost", "date": self.preview["commit_date"]}
            body = {"message": self.preview["commit_message"], "tree": self.produced["tree"],
                "parents": [self.preview["base_commit"]], "author": person, "committer": person}
            if self.remote_commit is None:
                raise PublicationError("remote_commit_id_required")
            return prefix + "/git/commits", body, prefix + "/git/commits/" + self.remote_commit
        if kind == "repo_publication_branch":
            if self.remote_commit is None:
                raise PublicationError("remote_commit_id_required")
            body = {"ref": "refs/heads/" + self.preview["branch_name"], "sha": self.remote_commit}
            return prefix + "/git/refs", body, prefix + "/git/ref/heads/" + self.preview["branch_name"]
        if kind == "repo_publication_pr":
            if self.pr_number is None:
                raise PublicationError("pr_number_required")
            if self.remote_commit is None:
                raise PublicationError("remote_commit_id_required")
            body = {"title": self.preview["title"], "body": self.preview["body"],
                "head": self.preview["branch_name"], "base": self.preview["base_branch"], "draft": False}
            return prefix + "/pulls", body, prefix + "/pulls/" + str(self.pr_number)
        raise PublicationError("publication_closure_intent_kind_invalid")

    def validate_blob(self, payload, identity, window):
        if not isinstance(payload, dict) or payload.get("encoding") != "base64" or payload.get("sha") != identity:
            raise PublicationError("remote_blob_mismatch")
        try:
            raw = base64.b64decode("".join(payload.get("content", "").split()), validate=True)
        except (ValueError, TypeError, AttributeError):
            raise PublicationError("remote_blob_mismatch") from None
        window.decoded(len(raw))
        expected = self.blobs.get(identity)
        if expected is None or raw != expected or payload.get("size") not in {None, len(raw)}:
            raise PublicationError("remote_blob_mismatch")

    def validate_tree(self, payload):
        if not isinstance(payload, dict) or payload.get("sha") != self.produced["tree"] or payload.get("truncated") is not False or not isinstance(payload.get("tree"), list):
            raise PublicationError("remote_tree_truncated")
        expected = {item["path"]: {"path": item["path"], "mode": item["mode"], "type": "blob", "sha": self.blob_ids[item["path"]]} for item in self.produced["files"]}
        directories = {str(parent) for path in expected for parent in Path(path).parents if str(parent) != "."}
        observed, observed_directories = {}, set()
        for item in payload["tree"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise PublicationError("remote_tree_mismatch")
            path = item["path"]
            if item.get("type") == "tree" and item.get("mode") == "040000":
                if path in observed_directories or path not in directories:
                    raise PublicationError("remote_tree_mismatch")
                oid(item.get("sha"))
                observed_directories.add(path)
            else:
                projection = {key: item.get(key) for key in ("path", "mode", "type", "sha")}
                if path in observed or projection != expected.get(path):
                    raise PublicationError("remote_tree_mismatch")
                observed[path] = projection
        if observed != expected or observed_directories != directories:
            raise PublicationError("remote_tree_mismatch")

    def validate_intent(self, current, prior, path, payload, service, window, boundary=None):
        self.valid(current)
        target, body, readpath = self.expected(prior)
        if prior.get("target_path") != target or prior.get("target_digest") != digest(body) or path != readpath:
            raise PublicationError("publication_closure_original_intent_changed")
        kind = prior["effect_type"]
        if kind.startswith("repo_publication_blob_"):
            self.validate_blob(payload, path.rsplit("/", 1)[1], window)
        elif kind == "repo_publication_tree":
            self.validate_tree(payload)
        elif kind == "repo_publication_commit":
            service.require(payload.get("sha") == self.remote_commit, "remote_commit_mismatch")
            service.verify_commit(payload, self.preview, self.produced["tree"])
        elif kind == "repo_publication_branch":
            service.require(payload.get("ref") == body["ref"] and payload.get("object", {}).get("type") == "commit" and payload.get("object", {}).get("sha") == self.remote_commit, "remote_branch_mismatch")
        elif kind == "repo_publication_pr":
            if type(boundary) is not _PrBoundary or boundary._origin is not _BOUNDARY_ORIGIN or boundary.input_identity != self._identity:
                raise PublicationError("publication_closure_complete_pr_boundary_required")
            service.require(payload.get("number") == self.pr_number, "remote_pr_mismatch")
            service.verify_pr(payload, self.preview, self.remote_commit)


def capture_inputs(service, current, preview, request):
    """Called only under the actual per-job guard after trusted terminal proof."""
    from src.workflows.repo_publication import read_file
    checkpoint = next((item.get("payload") for item in current.get("checkpoints", []) if item.get("checkpoint_id") == "publication_supervisor_admission"), None)
    if not isinstance(checkpoint, dict):
        raise PublicationError("publication_supervisor_admission_missing")
    actual = terminal(checkpoint)
    produced = actual.get("result")
    if not isinstance(produced, dict) or actual.get("status") != "complete":
        raise PublicationError("publication_complete_producer_required_for_remote_intents")
    stage = Path(canonical_workspace_root(settings.workspace_dir)) / f"artifacts/repo-publication/{current['job_id']}/producer"
    equivalent(file_manifest(stage), preview["tested_input"]["tested_files"])
    equivalent(produced["files"], preview["tested_input"]["tested_files"])
    if len(produced["files"]) > 2000 or sum(item["size"] for item in produced["files"]) > 64 * 1024 * 1024:
        raise PublicationError("publication_closure_file_bounds")
    contents = {item["path"]: read_file(str((stage / item["path"]).relative_to(Path(canonical_workspace_root(settings.workspace_dir)))), maximum=2 * 1024 * 1024) for item in produced["files"]}
    for item in produced["files"]:
        if len(contents[item["path"]]) != item["size"] or hashlib.sha256(contents[item["path"]]).hexdigest() != item["sha256"]:
            raise PublicationError("publication_closure_source_changed")
    before = {item["path"]: item for item in preview["tested_input"]["base_files"]}
    after = {item["path"]: item for item in produced["files"]}
    changed = tuple(sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path)))
    if list(changed) != produced["changed_paths"]:
        raise PublicationError("publication_closure_changed_paths_mismatch")
    known_commit = next((item.get("details", {}).get("remote_identity") for item in reversed(current["effects"]) if item.get("effect_type") == "repo_publication_commit" and item.get("receipt_kind") == "readback" and item.get("status") == "succeeded"), None)
    commit = request.remote_commit_id or known_commit
    if commit is not None:
        oid(commit)
    if known_commit is not None and commit != known_commit:
        raise PublicationError("remote_commit_id_binding_conflict")
    known_pr = next((item.get("details", {}).get("remote_identity") for item in reversed(current["effects"]) if item.get("effect_type") == "repo_publication_pr" and item.get("receipt_kind") == "readback" and item.get("status") == "succeeded"), None)
    number = request.pr_number or known_pr
    if number is not None:
        service.positive(number)
    if known_pr is not None and number != known_pr:
        raise PublicationError("pr_number_binding_conflict")
    blob_ids = {path: object_id("blob", raw) for path, raw in contents.items()}
    value = _PublicationClosureInputs(current["job_id"], current["revision"], preview, produced, MappingProxyType(contents), MappingProxyType(blob_ids), MappingProxyType({blob_ids[path]: raw for path, raw in contents.items()}), changed, commit, number, _INPUT_ORIGIN, "")
    from dataclasses import replace
    return replace(value, _identity=value.identity())


async def collect_pr_boundary(inputs, service, get, read_authority, window):
    """Positive exact full tree/blob/diff GETs, with protected raw capture."""
    from src.extensions.github_consent import digest as authority_digest
    records = []

    async def actual(path):
        payload = await get(path)
        captured = service.adapter._last_verified_get
        service.adapter._last_verified_get = None
        if captured is None or captured[0] != path or captured[2] != digest(payload) or captured[3] != authority_digest(read_authority.__dict__):
            raise PublicationError("publication_closure_actual_boundary_get_required")
        records.append({"path": path, "raw": captured[1], "semantic": captured[2]})
        return payload

    prefix = f"/repos/{inputs.preview['repository']}"
    commit = await actual(prefix + "/git/commits/" + inputs.remote_commit)
    service.require(commit.get("sha") == inputs.remote_commit, "remote_commit_mismatch")
    service.verify_commit(commit, inputs.preview, inputs.produced["tree"])
    tree = await actual(prefix + "/git/trees/" + inputs.produced["tree"] + "?recursive=1")
    inputs.validate_tree(tree)
    for blob in inputs.blobs:
        payload = await actual(prefix + "/git/blobs/" + blob)
        inputs.validate_blob(payload, blob, window)
    changed = set(inputs.changed_paths)
    actual_paths = set()
    for page in range(1, 22):
        values = await actual(prefix + f"/pulls/{inputs.pr_number}/files?per_page=100&page={page}")
        service.require(isinstance(values, list) and len(values) <= 100, "remote_pr_diff_invalid")
        for item in values:
            service.require(isinstance(item, dict), "remote_pr_diff_invalid")
            path, status = item.get("filename"), item.get("status")
            service.require(path in changed and path not in actual_paths and status in {"added", "removed", "modified", "renamed"}, "remote_pr_diff_mismatch")
            actual_paths.add(path)
            if status == "renamed":
                previous = item.get("previous_filename")
                service.require(previous in changed and previous not in actual_paths, "remote_pr_diff_mismatch")
                actual_paths.add(previous)
            if status != "removed":
                service.require(path in inputs.blob_ids and item.get("sha") == inputs.blob_ids[path], "remote_pr_diff_blob_mismatch")
            else:
                service.require(path not in inputs.blob_ids, "remote_pr_diff_mismatch")
        if len(values) < 100:
            service.require(actual_paths == changed, "remote_pr_diff_mismatch")
            return _PrBoundary(inputs._identity, digest(records), _BOUNDARY_ORIGIN)
    raise PublicationError("remote_pr_diff_truncated")
