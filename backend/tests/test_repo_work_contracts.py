"""Closed repository selections reject command and authority expansion."""
import pytest
from pydantic import ValidationError

from src.workflows.repo_repair import RepoIteration, RepoWorkInput, compile_repo_work_input


def test_existing_repair_parser_retains_two_closed_shapes_without_authority():
    from src.work_board.dispatcher import validate_capability_input, TypedInputError
    from src.workflows.repo_repair import RepoRepairInput
    work = selection()
    assert validate_capability_input('engineering.repo-repair.v1', work) == work
    legacy = RepoRepairInput(repository_path='projects/example',
        problem_statement='Correct addition.', source_paths=['calculator.py'],
        allowed_paths=['calculator.py', 'tests/test_calculator.py'],
        test_args=['-q', 'tests/test_calculator.py'], acceptance_criteria=['Actual checks pass.']).model_dump(mode='json')
    assert validate_capability_input('engineering.repo-repair.v1', legacy) == legacy
    with pytest.raises(TypedInputError):
        validate_capability_input('engineering.repo-repair.v1', work | {'repository_path': 'projects/example'})
    with pytest.raises(TypedInputError):
        validate_capability_input('engineering.repo-repair.v1', work | {'native_binding': {}})


def selection(**changes):
    value = {
        "repository_ref": "projects/example", "base_commit": "a" * 40,
        "intent": "Correct addition for negative operands.",
        "allowed_paths": ["calculator.py", "tests/test_calculator.py"],
        "language_profile": "test_python", "requested_checks": ["test"],
        "limits": {"max_iterations": 3, "max_total_seconds": 900, "max_cost_usd": 0.0010019},
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("field,value", [
    ("repository_ref", "/etc"), ("repository_ref", "projects/../secret"),
    ("repository_ref", "projects/./example"), ("repository_ref", "projects//example"),
    ("repository_ref", "https://example.invalid/repo"),
    ("allowed_paths", [".env.dev"]), ("allowed_paths", [".git/config"]),
    ("allowed_paths", ["calculator.py", "calculator.py"]),
    ("base_commit", "a" * 7), ("base_commit", "HEAD"),
    ("intent", "é" * 2001),
    ("requested_checks", ["test", "test"]), ("requested_checks", ["pytest -q"]),
    ("requested_checks", ["test; curl example.invalid"]),
    ("requested_checks", ["build"]), ("language_profile", "shell"),
])
def test_rejects_unreviewable_selection(field, value):
    with pytest.raises(ValidationError):
        RepoWorkInput.model_validate(selection(**{field: value}))


@pytest.mark.parametrize("field,value", [
    ("max_iterations", 4), ("max_iterations", True),
    ("max_total_seconds", 901), ("max_total_seconds", "900"),
    ("max_cost_usd", float("inf")), ("max_cost_usd", float("nan")),
    ("max_cost_usd", -1), ("max_cost_usd", True),
])
def test_limits_are_strict_original_bounds(field, value):
    data = selection()
    data["limits"][field] = value
    with pytest.raises(ValidationError):
        RepoWorkInput.model_validate(data)


def test_closed_inputs_cannot_supply_owner_or_commands():
    with pytest.raises(ValidationError):
        RepoWorkInput.model_validate(selection(owner="operator", test_args=["sh", "-c", "true"]))
    data = selection()
    data["limits"]["max_calls"] = 12
    with pytest.raises(ValidationError):
        RepoWorkInput.model_validate(data)
    work = RepoWorkInput.model_validate(selection())
    assert work.limits.max_cost_microusd == 1001
    assert len(RepoWorkInput.model_fields) == 7


def test_compiler_requires_actual_base_profile_and_named_allowed_test_path():
    work = RepoWorkInput.model_validate(selection())
    kwargs = dict(verified_base_commit=work.base_commit, selected_executor_profile="repo-python-pytest-v1",
        inspected_source_paths=("calculator.py",), inspected_python_test_paths=("tests/test_calculator.py",))
    repair = compile_repo_work_input(work, **kwargs)
    assert repair.test_args == ["pytest", "-q", "tests/test_calculator.py"]
    assert repair.problem_statement == work.intent
    for key, value in [("verified_base_commit", "b" * 40),
        ("selected_executor_profile", "repo-node24-npm-v1"),
        ("inspected_python_test_paths", ("elsewhere.py",)),
        ("inspected_python_test_paths", ())]:
        with pytest.raises(ValueError):
            compile_repo_work_input(work, **{**kwargs, key: value})


def test_node_selections_compile_to_existing_fixed_direct_command_plan():
    work = RepoWorkInput.model_validate(selection(language_profile="test_node", requested_checks=["test", "build"],
        allowed_paths=["calculator.js", "calculator.test.js"]))
    repair = compile_repo_work_input(work, verified_base_commit=work.base_commit,
        selected_executor_profile="repo-node24-npm-v1", inspected_source_paths=("calculator.js",))
    assert repair.test_args == ["npm", "run", "build", "test"]


def test_iteration_projection_cannot_carry_logs_or_claim_authority():
    value = dict(index=1, input_tree_digest="a" * 64, patch_digest="b" * 64,
        command_refs=["command:1"], result_artifacts=["artifact:1"])
    RepoIteration.model_validate(value)
    assert len(RepoIteration.model_fields) == 5
    for change in ({"index": True}, {"index": 4}, {"stdout": "private log"},
        {"result_artifacts": ["/workspace/private.patch"]}, {"command_refs": ["command:1", "command:1"]},
        {"approval_id": "approval:1"}):
        with pytest.raises(ValidationError):
            RepoIteration.model_validate({**value, **change})
