"""Hermes-style execute_code runtime surface backed by the sandbox."""

from smolagents import tool

from src.tools.shell_tool import run_sandboxed_code


@tool
def execute_code(code: str, language: str = "python") -> str:
    """Execute bounded code inside Seraph's sandbox and return the output.

    This is the preferred Hermes-parity runtime surface for sandboxed code
    execution. It keeps the current Python-only sandbox contract for v1 while
    leaving broader shell/process work to the dedicated runtime slice.

    Args:
        code: The code to execute inside the sandbox.
        language: The execution language. Only `python` is supported in v1.

    Returns:
        The stdout output or a formatted error string from the sandbox.
    """
    return run_sandboxed_code(code, language=language)
