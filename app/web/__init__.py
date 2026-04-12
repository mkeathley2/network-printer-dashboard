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

    # Create tables, apply column migrations, and seed default admin
    with app.app_context():
        from app import models as _models  # noqa: F401
        db.create_all()
        _run_migrations()
        logger.info("Database tables verified/created.")
        _seed_admin()
        _cleanup_stuck_scans()

    logger.info("Application created. Listening on port %d", cfg.app.port)
    return app


def _run_migrations() -> None:
    """
    Apply any missing column additions to existing tables.
    Safe to run on every startup — each statement is skipped if the column
    already exists (MariaDB/MySQL ignore duplicate-column errors).
    """
    from sqlalchemy import text
    migrations = [
        # Audit log table index (table created by db.create_all, index may not exist)
        "ALTER TABLE audit_log ADD INDEX ix_audit_log_occurred_at (occurred_at)",
        "ALTER TABLE audit_log ADD INDEX ix_audit_log_action (action)",
        # Expand vendor enum to include ricoh
        "ALTER TABLE printers MODIFY COLUMN vendor ENUM('hp','brother','canon','kyocera','ricoh','generic') NOT NULL DEFAULT 'generic'",
        # Expand snmp_version enum to include v1
        "ALTER TABLE printers MODIFY COLUMN snmp_version ENUM('2c','3','1') NOT NULL DEFAULT '2c'",
        # Per-printer alert threshold overrides
        "ALTER TABLE printers ADD COLUMN supply_warn_pct SMALLINT NULL",
        "ALTER TABLE printers ADD COLUMN supply_crit_pct SMALLINT NULL",
        # Location + custom asset fields
        "ALTER TABLE printers ADD COLUMN location_id BIGINT NULL",
        "ALTER TABLE printers ADD COLUMN assigned_person VARCHAR(512) NULL",
        "ALTER TABLE printers ADD COLUMN sql_number VARCHAR(128) NULL",
        "ALTER TABLE printers ADD COLUMN assigned_computer VARCHAR(255) NULL",
        "ALTER TABLE printers ADD COLUMN phone_ext VARCHAR(32) NULL",
        "ALTER TABLE printers ADD COLUMN printer_web_username VARCHAR(255) NULL",
        "ALTER TABLE printers ADD COLUMN printer_web_password VARCHAR(512) NULL",
    ]
    with db.engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                # Column already exists — safe to ignore
                pass

        # Migrate existing PrinterGroup rows → Location rows (one-time, skipped if locations exist)
        try:
            result = conn.execute(text("SELECT COUNT(*) FROM locations"))
            if result.scalar() == 0:
                conn.execute(text(
                    "INSERT IGNORE INTO locations (name, description, created_at) "
                    "SELECT name, description, created_at FROM printer_groups"
                ))
                conn.commit()
            # Copy group_id → location_id for any printers that have a group but no location
            conn.execute(text(
                "UPDATE printers p "
                "JOIN printer_groups g ON p.group_id = g.id "
                "JOIN locations l ON g.name = l.name "
                "SET p.location_id = l.id "
                "WHERE p.location_id IS NULL AND p.group_id IS NOT NULL"
            ))
            conn.commit()
        except Exception as e:
            logger.debug("Group→Location migration skipped or already done: %s", e)


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


def _cleanup_stuck_scans() -> None:
    """Mark any scans still 'running' as 'failed' — they were orphaned by a server restart."""
    from app.models import DiscoveryScan
    updated = db.session.query(DiscoveryScan).filter_by(status="running").update({"status": "failed"})
    if updated:
        db.session.commit()
        logger.info("Marked %d orphaned discovery scan(s) as failed.", updated)
