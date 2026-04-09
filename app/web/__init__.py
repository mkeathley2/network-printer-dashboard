"""Flask application factory."""
from __future__ import annotations

import logging

from flask import Flask
from flask_login import LoginManager
from werkzeug.security import generate_password_hash

from app.core import config as config_module
from app.core.config import load_config
from app.core.database import db, init_standalone_engine

logger = logging.getLogger(__name__)


def create_app(yaml_path: str | None = None) -> Flask:
    """Create and configure the Flask application."""
    cfg = load_config(yaml_path)
    config_module.config = cfg

    app = Flask(__name__, template_folder="templates", static_folder="static")

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

    # Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str):
        from app.models import User
        return db.session.get(User, int(user_id))

    # Register blueprints
    from app.web.routes.auth import bp as auth_bp
    from app.web.routes.dashboard import bp as dashboard_bp
    from app.web.routes.printers import bp as printers_bp
    from app.web.routes.discovery import bp as discovery_bp
    from app.web.routes.history import bp as history_bp
    from app.web.routes.alerts import bp as alerts_bp
    from app.web.routes.api import bp as api_bp
    from app.web.routes.config import bp as config_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(printers_bp)
    app.register_blueprint(discovery_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(config_bp)

    # Register error handlers
    from app.web.routes.auth import register_error_handlers
    register_error_handlers(app)

    # Inject removed_count into every template for the navbar badge
    @app.context_processor
    def inject_globals():
        try:
            from app.models import Printer
            count = db.session.query(Printer).filter_by(is_active=False).count()
            return {"removed_count": count}
        except Exception:
            return {"removed_count": 0}

    # Create tables and seed default admin
    with app.app_context():
        from app import models as _models  # noqa: F401
        db.create_all()
        logger.info("Database tables verified/created.")
        _seed_admin()

    logger.info("Application created. Listening on port %d", cfg.app.port)
    return app


def _seed_admin() -> None:
    """Create default admin user if no users exist."""
    from app.models import User
    if db.session.query(User).first():
        return
    admin = User(
        username="admin",
        role="admin",
        password_hash=generate_password_hash("admin"),
    )
    db.session.add(admin)
    db.session.commit()
    logger.info("Default admin user created (username: admin, password: admin)")
