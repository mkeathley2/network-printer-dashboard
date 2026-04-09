"""
Shared APScheduler instance.
Started in run.py after app creation to avoid double-start with Flask reloader.
"""
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(
    job_defaults={
        "coalesce": True,       # if a job misses its trigger, run it once, not many times
        "max_instances": 1,     # never run the same job concurrently
    }
)
