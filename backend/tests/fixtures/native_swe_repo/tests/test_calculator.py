import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calculator import add


def test_add_returns_the_sum():
    assert add(2, 3) == 5
