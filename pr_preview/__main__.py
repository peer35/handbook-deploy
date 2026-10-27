"""Entry point: `python -m pr_preview` serves the webhook app with waitress.

Runs as a single process on purpose: running builds are tracked in memory, so a
new push can cancel the build it supersedes.
"""

import logging
import os

from waitress import serve

from .config import Config
from .web import create_app


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(levelname)s [%(threadName)s] %(message)s")
    cfg = Config()
    app = create_app(cfg)
    logging.getLogger(__name__).info("listening on %s for %s, previews at %s",
                                     cfg.listen, cfg.repo, cfg.base_url)
    serve(app, listen=cfg.listen, threads=cfg.threads, ident="pr-preview-hook")


if __name__ == "__main__":
    main()
