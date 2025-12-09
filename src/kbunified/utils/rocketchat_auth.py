"""Utility module to extract Rocket.Chat credentials from Firefox profiles."""

import json  # <--- NEW
import logging
import shutil
import sqlite3
from configparser import ConfigParser
from contextlib import contextmanager
from pathlib import Path
from tempfile import gettempdir

logger = logging.getLogger("rocketchat_auth")

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

def get_firefox_ls_folder_name(domain: str) -> str:
    """Convert a domain (and optional port) to Firefox LS folder format.
    
    Examples:
        chat.example.com -> https+++chat.example.com
        chat.example.com:3000 -> https+++chat.example.com++3000
    """
    # Simple heuristic: assume https unless obviously local/http
    protocol = "https"
    if domain.startswith("http:"):
        protocol = "http"
        domain = domain.replace("http://", "")
    elif domain.startswith("https:"):
        protocol = "https"
        domain = domain.replace("https://", "")

    # Replace colons with ++
    safe_domain = domain.replace(":", "++")

    return f"{protocol}+++{safe_domain}"

def get_rocketchat_credentials(domain: str):
    """Attempt to extract Rocket.Chat credentials (uid, token, username).
    
    Returns: (user_id, auth_token, username) or (None, None, None)
    """
    profile_path = get_firefox_profile_path()
    if not profile_path:
        return None, None, None

    # Construct the Local Storage path
    # Path: storage/default/{encoded_domain}/ls/data.sqlite
    folder_name = get_firefox_ls_folder_name(domain)
    ls_path = profile_path / "storage/default" / folder_name / "ls/data.sqlite"

    if not ls_path.exists():
        logger.debug(f"LS DB not found at {ls_path}")
        return None, None, None

    logger.debug(f"Scanning Rocket.Chat LS: {ls_path}")

    # Copy DB to temp to avoid locks
    temp_db = Path(gettempdir()) / f"solaria_rc_{folder_name}.sqlite"
    shutil.copy2(ls_path, temp_db)

    uid = None
    token = None
    username = None

    try:
        with sqlite3_connect(temp_db) as con:
            cursor = con.cursor()

            # We look for the keys.
            # Meteor.userId / Meteor.loginToken are strings.
            # Meteor.user is a JSON object string (if present).
            keys_to_fetch = ["Meteor.userId", "Meteor.loginToken", "Meteor.user"]

            # Create a placeholder string for the IN clause
            placeholders = ", ".join(["?"] * len(keys_to_fetch))
            query = f"SELECT key, value, conversion_type FROM data WHERE key IN ({placeholders})"

            cursor.execute(query, keys_to_fetch)
            rows = cursor.fetchall()

            for key, blob, conversion in rows:
                if conversion == 1:
                    val = blob.decode("utf-8")
                else:
                    val = blob.decode("utf-16")

                if key == "Meteor.userId":
                    uid = val
                elif key == "Meteor.loginToken":
                    token = val
                elif key == "Meteor.user":
                    try:
                        user_obj = json.loads(val)
                        if isinstance(user_obj, dict):
                            username = user_obj.get("username")
                    except json.JSONDecodeError:
                        pass

    except Exception as e:
        logger.error(f"RC Credential extraction failed: {e}")
    finally:
        if temp_db.exists():
            temp_db.unlink()

    if uid and token:
        logger.info(f"Successfully extracted Rocket.Chat credentials for {domain}")
        return uid, token, username

    return None, None, None
