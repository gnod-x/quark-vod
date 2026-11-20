"""ASGI application factory for source, wheel and container installations."""


def create_app():
    # Import only after the CLI has applied startup options to the environment.
    from main import app
    return app
