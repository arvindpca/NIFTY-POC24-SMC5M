
import os

def paper_mode_enabled():
    return os.getenv("PAPER_ONLY", "true").strip().lower() == "true"

def validate_config():
    if not paper_mode_enabled():
        raise RuntimeError(
            "Safety stop: PAPER_ONLY must be true. "
            "This application is for paper trading only."
        )
    return True
