"""Flask application factory."""
from __future__ import annotations

import logging

from flask import Flask

from app.core import config as config_module
from app.core.config import load_config
from app.core.database import db, init_standalone_engine

logger = logging.getLogger(__name__)


def create_app(yaml_path: str | None = None) -> Flask:
    """Create and configure the Flask application."""
    # Load config into module-level singleton
    cfg = load_config(yaml_path)
    config_module.config = cfg

    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Flask config
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config["SQLALCHEMY_DATABASE_URI"] = cfg.db_url or "sqlite:///dev.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 3600,
        "pool_pre_ping": True,
    }

    # Initialize Flask-SQLAlchemy
    db.init_app(app)

    # Initialize standalone engine for background jobs
    init_standalone_engine(app.config["SQLALCHEMY_DATABASE_URI"])

    # Register blueprints
    from app.web.routes.dashboard import bp as dashboard_bp
    from app.web.routes.printers import bp as printers_bp
    from app.web.routes.discovery import bp as discovery_bp
    from app.web.routes.history import bp as history_bp
    from app.web.routes.alerts import bp as alerts_bp
    from app.web.routes.api import bp as api_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(printers_bp)
    app.register_blueprint(discovery_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(api_bp)

    # Inject removed_count into every template for the navbar badge
    @app.context_processor
    def inject_removed_count():
        try:
            from app.models import Printer
            count = db.session.query(Printer).filter_by(is_active=False).count()
            return {"removed_count": count}
        except Exception:
            return {"removed_count": 0}

    # Create tables on first start (idempotent)
    with app.app_context():
        # Import all models so SQLAlchemy knows about them
        # NOTE: must NOT use `import app.models` here — that would rebind
        # the local `app` variable (Flask instance) to the app package.
        from app import models as _models  # noqa: F401
        db.create_all()
        logger.info("Database tables verified/created.")

    logger.info("Application created. Listening on port %d", cfg.app.port)
    return app
