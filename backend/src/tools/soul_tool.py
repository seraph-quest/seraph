from smolagents import tool

from src.memory.soul import read_soul, update_soul_section


@tool
def view_soul() -> str:
    """View the current guardian record — the user's identity, values, priorities, and personality notes.

    Use this to understand who the user is and what matters to them.

    Returns:
        The full contents of the guardian record.
    """
    return read_soul()


@tool
def update_soul(section: str, content: str) -> str:
    """Update a section of the guardian record with new information about the user.

    Only update when you learn something meaningful about the user — their
    identity, values, priorities, or personality patterns. Always tell the user
    what you're remembering.

    Args:
        section: The section name (e.g., 'Identity', 'Values', 'Goals', 'Personality Notes').
        content: The new content for that section.

    Returns:
        Confirmation message.
    """
    update_soul_section(section, content)
    return f"Guardian record updated: section '{section}' has been saved."
