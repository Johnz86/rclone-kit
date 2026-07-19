import logging
import sys
from threading import Event

_DEFAULT_LOGGING_INITIALISED = Event()


def setup_default_logging():
    """Set up default logging configuration if none exists."""
    if _DEFAULT_LOGGING_INITIALISED.is_set():
        return
    _DEFAULT_LOGGING_INITIALISED.set()
    if not logging.root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),
            ],
        )


def configure_logging(level=logging.INFO, log_file=None):
    """Configure logging for the rclone_kit package.

    Args:
        level: The logging level (default: logging.INFO)
        log_file: Optional path to a log file
    """
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
