import os

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/seraph-test")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("DEPLOYMENT_ENVIRONMENT", "test")
os.environ.setdefault("OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS", "true")
