"""Persistent app configuration stored in %APPDATA%/SpiritSeeker/config.json.

Holds the auto-generated Soulseek credentials (the Soulseek network creates an
account on first login, no registration or API key needed) and UI preferences.
"""
import json
import os
import random
import string
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "SpiritSeeker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path() -> Path:
    return config_dir() / "config.json"


def _generate_credentials() -> dict:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    password = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    return {"username": f"spiritseeker_{suffix}", "password": password}


DEFAULTS = {
    "soulseek_username": "",
    "soulseek_password": "",
    # When enabled, log in with the user's own Soulseek account instead of
    # the auto-generated identity
    "use_custom_login": False,
    "custom_username": "",
    "custom_password": "",
    # Folders shared (uploaded) to other Soulseek users while running
    "shared_folders": [],
    "output_dir": str(Path.home() / "Music" / "SpiritSeeker"),
    "allow_lower_quality": False,
    "spectral_check": True,
    "listening_port": 61000,
    # Give up on a track after this long - only counts searching and queue
    # waiting; a download that is actually receiving data is never cut off
    "track_timeout_min": 8,
    # Cancel a download when no data arrives for this long
    "stall_timeout_sec": 60,
    # How many sources to try per track before giving up
    "max_attempts": 5,
    # Tracks downloaded at the same time (searches stay globally paced)
    "concurrent_downloads": 2,
    # After the playlist finishes, make one more pass over failed tracks
    "auto_retry_failed": True,
    # Re-download and replace songs that already exist in the save folder
    # instead of skipping them
    "overwrite_duplicates": False,
    # Toast + sound when a run completes
    "notify_on_finish": True,
    # Connect to Soulseek (and start the share scan) as soon as the app opens
    "connect_on_startup": True,
    # Has the first-run welcome been shown?
    "welcomed": False,
    # Suppress the automatic port-forwarding help popup after the user dismisses it
    "hide_port_help": False,
    "dark_mode": True,
}


class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        self.load()
        if not self.data["soulseek_username"]:
            creds = _generate_credentials()
            self.data["soulseek_username"] = creds["username"]
            self.data["soulseek_password"] = creds["password"]
            self.save()

    def load(self):
        try:
            with open(_config_path(), "r", encoding="utf-8") as f:
                stored = json.load(f)
            self.data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def save(self):
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def regenerate_credentials(self):
        """New random Soulseek identity (e.g. if the username is taken)."""
        creds = _generate_credentials()
        self.data["soulseek_username"] = creds["username"]
        self.data["soulseek_password"] = creds["password"]
        self.save()

    def effective_credentials(self) -> tuple[str, str, bool]:
        """(username, password, is_custom) for the account actually used."""
        if (self.data["use_custom_login"] and self.data["custom_username"]
                and self.data["custom_password"]):
            return (self.data["custom_username"],
                    self.data["custom_password"], True)
        return (self.data["soulseek_username"],
                self.data["soulseek_password"], False)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
