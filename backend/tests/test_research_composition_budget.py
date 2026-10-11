"""Original physical staging mechanics; resource capacity confers no Source."""
import copy
import os

import pytest

from config.settings import settings
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, MAX_BYTES
from src.work_board import input_artifacts as inputs
from src.work_board import research_artifacts as artifacts
from src.work_board.repository import BoardError


@pytest.fixture
def staging(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    return tmp_path


def request():
    return dict(programme_id="1" * 32, job_id="goal-discovery:" + "2" * 32,
        kind="public_brief", slot=0, content=b"original bounded public brief")


def reference(values):
    digest = artifacts.sha(values["content"])
    key = artifacts.sha(artifacts.json_bytes([values["job_id"], values["kind"], values["slot"]]))
    return f"goal-programmes/{values['programme_id']}/{key}-{digest}.json"


def files(root):
    return {str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def descriptors():
    live = set()
    for value in os.listdir("/proc/self/fd"):
        try:
            os.fstat(int(value))
        except OSError:
            continue
        live.add(int(value))
    return live


def reserve_cost(values):
    size = len(values["content"])
    return 3 * size + 2


def test_new_staging_reserves_before_real_write_and_replay_pays_each_read(staging, monkeypatch):
    values, budget = request(), HeaderReadBudget()
    initial_refs = set(budget.physical_references)
    actual_write = inputs._write_payload
    writes = []

    def observed_write(path, payload, **kwargs):
        assert budget.remaining == MAX_BYTES - len(payload) - 1 - reserve_cost(values)
        assert files(staging) == {}
        writes.append(path)
        return actual_write(path, payload, **kwargs)

    monkeypatch.setattr(inputs, "_write_payload", observed_write)
    before_fds = descriptors()
    result = artifacts.stage_discovery_artifact(**values, header_budget=budget)
    assert result.content == values["content"] and result.file_path == reference(values)
    assert len(writes) == 1
    assert files(staging) == {result.file_path: values["content"]}
    expected = MAX_BYTES - len(values["content"]) - 1 - reserve_cost(values)
    assert budget.remaining == expected
    for _ in range(2):
        replay = artifacts.stage_discovery_artifact(**values, header_budget=budget)
        expected -= len(values["content"]) + 1
        assert replay == result and budget.remaining == expected
    assert len(writes) == 1 and descriptors() == before_fds
    assert budget.physical_references == initial_refs  # SQL identities only.


@pytest.mark.parametrize("phase", ["probe", "reservation"])
def test_capacity_failure_precedes_body_or_file_effect(staging, monkeypatch, phase):
    values, budget = request(), HeaderReadBudget()
    size = len(values["content"])
    allowance = size if phase == "probe" else size + 1 + reserve_cost(values) - 1
    budget.debit(MAX_BYTES - allowance)
    reads, writes = [], []
    actual_read = os.read
    actual_write = inputs._write_payload

    def observed_read(*args):
        reads.append(args[1])
        return actual_read(*args)

    def observed_write(*args, **kwargs):
        writes.append(True)
        return actual_write(*args, **kwargs)

    monkeypatch.setattr(os, "read", observed_read)
    monkeypatch.setattr(inputs, "_write_payload", observed_write)
    before_fds = descriptors()
    with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
        artifacts.stage_discovery_artifact(**values, header_budget=budget)
    assert reads == writes == [] and list(staging.iterdir()) == []
    assert budget.remaining == allowance - (size + 1 if phase == "reservation" else 0)
    assert descriptors() == before_fds


@pytest.mark.parametrize("collision", ["equal", "different", "appended", "truncated"])
def test_original_no_clobber_race_is_reserved_and_keeps_exact_bytes(staging, monkeypatch, collision):
    values, budget = request(), HeaderReadBudget()
    actual_write = inputs._write_payload
    actual_read = os.read
    read_sizes = []

    def race(path, payload, **kwargs):
        assert budget.remaining == MAX_BYTES - len(payload) - 1 - reserve_cost(values)
        raced = {"equal": payload, "different": b"different original racer",
            "appended": payload + b"x", "truncated": payload[:-1]}[collision]
        actual_write(path, raced)
        return actual_write(path, payload, **kwargs)

    def observed_read(fd, size):
        read_sizes.append(size)
        return actual_read(fd, size)

    monkeypatch.setattr(inputs, "_write_payload", race)
    monkeypatch.setattr(os, "read", observed_read)
    before_fds = descriptors()
    if collision == "equal":
        assert artifacts.stage_discovery_artifact(**values, header_budget=budget).content == values["content"]
    else:
        with pytest.raises(OSError, match="input artifact collision"):
            artifacts.stage_discovery_artifact(**values, header_budget=budget)
    assert len(values["content"]) + 1 in read_sizes
    assert inputs.INPUT_ARTIFACT_MAX_BYTES + 1 not in read_sizes
    expected = {"equal": values["content"], "different": b"different original racer",
        "appended": values["content"] + b"x", "truncated": values["content"][:-1]}[collision]
    assert files(staging) == {reference(values): expected}
    assert descriptors() == before_fds
    assert budget.remaining == MAX_BYTES - len(values["content"]) - 1 - reserve_cost(values)


@pytest.mark.parametrize("failure", ["write", "fsync", "readback"])
def test_staging_error_does_not_refund_or_return_success_and_closes_fds(staging, monkeypatch, failure):
    values, budget = request(), HeaderReadBudget()
    actual_write = inputs._write_payload
    actual_read = artifacts.read_discovery
    attempts = []
    if failure == "write":
        def failed_write(path, payload, **kwargs):
            attempts.append(True)
            raise OSError("original write failed")
        monkeypatch.setattr(inputs, "_write_payload", failed_write)
    elif failure == "fsync":
        def failed_fsync(fd):
            attempts.append(True)
            raise OSError("original fsync failed")
        monkeypatch.setattr(os, "fsync", failed_fsync)
    else:
        def failed_readback(*args, **kwargs):
            if kwargs.get("header_budget") is None:
                attempts.append(True)
                raise OSError("original readback failed")
            return actual_read(*args, **kwargs)
        monkeypatch.setattr(artifacts, "read_discovery", failed_readback)
    before_fds = descriptors()
    with pytest.raises(OSError, match="original .* failed"):
        artifacts.stage_discovery_artifact(**values, header_budget=budget)
    assert attempts and descriptors() == before_fds
    assert budget.remaining == MAX_BYTES - len(values["content"]) - 1 - reserve_cost(values)
    assert files(staging) == ({reference(values): values["content"]} if failure == "readback" else {})


@pytest.mark.parametrize("invalid", ["symlink", "hardlink", "mode", "digest", "size"])
def test_existing_invalid_file_never_becomes_missing_output(staging, monkeypatch, invalid):
    values, budget = request(), HeaderReadBudget()
    path = staging / reference(values)
    path.parent.mkdir(parents=True, mode=0o700)
    if invalid == "symlink":
        target = staging / "private-target"
        target.write_bytes(values["content"])
        target.chmod(0o600)
        path.symlink_to(target)
    else:
        path.write_bytes(b"x" * len(values["content"]) if invalid == "digest" else
            values["content"] + b"x" if invalid == "size" else values["content"])
        path.chmod(0o644 if invalid == "mode" else 0o600)
        if invalid == "hardlink":
            os.link(path, staging / "second-link")
    before = files(staging)
    writes = []
    actual_write = inputs._write_payload
    def observed_write(*args, **kwargs):
        writes.append(True)
        return actual_write(*args, **kwargs)
    monkeypatch.setattr(inputs, "_write_payload", observed_write)
    before_fds = descriptors()
    with pytest.raises(BoardError):
        artifacts.stage_discovery_artifact(**values, header_budget=budget)
    assert writes == [] and files(staging) == before and descriptors() == before_fds
    assert budget.remaining == MAX_BYTES - len(values["content"]) - 1


@pytest.mark.parametrize("changed", ["copy", "frame", "path", "payload", "reuse"])
def test_private_reservation_is_exact_one_use_resource_only(staging, changed):
    values, budget = request(), HeaderReadBudget()
    args = dict(budget=budget, path=staging / reference(values), reference=reference(values),
        programme_id=values["programme_id"], kind=values["kind"], content=values["content"])
    reservation = artifacts._DiscoveryStagingReservation(**args)
    if changed == "copy":
        reservation = copy.copy(reservation)
    elif changed == "frame":
        args["budget"] = HeaderReadBudget()
    elif changed == "path":
        args["path"] = staging / "foreign-file"
    elif changed == "payload":
        args["content"] += b"x"
    else:
        assert reservation.publish(**args) == values["content"]
    before = files(staging)
    with pytest.raises(ValueError, match="discovery staging reservation changed"):
        reservation.publish(**args)
    assert files(staging) == before
    assert budget.remaining == MAX_BYTES - reserve_cost(values)


def test_default_original_stage_keeps_existing_output_without_budget(staging):
    values = request()
    first = artifacts.stage_discovery_artifact(**values)
    assert artifacts.stage_discovery_artifact(**values) == first
    assert files(staging) == {first.file_path: values["content"]}


@pytest.mark.parametrize("case", ["equal", "different", "appended", "truncated",
    "exact-cap", "exact-cap-appended", "above-cap-replay"])
def test_shared_writer_collision_bounds_preserve_replay_and_cap(staging, monkeypatch, case):
    cap = inputs.INPUT_ARTIFACT_MAX_BYTES
    size = 98304 if case == "above-cap-replay" else cap if case.startswith("exact-cap") else 37
    payload = b"x" * size
    existing = (b"y" * size if case == "different" else payload + b"y"
        if case in {"appended", "exact-cap-appended"} else payload[:-1]
        if case == "truncated" else payload)
    target = staging / "collision.json"
    target.write_bytes(existing)
    target.chmod(0o600)
    before_fds = descriptors()
    read_sizes = []
    real_read = os.read
    def observed_read(fd, count):
        read_sizes.append(count)
        return real_read(fd, count)
    monkeypatch.setattr(os, "read", observed_read)
    owner = inputs._PayloadClosureOwner(target, payload)
    accepted = case in {"equal", "exact-cap"}
    if accepted:
        inputs._write_payload(target, payload, _closure_owner=owner)
        closure = inputs._verified_payload_closure(owner)
        assert closure.replayed and closure.size_bytes == len(payload)
    else:
        with pytest.raises(OSError, match="^input artifact collision$"):
            inputs._write_payload(target, payload, _closure_owner=owner)
        with pytest.raises(OSError, match="^original_private_payload_closure_required$"):
            inputs._verified_payload_closure(owner)
    expected_read = 38 if size == 37 else 65537
    assert read_sizes == [expected_read]
    assert target.read_bytes() == existing
    assert list(staging.iterdir()) == [target]
    assert descriptors() == before_fds


@pytest.mark.parametrize("invalid", ["symlink", "mode", "hardlink"])
def test_shared_writer_collision_metadata_denies_before_read_and_closes(staging, monkeypatch, invalid):
    target = staging / "collision.json"
    payload = b"original private collision"
    other = staging / "other.json"
    if invalid == "symlink":
        other.write_bytes(payload)
        other.chmod(0o600)
        target.symlink_to(other.name)
    else:
        target.write_bytes(payload)
        target.chmod(0o644 if invalid == "mode" else 0o600)
        if invalid == "hardlink":
            os.link(target, other)
    initial_names = set(staging.iterdir())
    before_bytes = files(staging)
    before_fds = descriptors()
    reads = []
    real_read = os.read
    def observed_read(fd, count):
        reads.append(count)
        return real_read(fd, count)
    monkeypatch.setattr(os, "read", observed_read)
    with pytest.raises(OSError):
        inputs._write_payload(target, payload)
    assert reads == [] and set(staging.iterdir()) == initial_names
    assert files(staging) == before_bytes and descriptors() == before_fds
    if invalid == "symlink":
        assert target.is_symlink() and os.readlink(target) == other.name
