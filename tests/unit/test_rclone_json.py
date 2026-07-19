"""
Unit test file.
"""

import unittest

import pytest

from rclone_kit import Config
from rclone_kit.exceptions import ConfigParseError

TEXT = """
{
    "dst": {
        "type": "s3",
        "bucket": "bucket",
        "endpoint": "https://s3.amazonaws.com",
        "access_key_id": "access key",
        "access_secret_key": "access secret key"
    }
}
"""

JSON_DATA = {
    "dst": {
        "type": "s3",
        "bucket": "bucket",
        "endpoint": "https://s3.amazonaws.com",
        "access_key_id": "access key",
        "access_secret_key": "access secret key",
    }
}


class MainTester(unittest.TestCase):
    """Main tester class."""

    def test_json_to_rclone(self) -> None:
        """Test command line interface (CLI)."""
        rclone_conf = Config.from_json(JSON_DATA)
        self.assertIsInstance(rclone_conf, Config)


def test_from_json_raises_config_parse_error_for_non_mapping_section() -> None:
    with pytest.raises(ConfigParseError):
        Config.from_json({"dst": "not-a-mapping"})


if __name__ == "__main__":
    unittest.main()
