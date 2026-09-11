"""Single-operator authentication boundary."""

from src.auth.service import AuthFailure, AuthenticatedOperator, auth_enabled

__all__ = ["AuthFailure", "AuthenticatedOperator", "auth_enabled"]
