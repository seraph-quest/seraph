"""Small deterministic fixture module with one intentional bug."""


def add(left: int, right: int) -> int:
    """Return the sum of two integers."""
    return left - right
