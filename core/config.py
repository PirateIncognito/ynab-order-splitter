"""Loading/writing core secrets (core/.env)."""

from pathlib import Path

from dotenv import dotenv_values, set_key

CORE_DIR = Path(__file__).resolve().parent
ENV_PATH = CORE_DIR / ".env"

YNAB_ACCESS_TOKEN_KEY = "YNAB_ACCESS_TOKEN"


def get_env_var(key: str, path: Path = ENV_PATH) -> str | None:
    """Read one secret from a .env file (core/.env by default; plugins pass
    their own plugins/<name>/.env). Secrets are never copied into the
    process environment, so child processes (e.g. the Playwright browser)
    don't inherit them."""
    return dotenv_values(path).get(key) if path.exists() else None


def set_env_var(key: str, value: str, path: Path = ENV_PATH) -> None:
    # Secrets: readable by this user only, including files created before
    # this was enforced.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    set_key(str(path), key, value)


def get_ynab_access_token() -> str | None:
    return get_env_var(YNAB_ACCESS_TOKEN_KEY)


def set_ynab_access_token(token: str) -> None:
    set_env_var(YNAB_ACCESS_TOKEN_KEY, token)
