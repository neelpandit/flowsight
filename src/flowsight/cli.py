import logging
import sys

import uvicorn

from flowsight.api.app import create_app
from flowsight.config import ConfigError, load_settings


def main() -> None:
    """`flowsight serve`: runs the HTTP API, configured by environment variables or .env."""
    if sys.argv[1:] != ["serve"]:
        sys.exit("usage: flowsight serve")
    try:
        settings = load_settings()
    except ConfigError as error:
        sys.exit(str(error))
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level
    )
