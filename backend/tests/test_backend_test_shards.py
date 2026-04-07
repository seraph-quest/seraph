from pathlib import Path

from scripts.backend_test_shards import TestFile, assign_test_shards, discover_test_files, shard_for_index


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
