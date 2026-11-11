"""photos2yt: Google Photos to YouTube Migration & Curation Tool."""

import logging

__version__ = "0.1.0"

# Library convention: without this, Python's logging.lastResort handler writes
# WARNING+ records to stderr whenever the application configured no logging,
# which duplicated every warning that was also printed via ``out``.
logging.getLogger(__name__).addHandler(logging.NullHandler())

# Set (via ``extra=``) on log records whose message was already printed to the
# terminal, so the CLI's stderr handler does not print it a second time.
ECHOED_TO_TERMINAL = "photos2yt_echoed_to_terminal"
