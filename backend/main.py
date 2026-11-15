import uvicorn

from src.app import create_app

# Importing this module validates auth configuration. Local no-login mode
# (OPERATOR_AUTH_LOCAL_NO_LOGIN) refuses this entry point: ``uvicorn.run`` from
# Python hides its bind from the startup loopback check, which accepts only a
# uvicorn command line with an explicit loopback --host. Use
# ``./manage.sh -e dev local up`` for that mode.
app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8003, reload=True)
