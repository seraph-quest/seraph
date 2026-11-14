from pathlib import Path

from scripts.backend_test_shards import (
    TestFile,
    assign_test_shards,
    discover_test_files,
    durations_from_shard_logs,
    hinted_test_weight,
    load_measured_durations,
    shard_for_index,
)


def test_assign_test_shards_is_total_and_disjoint():
    files = [
        TestFile(path="tests/test_a.py", weight=100),
        TestFile(path="tests/test_b.py", weight=90),
        TestFile(path="tests/test_c.py", weight=80),
        TestFile(path="tests/test_d.py", weight=70),
    ]

    shards = assign_test_shards(files, 3)
    flattened = [path for shard in shards for path in shard]

    assert sorted(flattened) == sorted(file.path for file in files)
    assert len(flattened) == len(set(flattened))


def test_discover_test_files_only_returns_pytest_files(tmp_path: Path):
    root = tmp_path
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_alpha.py").write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
    (tests_dir / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    discovered = discover_test_files(root)

    assert [item.path for item in discovered] == ["tests/test_alpha.py"]


def test_hinted_test_weight_can_raise_runtime_heavy_files_above_size_weight():
    assert hinted_test_weight("tests/test_eval_harness.py", 90_000) == 240_000
    assert hinted_test_weight("tests/test_approvals_api.py", 90_000) == 170_000
    assert hinted_test_weight("tests/test_context_window.py", 90_000) == 160_000
    assert hinted_test_weight("tests/test_small.py", 1_200) == 1_200


def test_assign_test_shards_separates_runtime_heavy_files_when_possible():
    files = [
        TestFile(path="tests/test_eval_harness.py", weight=hinted_test_weight("tests/test_eval_harness.py", 90_000)),
        TestFile(path="tests/test_llm_runtime.py", weight=hinted_test_weight("tests/test_llm_runtime.py", 96_000)),
        TestFile(path="tests/test_workflows.py", weight=hinted_test_weight("tests/test_workflows.py", 194_000)),
        TestFile(path="tests/test_alpha.py", weight=5_000),
        TestFile(path="tests/test_beta.py", weight=4_500),
        TestFile(path="tests/test_gamma.py", weight=4_000),
    ]

    shards = assign_test_shards(files, 3)
    shard_by_file = {
        path: index
        for index, shard in enumerate(shards)
        for path in shard
    }

    assert shard_by_file["tests/test_eval_harness.py"] != shard_by_file["tests/test_llm_runtime.py"]
    assert shard_by_file["tests/test_workflows.py"] != shard_by_file["tests/test_eval_harness.py"]


def test_shard_for_index_rejects_out_of_range(tmp_path: Path):
    root = tmp_path
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_alpha.py").write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    try:
        shard_for_index(root, shard_count=2, shard_index=3)
    except ValueError as exc:
        assert "shard_index" in str(exc)
    else:
        raise AssertionError("expected shard_for_index to reject an out-of-range index")


def test_measured_durations_override_size_weight_in_milliseconds():
    measured = {"tests/test_slow.py": 240.5}
    assert hinted_test_weight("tests/test_slow.py", 1_200, measured) == 240_500
    # Unmeasured files keep the size/hint estimate.
    assert hinted_test_weight("tests/test_eval_harness.py", 90_000, measured) == 240_000
    assert hinted_test_weight("tests/test_small.py", 1_200, measured) == 1_200


def test_durations_from_shard_logs_sums_split_invocations_and_drops_red_files():
    log = "\n".join([
        "2026-11-14T18:00:00Z [backend-shard] tests/test_alpha.py -> 0 (12.50s) timeout=900s",
        "2026-11-14T18:00:01Z [backend-shard] tests/test_split.py::part_one -> 0 (10.00s) timeout=900s",
        "2026-11-14T18:00:02Z [backend-shard] tests/test_split.py::part_two -> 0 (5.25s) timeout=900s",
        "2026-11-14T18:00:03Z [backend-shard] tests/test_red.py -> 1 (3.00s) timeout=900s",
        "2026-11-14T18:00:04Z [backend-shard] tests/test_hung.py -> 124 (900.00s) timed out after 900s",
        "2026-11-14T18:00:05Z [backend-shard] RETIRED/NOT RUN tests/test_eval_harness.py: cancelled module",
    ])
    assert durations_from_shard_logs([log]) == {"tests/test_alpha.py": 12.5, "tests/test_split.py": 15.25}


def test_measured_durations_balance_shards_that_size_alone_cannot(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    # Same size, very different runtime: size alone pairs the two slow files.
    for name in ("test_a", "test_b", "test_c", "test_d"):
        (tests_dir / f"{name}.py").write_text("x" * 1_000, encoding="utf-8")
    measured = {"tests/test_a.py": 600.0, "tests/test_b.py": 590.0, "tests/test_c.py": 10.0, "tests/test_d.py": 9.0}
    shards = assign_test_shards(discover_test_files(tmp_path, measured), 2)
    assert {frozenset(shard) for shard in shards} == {
        frozenset({"tests/test_a.py", "tests/test_d.py"}), frozenset({"tests/test_b.py", "tests/test_c.py"})}


def test_load_measured_durations_ignores_missing_or_malformed_files(tmp_path: Path):
    assert load_measured_durations(tmp_path / "absent.json") == {}
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert load_measured_durations(tmp_path / "bad.json") == {}
    (tmp_path / "ok.json").write_text('{"seconds": {"tests/test_a.py": 4.5, "tests/test_b.py": -1}}', encoding="utf-8")
    assert load_measured_durations(tmp_path / "ok.json") == {"tests/test_a.py": 4.5}
