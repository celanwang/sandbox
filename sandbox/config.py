"""Load local configuration for the command-line entry points."""

from pathlib import Path

from dotenv import load_dotenv


def load_local_env() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
