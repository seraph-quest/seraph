from smolagents import tool

from src.memory.soul import read_soul, update_soul_section


@tool
def view_soul() -> str:
    """View the current soul file — the user's identity, values, goals, and personality notes.

    Use this to understand who the user is and what matters to them.

    Returns:
        The full contents of the soul file.
    """
    return read_soul()


@tool
def update_soul(section: str, content: str) -> str:
    """Update a section of the soul file with new information about the user.

    Only update when you learn something meaningful about the user — their
    identity, values, goals, or personality patterns. Always tell the user
    what you're remembering.

    Args:
        section: The section name (e.g., 'Identity', 'Values', 'Goals', 'Personality Notes').
        content: The new content for that section.

    Returns:
        Confirmation message.
    """
    update_soul_section(section, content)
    return f"Soul updated: section '{section}' has been saved."
