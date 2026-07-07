"""ASGI entry point: `uvicorn app.asgi:app`. Reads settings from the environment."""

from .config import Settings
from .main import create_app

app = create_app(Settings.from_env())
