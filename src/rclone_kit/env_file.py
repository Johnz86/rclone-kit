"""Minimal stdlib loader for the `.env` file convention this project uses.

Only the common `KEY=VALUE` case actually used across this codebase is
supported: multiline values, variable expansion/interpolation, and `export`
prefixes are all out of scope. Use a real `.env` parser if that ever
changes.
"""

import os
from pathlib import Path

_DEFAULT_ENV_FILENAME = ".env"
_COMMENT_PREFIX = "#"
_ASSIGNMENT_SEPARATOR = "="
_QUOTE_CHARACTERS = ("'", '"')


def load_env_file(path: Path | None = None) -> None:
    """Load `KEY=VALUE` assignments from `path` into `os.environ`.

    `path` defaults to `.env` in the current working directory. Does
    nothing when the file does not exist.

    Supported syntax, one entry per line:

    - `KEY=VALUE`, with surrounding whitespace stripped from both `KEY` and
      `VALUE`.
    - A `VALUE` wrapped in a single matching pair of single or double quotes
      has those quotes stripped.
    - Blank lines and lines whose first non-whitespace character is `#` are
      skipped as comments.

    Not supported: multiline values, `export` prefixes, variable
    expansion/interpolation, and inline comments trailing a value.

    Matches `python-dotenv`'s default `override=False` behavior: a key
    already present in `os.environ` is left untouched.
    """
    env_path = path if path is not None else Path(_DEFAULT_ENV_FILENAME)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(_COMMENT_PREFIX):
            continue
        if _ASSIGNMENT_SEPARATOR not in line:
            continue
        key, _, value = line.partition(_ASSIGNMENT_SEPARATOR)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTE_CHARACTERS:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
