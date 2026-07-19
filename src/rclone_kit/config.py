import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rclone_kit.exceptions import ConfigParseError, RcloneCommandError

if TYPE_CHECKING:
    from rclone_kit import Rclone
    from rclone_kit.rclone_impl import RcloneImpl

_RCLONE_CONFIG_ENV_VAR = "RCLONE_CONFIG"
_CONFIG_PATHS_COMMAND = ("config", "paths")
_CONFIG_FILE_LABEL_PREFIX = "config file"


class ConfigDiscoveryError(Exception):
    """Raised when rclone configuration-file discovery fails outright rather
    than legitimately finding no configured file.

    Carries the original failure (an unresolvable executable, a non-zero
    `rclone config paths` exit, or an `OSError` starting the process) as
    `__cause__`.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Failed to discover the rclone config file: {detail}")


@dataclass
class Section:
    name: str
    data: dict[str, str] = field(default_factory=dict)

    def add(self, key: str, value: str) -> None:
        self.data[key] = value

    def type(self) -> str:
        return self.data["type"]

    def provider(self) -> str | None:
        return self.data.get("provider")

    def access_key_id(self) -> str:
        if "access_key_id" in self.data:
            return self.data["access_key_id"]
        elif "account" in self.data:
            return self.data["account"]
        raise KeyError("No access key found")

    def secret_access_key(self) -> str:

        if "secret_access_key" in self.data:
            return self.data["secret_access_key"]
        elif "key" in self.data:
            return self.data["key"]
        raise KeyError("No secret access key found")

    def endpoint(self) -> str | None:
        return self.data.get("endpoint")


@dataclass
class Parsed:
    sections: dict[str, Section]

    @staticmethod
    def parse(content: str) -> "Parsed":
        return parse_rclone_config(content)


class Config:
    """Rclone configuration dataclass."""

    def __init__(self, text: str | dict | None) -> None:
        self.text: str
        if text is None:
            self.text = ""
        elif isinstance(text, dict):
            self.text = _json_to_rclone_config_str_or_raise(text)
        else:
            self.text = text

        try:
            new_text = _json_to_rclone_config_str_or_raise(self.text)
            self.text = new_text
        except (json.JSONDecodeError, TypeError, AttributeError, AssertionError):
            pass

    @staticmethod
    def from_json(json_data: dict) -> "Config":
        """Build a `Config` from a JSON dict of `{section: {key: value}}`.

        Raises `ConfigParseError` when `json_data` isn't shaped that way.
        """
        return json_to_rclone_config(json_data)

    def parse(self) -> Parsed:
        return Parsed.parse(self.text)


def find_conf_file(
    rclone: "Rclone | RcloneImpl | None" = None,
    *,
    explicit_path: Path | None = None,
) -> Path | None:
    """Discover the rclone configuration file to use.

    Resolution order:

    1. `explicit_path`, when given: an authoritative override, returned
       without existence validation since the caller already knows the
       file's location (it may not exist yet, e.g. for a config about to be
       written).
    2. The `RCLONE_CONFIG` environment variable, when set.
    3. `rclone config paths`, invoked through `rclone`'s resolved executable
       when given, or a freshly resolved executable otherwise (never a
       directory, and never through a shell).

    Returns `None` when every step above finds no path — a valid outcome
    meaning rclone itself reports no configured config file. Raises
    `ConfigDiscoveryError` when discovery itself fails, such as when no
    rclone executable can be resolved at all or the resolved executable
    cannot be invoked.
    """
    if explicit_path is not None:
        return explicit_path

    if env_value := os.environ.get(_RCLONE_CONFIG_ENV_VAR):
        return Path(env_value)

    config_file_path = _discover_config_file_path(rclone)
    if config_file_path is not None and config_file_path.exists():
        return config_file_path
    return None


def _discover_config_file_path(rclone: "Rclone | RcloneImpl | None") -> Path | None:
    """Return the "Config file" path `rclone config paths` reports, without
    checking whether it exists on disk.

    When `rclone` is given, its own `config_paths()` is reused rather than
    spawning a second process. `rclone config paths` prints exactly three
    stable, ordered lines — "Config file", "Cache dir", "Temp dir" — so the
    first entry of that method's parsed result is always the config file
    entry.
    """
    from rclone_kit import Rclone
    from rclone_kit.rclone_impl import RcloneImpl

    if rclone is None:
        paths = _config_paths_via_resolved_executable()
        return paths[0] if paths else None

    rclone_impl = rclone.impl if isinstance(rclone, Rclone) else rclone
    if not isinstance(rclone_impl, RcloneImpl):
        raise TypeError(f"rclone must be an Rclone or RcloneImpl instance, got {type(rclone)!r}")

    try:
        paths = rclone_impl.config_paths()
    except RcloneCommandError as error:
        raise ConfigDiscoveryError("rclone config paths") from error
    return paths[0] if paths else None


def _config_paths_via_resolved_executable() -> list[Path]:
    from rclone_kit.runtime.exceptions import RcloneRuntimeError
    from rclone_kit.util import get_rclone_exe

    try:
        rclone_exe = get_rclone_exe(None)
    except RcloneRuntimeError as error:
        raise ConfigDiscoveryError("resolve an rclone executable") from error

    try:
        completed = subprocess.run(
            [str(rclone_exe), *_CONFIG_PATHS_COMMAND],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ConfigDiscoveryError(f"invoke {rclone_exe} config paths") from error

    return _parse_config_paths_output(completed.stdout)


def _parse_config_paths_output(stdout: str) -> list[Path]:
    """Parse the `Label: value` lines printed by `rclone config paths`."""
    paths: list[Path] = []
    for line in stdout.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().lower() == _CONFIG_FILE_LABEL_PREFIX:
            paths.append(Path(value.strip()))
    return paths


def parse_rclone_config(content: str) -> Parsed:
    """
    Parses an rclone configuration file and returns a list of RcloneConfigSection objects.

    Each section in the file starts with a line like [section_name]
    followed by key=value pairs.
    """
    sections: list[Section] = []
    current_section: Section | None = None

    lines = content.splitlines()
    for raw_line in lines:
        line = raw_line.strip()

        if not line or line.startswith(("#", ";")):
            continue

        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            current_section = Section(name=section_name)
            sections.append(current_section)
        elif "=" in line and current_section is not None:
            key, value = line.split("=", 1)
            current_section.add(key.strip(), value.strip())

    data: dict[str, Section] = {}
    for section in sections:
        data[section.name] = section
    return Parsed(sections=data)


def _json_to_rclone_config_str_or_raise(json_data: dict | str) -> str:
    """Convert JSON data to rclone config."""
    if isinstance(json_data, str):
        json_data = json.loads(json_data)
    assert isinstance(json_data, dict)
    out = ""
    for key, value in json_data.items():
        out += f"[{key}]\n"
        for k, v in value.items():
            out += f"{k} = {v}\n"
    return out


def json_to_rclone_config(json_data: dict) -> Config:
    """Build a `Config` from a JSON dict of `{section: {key: value}}`.

    Raises `ConfigParseError` when `json_data` isn't shaped that way.
    """
    try:
        text = _json_to_rclone_config_str_or_raise(json_data)
    except (json.JSONDecodeError, TypeError, AttributeError, AssertionError) as e:
        raise ConfigParseError(e) from e
    return Config(text=text)
