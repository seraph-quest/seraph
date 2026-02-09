"""Shell execution tool — runs code in a sandboxed snekbox container."""

import logging

import httpx
from smolagents import tool

from config.settings import settings

logger = logging.getLogger(__name__)


@tool
def shell_execute(code: str, language: str = "python") -> str:
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
            stderr = result.get("stderr", "")
            if stderr:
                return f"Exit code {returncode}:\n{stdout}\n--- stderr ---\n{stderr}"
            return f"Exit code {returncode}:\n{stdout}" if stdout else f"Execution failed with exit code {returncode}."

        return stdout if stdout else "(no output)"

    except httpx.TimeoutException:
        return f"Error: Code execution timed out after {settings.sandbox_timeout}s."
    except httpx.ConnectError:
        return "Error: Sandbox service is not available. The snekbox container may not be running."
    except Exception as e:
        logger.exception("Shell execution failed")
        return f"Error: {e}"
