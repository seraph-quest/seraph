"""Fixed Python/Node finalization inside the ORIGINAL supervised producer."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time


def finalize_original_outputs(payload, control):
    from config.settings import RepoSandboxSettings
    from src.execution.repo_sandbox import LocalRepoRepairExecutor, RepoSandboxJob, _patch_paths_from_diff
    from src.execution.repo_original_producer import canonical, digest, FINALIZATION_RESERVE_SECONDS
    physical_deadline = payload["deadline_at"] - FINALIZATION_RESERVE_SECONDS
    if time.monotonic() >= physical_deadline:
        raise ValueError("original_producer_cleanup_deadline")
    from src.execution.repo_node import NodeRepoRepairExecutor, PROFILE, MAX_DEP_FILE_BYTES, MAX_DEP_FILES, read_regular
    from src.execution.repo_sandbox import SnapshotEntry, _digest_entries
    durable = payload["original_producer"]
    stage = Path(payload["stage"])
    config = RepoSandboxSettings.model_validate(durable["config"])
    node = payload["profile"] == PROFILE
    executor = (NodeRepoRepairExecutor if node else LocalRepoRepairExecutor)(config, workspace_dir=stage.parents[3])
    identity = stage.stat(follow_symlinks=False)
    if [identity.st_dev, identity.st_ino] != durable["stage_identity"]:
        raise ValueError("original_producer_stage_changed")
    raw = json.loads(executor._read_private_output(stage / "out", "supervisor-result.json"))
    proof = raw.get("process_cleanup", {})
    if (raw.get("job_id") != payload["job_id"] or raw.get("token") != payload["token"]
            or raw.get("iteration_binding") != payload["iteration_binding"]
            or raw.get("supervisor_pid") != os.getpid()
            or proof.get("cleanup_proven") is not True or proof.get("oracle") != "linux_subreaper_waitpid_echild"):
        raise ValueError("original_producer_quiescence_unproven")
    names = ("diff.patch", "pytest.stdout", "pytest.stderr", "build.stdout", "build.stderr") if node else (
        "diff.patch", "pytest.stdout", "pytest.stderr")
    outputs = {}
    for name in names:
        outputs[name] = executor._read_private_output(stage / "out", name)
    # Node's original approved input uses its descriptor-bound source reader;
    # it is not an exported 0600 Python worker output.
    patch = (read_regular(stage, "patch.diff", executor.limits.max_patch_bytes) if node else
        executor._read_private_output(stage / "input", "patch.diff"))
    if digest(patch) != durable["patch_sha256"]:
        raise ValueError("original_producer_approved_patch_changed")
    fields = dict(durable["job"])
    fields["allowed_paths"] = tuple(fields["allowed_paths"])
    fields["test_args"] = tuple(fields["test_args"])
    job = RepoSandboxJob(**fields, patch_bytes=patch)
    commands = raw.get("commands")
    if (not isinstance(commands, list) or any(entry.get("stdout_eof") is not True
            or entry.get("stderr_eof") is not True or entry.get("waited") is not True
            or entry.get("cleanup", {}).get("cleanup_proven") is not True for entry in commands)):
        raise ValueError("original_producer_command_closure_unproven")
    interrupted = control.interrupted or raw.get("status") == "cancelled"
    eligible = not interrupted and bool(commands) and not any(
        entry.get("timed_out") or entry.get("cancelled") or entry.get("leftover_descendant")
        or entry.get("stdout_truncated") for entry in commands)
    if node:
        if raw.get("diff_sha256") != digest(outputs["diff.patch"]):
            raise ValueError("original_producer_diff_changed")
        tested = raw.get("tested_file_hash_metadata")
        if eligible:
            if (not isinstance(tested, list) or not tested or len(tested) > executor.limits.max_files + MAX_DEP_FILES
                    or any(not isinstance(entry, dict) or set(entry) != {"path", "size_bytes", "sha256"}
                        or not isinstance(entry["path"], str) or entry["path"].startswith("/")
                        or ".." in entry["path"].split("/") or type(entry["size_bytes"]) is not int
                        or not 0 <= entry["size_bytes"] <= (MAX_DEP_FILE_BYTES if entry["path"].startswith("node_modules/") else executor.limits.max_file_bytes)
                        or not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64 for entry in tested)
                    or len({entry["path"] for entry in tested}) != len(tested)
                    or _digest_entries(SnapshotEntry(entry["path"], entry["size_bytes"], entry["sha256"])
                        for entry in tested) != raw.get("after_digest")):
                raise ValueError("original_producer_tested_tree_changed")
        for entry in commands:
            stream = "pytest" if entry.get("script") == "test" else "build"
            if digest(outputs[stream + ".stdout"]) != entry.get("stdout_sha256") or digest(outputs[stream + ".stderr"]) != entry.get("stderr_sha256"):
                raise ValueError("original_producer_command_output_changed")
        # A profile-defined failing build can omit its dependent test; an EOF
        # prefix cannot. The actual requested-check list determines eligibility.
        expected = payload["plan"]["commands"]
        if eligible and (len(commands) > len(expected) or any(
                entry.get("argv") != expected[index]["argv"] or entry.get("script") != expected[index]["script"]
                for index, entry in enumerate(commands))):
            raise ValueError("original_producer_check_plan_changed")
        if eligible and len(commands) != len(expected) and commands[-1].get("exit_code") == 0:
            eligible = False
        worker_failed = raw.get("status") != "succeeded"
        if eligible and (any(type(entry.get("exit_code")) is not int for entry in commands)
                or (worker_failed and (raw.get("reason") != "node_command_failed_or_cancelled_or_timeout_or_descendant"
                    or not any(entry["exit_code"] != 0 for entry in commands)))
                or (not worker_failed and (raw.get("reason") is not None
                    or any(entry["exit_code"] != 0 for entry in commands)))):
            eligible = False
        manifest = {**raw, "execution_plan": payload["plan"], "execution_identity": payload["runtime"]}
    else:
        worker_outputs = {**outputs, **{name: executor._read_private_output(stage / "out", name)
            for name in ("manifest.json", "readback.json")}}
        if json.loads(worker_outputs["manifest.json"]).get("status") == "blocked":
            eligible = False
            manifest = {**raw}
            worker_failed = True
        else:
            manifest, readback, worker_failed = executor._validate_worker_output(outputs=worker_outputs,
                job=job, image="", patch_paths=_patch_paths_from_diff(patch, job.allowed_paths))
            runtime = payload["runtime"]
            expected_identity = {"schema": "seraph.repo_repair_execution_identity.v1", "backend_kind": "local",
                "profile": str(config.profile), "job_id": job.job_id, "authority_digest": job.authority_digest,
                **{key: runtime[key] for key in ("worker_source_sha256", "interpreter_entry_path", "interpreter_path",
                    "interpreter_sha256", "pytest_executable_path", "pytest_executable_sha256",
                    "pytest_package_path", "pytest_package_sha256")}}
            if manifest.get("backend_kind") != "local" or manifest.get("execution_identity") != expected_identity:
                raise ValueError("original_producer_worker_identity_changed")
            if "publication_runtime" in runtime:
                from src.execution.repo_publication_runtime import capture, verify
                captured = capture(deadline_at=physical_deadline)
                if captured["proof"] != runtime["publication_runtime"]:
                    raise ValueError("original_producer_publication_runtime_changed")
                verify(stage / "python-runtime", captured, deadline_at=physical_deadline)
                attestation = manifest.get("publication_test_input")
                if (attestation != readback.get("publication_test_input") or not isinstance(attestation, dict)
                        or attestation.get("environment", {}).get("runtime_proof") != runtime["publication_runtime"]
                        or attestation.get("environment_unchanged") is not True):
                    raise ValueError("original_producer_publication_attestation_changed")
            manifest = {**manifest, **raw}
    original = executor.snapshot_repository(job.repository_root, stage / "original-after")
    if original.digest != job.base_digest:
        raise ValueError("original_producer_original_source_changed")
    if time.monotonic() >= physical_deadline:
        raise ValueError("original_producer_cleanup_deadline")
    executor._assert_stage_identity(stage, {"device": identity.st_dev, "inode": identity.st_ino})
    shutil.rmtree(stage)
    if stage.exists():
        raise ValueError("original_producer_stage_cleanup_unproven")
    parent_descriptor = os.open(stage.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    if time.monotonic() >= physical_deadline:
        raise ValueError("original_producer_cleanup_deadline")
    status = ("failed" if worker_failed else "succeeded") if eligible else "unknown_external_effect"
    outcome = ("completed_requested_check_failure" if worker_failed else "completed_requested_checks") if eligible else (
        "zero_command_prefix" if control.authorized_commands == 0 else "interrupted_prefix")
    manifest = {**manifest, "schema": "seraph.repo_repair_execution.v1", "profile": str(config.profile), "executor_kind": "local", "status": status,
        "authority_digest": job.authority_digest, "posture_digest": job.expected_posture_digest,
        "iteration_binding": payload["iteration_binding"], "source_original_unchanged": True,
        "cleanup_proven": True, "stage_removed": True, "isolation_claim": "none", "network_isolation": "not_verified",
        "resource_enforcement": "admission_and_wall_timeout_only", "learning": "no_learning",
        "supervisor_transport": {"transport_kind": "original_producer_durable_v1", "command_output_drained": True,
            "command_descriptors_closed": True, "original_children_waited": True, "no_spawn": control.no_spawn},
        "supervisor_identity": {"pid": os.getpid(), "start_identity": raw["supervisor_start"],
            "source_sha256": durable["posture"]["supervisor_source_sha256"], "token": payload["token"]}}
    outputs["manifest.json"] = canonical(manifest)
    outputs["readback.json"] = outputs["manifest.json"]
    if sum(len(raw) for raw in outputs.values()) > durable["max_output_bytes"]:
        raise ValueError("original_producer_total_output_bound")
    return manifest, outputs, outcome
