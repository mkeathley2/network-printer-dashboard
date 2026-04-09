"""
Loads configuration from config.yaml (non-secret settings) and environment
variables (secrets). Exposes a single `config` singleton used across the app.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class SnmpConfig:
    timeout: int = 3
    retries: int = 2
    community_v2c: str = "public"


@dataclass
class PollingConfig:
    interval_minutes: int = 60
    poll_workers: int = 20
    discovery_workers: int = 50
    discovery_timeout: int = 1


@dataclass
class AlertsConfig:
    toner_warning_pct: int = 15
    toner_critical_pct: int = 5
    drum_warning_pct: int = 10
    drum_critical_pct: int = 5
    replacement_jump_threshold: int = 20
    offline_after_failures: int = 3
    alert_to: List[str] = field(default_factory=list)


@dataclass
class DiscoveryConfig:
    default_community: str = "public"
    default_snmp_version: str = "2c"


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    from_addr: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.user and self.password)


@dataclass
class AppConfig:
    port: int = 7070
    debug: bool = False


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    snmp: SnmpConfig = field(default_factory=SnmpConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    db_url: str = ""
    secret_key: str = "dev-insecure-key"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_config(yaml_path: Optional[str] = None) -> Config:
    """Load config from YAML file + environment variables."""
    path = Path(yaml_path or os.environ.get("CONFIG_PATH", "/project/app/config.yaml"))
    raw = _load_yaml(path)

    cfg = Config()

    # --- app section ---
    app_raw = raw.get("app", {})
    cfg.app = AppConfig(
        port=int(app_raw.get("port", 7070)),
        debug=bool(app_raw.get("debug", False)),
    )

    # --- snmp section ---
    snmp_raw = raw.get("snmp", {})
    cfg.snmp = SnmpConfig(
        timeout=int(snmp_raw.get("timeout", 3)),
        retries=int(snmp_raw.get("retries", 2)),
        community_v2c=snmp_raw.get("community_v2c", "public"),
    )

    # --- polling section ---
    poll_raw = raw.get("polling", {})
    cfg.polling = PollingConfig(
        interval_minutes=int(poll_raw.get("interval_minutes", 60)),
        poll_workers=int(poll_raw.get("poll_workers", 20)),
        discovery_workers=int(poll_raw.get("discovery_workers", 50)),
        discovery_timeout=int(poll_raw.get("discovery_timeout", 1)),
    )

    # --- alerts section ---
    alert_raw = raw.get("alerts", {})
    cfg.alerts = AlertsConfig(
        toner_warning_pct=int(alert_raw.get("toner_warning_pct", 15)),
        toner_critical_pct=int(alert_raw.get("toner_critical_pct", 5)),
        drum_warning_pct=int(alert_raw.get("drum_warning_pct", 10)),
        drum_critical_pct=int(alert_raw.get("drum_critical_pct", 5)),
        replacement_jump_threshold=int(alert_raw.get("replacement_jump_threshold", 20)),
        offline_after_failures=int(alert_raw.get("offline_after_failures", 3)),
        alert_to=list(alert_raw.get("alert_to", [])),
    )

    # --- discovery section ---
    disc_raw = raw.get("discovery", {})
    cfg.discovery = DiscoveryConfig(
        default_community=disc_raw.get("default_community", "public"),
        default_snmp_version=disc_raw.get("default_snmp_version", "2c"),
    )

    # --- secrets from environment ---
    cfg.db_url = os.environ.get("DATABASE_URL", "")
    cfg.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-key-change-me")

    cfg.smtp = SmtpConfig(
        host=os.environ.get("SMTP_HOST", ""),
        port=int(os.environ.get("SMTP_PORT", "587")),
        user=os.environ.get("SMTP_USER", ""),
        password=os.environ.get("SMTP_PASSWORD", ""),
        from_addr=os.environ.get("SMTP_FROM", ""),
    )

    return cfg


# Module-level singleton — populated when create_app() calls load_config()
config: Config = Config()
