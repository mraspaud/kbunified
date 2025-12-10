"""Utility module to extract Slack credentials from Firefox profiles.
Adapted from: https://github.com/p_hue/slack-token-extractor
"""

import json
import logging
import shutil
import sqlite3
from configparser import ConfigParser
from contextlib import contextmanager
from pathlib import Path
from tempfile import gettempdir

# Try importing snappy (user confirmed existence)
try:
    from snappy import snappy
    SNAPPY_AVAILABLE = True
except ImportError:
    SNAPPY_AVAILABLE = False

logger = logging.getLogger("slack_auth")

@contextmanager
def sqlite3_connect(path):
    # Connect in immutable mode to avoid locking the browser DB
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    try:
        yield con
    finally:
        con.close()

def get_firefox_profile_path():
    """Locate the default Firefox profile directory."""
    home = Path.home()
    # Check common Linux paths (Snap vs Native)
    possible_roots = [
        home / ".mozilla/firefox",
        home / "snap/firefox/common/.mozilla/firefox"
    ]

    browser_data = next((p for p in possible_roots if p.exists()), None)
    if not browser_data:
        logger.warning("No Firefox directory found.")
        return None

    # Parse profiles.ini to find the default
    ini_path = browser_data / "profiles.ini"
    default_path = None

    if ini_path.exists():
        parser = ConfigParser()
        parser.read(ini_path)
        for section in parser.sections():
            if "Default" in parser[section]:
                path = parser[section]["Default"]
                candidate = browser_data / path
                if candidate.exists():
                    default_path = candidate
                    break

    return default_path

def get_slack_credentials():
    """Attempt to extract Slack credentials (token, d-cookie, ds-cookie).
    Returns: (token, d_cookie, ds_cookie) or (None, None, None)
    """
    if not SNAPPY_AVAILABLE:
        logger.error("python-snappy not installed. Cannot decrypt Slack local storage.")
        return None, None, None

    profile_path = get_firefox_profile_path()
    if not profile_path:
        return None, None, None

    logger.debug(f"Scanning Firefox profile: {profile_path}")

    # 1. Extract Cookies (d, d-s)
    cookies_db = profile_path / "cookies.sqlite"
    d_cookie = None
    ds_cookie = None

    if cookies_db.exists():
        # Copy DB to temp to avoid locks
        temp_db = Path(gettempdir()) / "solaria_cookies.sqlite"
        shutil.copy2(cookies_db, temp_db)

        try:
            query = "SELECT name, value FROM moz_cookies WHERE host = '.slack.com' AND name IN ('d', 'd-s')"
            with sqlite3_connect(temp_db) as con:
                rows = con.execute(query).fetchall()
                for name, value in rows:
                    if name == "d": d_cookie = value
                    elif name == "d-s": ds_cookie = value
        except Exception as e:
            logger.error(f"Cookie extraction failed: {e}")
            raise
        finally:
            if temp_db.exists(): temp_db.unlink()

    if not d_cookie:
        logger.warning("Slack 'd' cookie not found in Firefox.")
        return None, None, None

    # 2. Extract Token (xoxc) from Local Storage
    # Path: storage/default/https+++app.slack.com/ls/data.sqlite
    ls_path = profile_path / "storage/default/https+++app.slack.com/ls/data.sqlite"
    token = None

    if ls_path.exists():
        try:
            # Query key: localConfig_v2
            query = "SELECT compression_type, conversion_type, value FROM data WHERE key = 'localConfig_v2'"

            with sqlite3_connect(ls_path) as con:
                row = con.execute(query).fetchone()
                if row:
                    is_compressed, conversion, payload = row

                    if is_compressed:
                        payload = snappy.decompress(payload)

                    # Decode based on conversion type (1=utf-8, else utf-16 usually)
                    if conversion == 1:
                        json_str = payload.decode("utf-8")
                    else:
                        json_str = payload.decode("utf-16")

                    data = json.loads(json_str)

                    # Iterate teams to find a valid token
                    teams = data.get("teams", {})
                    for team in teams.values():
                        candidate = team.get("token")
                        if candidate and candidate.startswith("xoxc-"):
                            token = candidate
                            # We grab the first valid one. Logic could be improved to match specific Team ID.
                            break
        except Exception as e:
            logger.error(f"Token extraction failed: {e}")
            raise

    if not token:
        logger.warning("Slack xoxc token not found in Local Storage.")

    return token, d_cookie, ds_cookie
