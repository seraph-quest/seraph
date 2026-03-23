"""Legacy shell execution alias backed by the sandboxed code runner."""

import logging

import httpx

from config.settings import settings
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)


def run_sandboxed_code(code: str, language: str = "python") -> str:
    """Execute code in a secure sandboxed environment and return the output.

    Use this tool to run Python code, perform calculations, process data,
    or test code snippets. The code runs in an isolated container with
    no network access and limited resources.

    Args:
        code: The code to execute. For Python, write it directly.
              For bash commands, wrap them in subprocess.run().
        language: The programming language. Currently only 'python' is supported.

    Returns:
        The stdout output of the code, or error messages if execution failed.
    """
    if language != "python":
        return f"Error: Only 'python' is currently supported, got '{language}'."

    # Limit code size to prevent DoS
    max_code_size = 100_000  # 100KB
    if len(code) > max_code_size:
        return f"Error: Code too large ({len(code)} bytes). Maximum is {max_code_size} bytes."

    sandbox_url = settings.sandbox_url
    try:
        with httpx.Client(timeout=settings.sandbox_timeout) as client:
            response = client.post(
                f"{sandbox_url}/eval",
                json={"input": code},
            )
            response.raise_for_status()
            result = response.json()

        stdout = result.get("stdout", "")
        returncode = result.get("returncode", -1)

        if returncode != 0:
            log_integration_event_sync(
                integration_type="sandbox",
                name="snekbox",
                outcome="returned_nonzero",
                details={
                    "returncode": returncode,
                    "stdout_length": len(stdout),
                    "stderr_present": bool(result.get("stderr", "")),
                },
            )
            stderr = result.get("stderr", "")
            if stderr:
                return f"Exit code {returncode}:\n{stdout}\n--- stderr ---\n{stderr}"
            return f"Exit code {returncode}:\n{stdout}" if stdout else f"Execution failed with exit code {returncode}."

        log_integration_event_sync(
            integration_type="sandbox",
            name="snekbox",
            outcome="succeeded",
            details={
                "returncode": returncode,
                "stdout_length": len(stdout),
            },
        )
        return stdout if stdout else "(no output)"

    except httpx.TimeoutException:
        log_integration_event_sync(
            integration_type="sandbox",
            name="snekbox",
            outcome="timed_out",
            details={"timeout_seconds": settings.sandbox_timeout},
        )
        return f"Error: Code execution timed out after {settings.sandbox_timeout}s."
    except httpx.ConnectError:
        log_integration_event_sync(
            integration_type="sandbox",
            name="snekbox",
            outcome="unavailable",
            details={"sandbox_url": sandbox_url},
        )
        return "Error: Sandbox service is not available. The snekbox container may not be running."
    except Exception as e:
        log_integration_event_sync(
            integration_type="sandbox",
            name="snekbox",
            outcome="failed",
            details={"error": str(e)},
        )
        logger.exception("Shell execution failed")
        return f"Error: {e}"


def shell_execute(code: str, language: str = "python") -> str:
    """Legacy alias for sandboxed code execution.

    `execute_code` is the preferred runtime surface. Keep this alias until
    existing skills, workflows, and user-authored packages finish migrating.

    Args:
        code: The code to execute inside the sandbox.
        language: The execution language. Only `python` is supported in v1.

    Returns:
        The stdout output or a formatted error string from the sandbox.
    """
    return run_sandboxed_code(code, language=language)
