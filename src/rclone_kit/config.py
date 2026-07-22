import contextlib
import json
from dataclasses import dataclass, field

from rclone_kit.exceptions import ConfigParseError


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
            with contextlib.suppress(
                json.JSONDecodeError, TypeError, AttributeError, AssertionError
            ):
                self.text = _json_to_rclone_config_str_or_raise(self.text)

    @staticmethod
    def from_json(json_data: dict) -> "Config":
        """Build a `Config` from a JSON dict of `{section: {key: value}}`.

        Raises `ConfigParseError` when `json_data` isn't shaped that way.
        """
        return json_to_rclone_config(json_data)

    def parse(self) -> Parsed:
        return Parsed.parse(self.text)


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
