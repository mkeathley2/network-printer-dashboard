"""
Application entry point.
Creates the Flask app, wires up APScheduler, then starts the server.
"""
from __future__ import annotations

import logging
import os

from app.core.extensions import scheduler
from app.web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = create_app()


def _scheduled_poll() -> None:
    """Wrapper so the scheduler job runs inside the Flask app context."""
    with app.app_context():
        try:
            from app.core.config import config
            from app.core.database import get_db
            from app.scanner.poller import poll_all_printers

            with get_db() as db_session:
                poll_all_printers(db_session)
        except Exception:
            logger.exception("Error during scheduled poll")


# Wire up the hourly poll job
from app.core.config import config  # noqa: E402 (after app creation)

scheduler.add_job(
    _scheduled_poll,
    trigger="interval",
    minutes=config.polling.interval_minutes,
    id="hourly_poll",
    replace_existing=True,
)

scheduler.start()
logger.info("Scheduler started. Poll interval: %d minutes", config.polling.interval_minutes)

if __name__ == "__main__":
    # Development server only; production uses gunicorn
    app.run(
        host="0.0.0.0",
        port=config.app.port,
        debug=config.app.debug,
        use_reloader=False,  # CRITICAL: reloader spawns second process → double scheduler
    )
