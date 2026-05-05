#!/usr/bin/env python3
"""
Feedly Enterprise Users Audit - Weekly permission change tracker

Fetches the enterprise user list from the Feedly API, strips all PII
(names, emails, avatars), and retains only user UUIDs alongside
non-identifying fields such as role, permissions, plan, and status.

Each run saves a timestamped JSON snapshot. Consecutive snapshots are
automatically diffed so that added/removed users and any permission or
role changes are surfaced in the output — suitable for capture by cron
and delivery via email or log aggregation.

© 2025 Feedly, Inc. All rights reserved.

DISCLAIMERS. THE API SCRIPTS ARE PROVIDED "AS IS" FOR YOUR INTERNAL BUSINESS
USE ONLY. THE ENTIRE RISK AS TO THE QUALITY AND PERFORMANCE OF THE API SCRIPTS
IS WITH YOU. YOU AGREE THAT YOUR USE OF THE API SCRIPTS WILL BE AT YOUR SOLE
RISK. TO THE FULLEST EXTENT PERMITTED BY LAW, FEEDLY DISCLAIMS ALL WARRANTIES,
EXPRESS OR IMPLIED, IN CONNECTION WITH THE API SCRIPTS AND YOUR USE THEREOF,
INCLUDING, WITHOUT LIMITATION, THE IMPLIED WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. FEEDLY MAKES NO
WARRANTIES OR REPRESENTATIONS ABOUT THE ACCURACY OR COMPLETENESS OF THE API
SCRIPTS AND NO REPRESENTATIONS THAT THE API SCRIPTS ARE NOT OTHERWISE
ENCUMBERED BY ANY THIRD PARTY LICENSE, INCLUDING ANY OPEN-SOURCE LICENSE.
FEEDLY ASSUMES NO LIABILITY OR RESPONSIBILITY FOR ANY: (1) ERRORS, MISTAKES,
OR INACCURACIES; (2) PERSONAL INJURY OR PROPERTY DAMAGE, OF ANY NATURE
WHATSOEVER, RESULTING FROM YOUR USE OF THE API SCRIPTS; (3) ANY UNAUTHORIZED
ACCESS TO OR USE OF API SCRIPTS; (4) ANY INTERRUPTION OR CESSATION OF
TRANSMISSION TO OR FROM THE API SCRIPTS; (5) ANY BUGS, VIRUSES, TROJAN HORSES,
OR THE LIKE WHICH MAY BE TRANSMITTED TO OR THROUGH THE API SCRIPTS BY ANY
THIRD PARTY; OR (6) ANY ERRORS OR OMISSIONS IN THE API SCRIPTS OR FOR ANY LOSS
OR DAMAGE OF ANY KIND INCURRED AS A RESULT OF THE USE OF THE API SCRIPTS.

LIMITATION OF LIABILITY. IN NO EVENT SHALL FEEDLY BE LIABLE FOR ANY DAMAGES.
FURTHER, IN NO EVENT SHALL FEEDLY BE LIABLE FOR ANY CONSEQUENTIAL, INCIDENTAL
OR INDIRECT DAMAGES, INCLUDING, WITHOUT LIMITATION, ANY LOSS OF DATA, OR LOSS
OF PROFITS OR LOST SAVINGS, ARISING OUT OF USE OF OR INABILITY TO USE THE
LICENSED PRODUCT, EVEN IF FEEDLY HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH
DAMAGES, OR FOR ANY CLAIM BY ANY THIRD PARTY.

YOU ACKNOWLEDGE THAT YOU HAVE READ AND UNDERSTAND THESE TERMS AND AGREE TO BE
BOUND BY THEM. YOU FURTHER AGREE THAT THESE TERMS ARE THE COMPLETE AND
EXCLUSIVE STATEMENT OF THE AGREEMENT BETWEEN YOU AND FEEDLY FOR THE USE OF THE
API SCRIPTS, AND THESE TERMS SUPERSEDE ANY PRIOR AGREEMENT, ORAL OR WRITTEN,
AND ANY OTHER COMMUNICATIONS RELATING TO THE SUBJECT MATTER HEREOF.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    print("Error: 'requests' library is required. Install with: pip install requests")
    sys.exit(1)

try:
    import yaml
except ImportError:
    yaml = None

# =============================================================================
# CONFIGURATION
# =============================================================================

# Known PII field names — stripped before any snapshot is written or printed.
# The script uses a blocklist so that unexpected non-PII fields are preserved.
_PII_FIELDS = {
    "email",
    "name",
    "firstName",
    "lastName",
    "fullName",
    "displayName",
    "givenName",
    "familyName",
    "picture",
    "avatar",
    "avatarUrl",
    "profileImage",
    "imageUrl",
    "photoUrl",
    "locale",
    "timeZone",
    "timezone",
    "phone",
    "phoneNumber",
}

BASE_URL = "https://api.feedly.com"
ENDPOINT = "/v3/enterprise/users"

MAX_RETRIES = 3
RETRY_DELAY = 2       # seconds between retries
RATE_LIMIT_DELAY = 1  # seconds after a successful call

SNAPSHOT_PREFIX = "enterprise_users_"
SNAPSHOT_SUFFIX = ".json"


# =============================================================================
# API KEY LOADING
# =============================================================================

def load_api_key() -> str:
    """
    Load API key from environment variable or .env file.
    Priority: FEEDLY_API_KEY env var > .env file > fallback placeholder
    """
    api_key = os.environ.get("FEEDLY_API_KEY")
    if api_key:
        return api_key

    env_locations = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]

    for env_path in env_locations:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("FEEDLY_API_KEY=") and not line.startswith("#"):
                            key = line.split("=", 1)[1].strip()
                            if (key.startswith('"') and key.endswith('"')) or \
                               (key.startswith("'") and key.endswith("'")):
                                key = key[1:-1]
                            if key:
                                return key
            except Exception:
                pass

    return "APIKEYHERE"


API_KEY = load_api_key()


# =============================================================================
# CONFIG FILE LOADING
# =============================================================================

def load_config(config_path: Optional[str]) -> Dict[str, Any]:
    """
    Load settings from a YAML config file.
    Returns an empty dict if no path is given, yaml is unavailable, or the file
    cannot be parsed — CLI arguments and environment variables always take priority.
    """
    if not config_path:
        return {}

    if yaml is None:
        print("Warning: PyYAML not installed — config file ignored. Install with: pip install pyyaml")
        return {}

    path = Path(config_path)
    if not path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        return data
    except yaml.YAMLError as exc:
        print(f"Error: Could not parse config file: {exc}")
        sys.exit(1)


def resolve_api_key(config: Dict[str, Any]) -> str:
    """Return API key from environment/env-file (highest priority) or config file."""
    env_key = load_api_key()
    if env_key != "APIKEYHERE":
        return env_key
    config_key = config.get("feedly", {}).get("api_token", "").strip()
    if config_key and config_key not in ("YOUR_FEEDLY_API_TOKEN_HERE", "APIKEYHERE"):
        return config_key
    return "APIKEYHERE"


# =============================================================================
# API CLIENT
# =============================================================================

class FeedlyEnterpriseClient:
    """Minimal client for the Feedly Enterprise Users endpoint."""

    def __init__(self, api_key: str, verbose: bool = False):
        self.api_key = api_key
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        })

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"  [verbose] {message}")

    def get_enterprise_users(self) -> List[Dict[str, Any]]:
        """Fetch the full enterprise user list with retry logic."""
        url = f"{BASE_URL}{ENDPOINT}"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._log(f"GET {url} (attempt {attempt}/{MAX_RETRIES})")
                response = self.session.get(url, timeout=30)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", RETRY_DELAY * attempt))
                    print(f"  Rate limited. Waiting {retry_after}s before retry...")
                    time.sleep(retry_after)
                    continue

                if response.status_code == 401:
                    print("Error: Unauthorized. Check that FEEDLY_API_KEY is valid.")
                    sys.exit(1)

                if response.status_code == 403:
                    print("Error: Forbidden. The API key does not have enterprise user access.")
                    sys.exit(1)

                if response.status_code >= 400:
                    if attempt < MAX_RETRIES:
                        wait = RETRY_DELAY * attempt
                        print(f"  HTTP {response.status_code}. Retrying in {wait}s...")
                        time.sleep(wait)
                        continue
                    print(f"Error: API returned HTTP {response.status_code} after {MAX_RETRIES} attempts.")
                    sys.exit(1)

                data = response.json()
                time.sleep(RATE_LIMIT_DELAY)

                # The endpoint may return a list directly or wrap it in a key.
                if isinstance(data, list):
                    return data
                for candidate in ("users", "items", "members", "data", "results"):
                    if candidate in data and isinstance(data[candidate], list):
                        self._log(f"Unwrapped response from key '{candidate}'")
                        return data[candidate]

                # Unexpected structure — return as-is and let the caller handle it.
                self._log(f"Unexpected response shape: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                return data if isinstance(data, list) else []

            except requests.exceptions.ConnectionError:
                if attempt < MAX_RETRIES:
                    wait = RETRY_DELAY * attempt
                    print(f"  Connection error. Retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                print("Error: Could not connect to the Feedly API.")
                sys.exit(1)
            except requests.exceptions.Timeout:
                if attempt < MAX_RETRIES:
                    wait = RETRY_DELAY * attempt
                    print(f"  Request timed out. Retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                print("Error: Request timed out after multiple attempts.")
                sys.exit(1)
            except requests.exceptions.RequestException as exc:
                print(f"Error: {exc}")
                sys.exit(1)

        return []


# =============================================================================
# PII FILTERING
# =============================================================================

def strip_pii(user: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the user record with all PII fields removed."""
    return {k: v for k, v in user.items() if k not in _PII_FIELDS}


def anonymize_users(users: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip PII from every user record in the list."""
    return [strip_pii(u) for u in users]


# =============================================================================
# SNAPSHOT MANAGEMENT
# =============================================================================

def snapshot_filename(timestamp: datetime) -> str:
    return f"{SNAPSHOT_PREFIX}{timestamp.strftime('%Y%m%d_%H%M%S')}{SNAPSHOT_SUFFIX}"


def save_snapshot(users: List[Dict[str, Any]], output_dir: Path, timestamp: datetime) -> Path:
    """Write the anonymized user list to a timestamped JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / snapshot_filename(timestamp)
    payload = {
        "snapshot_timestamp": timestamp.isoformat(),
        "user_count": len(users),
        "users": users,
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    """Load and return a snapshot file, or None if it cannot be read."""
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        print(f"Warning: Could not read snapshot {path.name}: {exc}")
        return None


def find_previous_snapshot(output_dir: Path, current_filename: str) -> Optional[Path]:
    """Return the most recent snapshot file that is not the current one."""
    snapshots = sorted(
        output_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}"),
        key=lambda p: p.name,
        reverse=True,
    )
    for s in snapshots:
        if s.name != current_filename:
            return s
    return None


# =============================================================================
# DIFF / CHANGE DETECTION
# =============================================================================

def _user_id(user: Dict[str, Any]) -> Optional[str]:
    """Extract the UUID from a user record."""
    for key in ("id", "userId", "user_id", "uuid"):
        if key in user:
            return str(user[key])
    return None


def diff_snapshots(
    previous: List[Dict[str, Any]],
    current: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compare two anonymized user lists keyed by UUID.
    Returns a structured diff with added, removed, and changed entries.
    """
    prev_map = {_user_id(u): u for u in previous if _user_id(u)}
    curr_map = {_user_id(u): u for u in current if _user_id(u)}

    prev_ids = set(prev_map)
    curr_ids = set(curr_map)

    added = [curr_map[uid] for uid in sorted(curr_ids - prev_ids)]
    removed = [prev_map[uid] for uid in sorted(prev_ids - curr_ids)]

    changed = []
    for uid in sorted(prev_ids & curr_ids):
        prev_user = prev_map[uid]
        curr_user = curr_map[uid]
        field_diffs = {}
        all_keys = set(prev_user) | set(curr_user)
        for key in sorted(all_keys):
            pv = prev_user.get(key)
            cv = curr_user.get(key)
            if pv != cv:
                field_diffs[key] = {"previous": pv, "current": cv}
        if field_diffs:
            changed.append({"id": uid, "changes": field_diffs})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": {
            "total_previous": len(prev_map),
            "total_current": len(curr_map),
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
    }


def print_diff_report(diff: Dict[str, Any], previous_name: str, current_name: str) -> None:
    """Print a human-readable change report to stdout."""
    s = diff["summary"]
    print("\n" + "=" * 60)
    print("FEEDLY ENTERPRISE USER CHANGE REPORT")
    print("=" * 60)
    print(f"  Previous snapshot : {previous_name}")
    print(f"  Current snapshot  : {current_name}")
    print(f"  Users (previous)  : {s['total_previous']}")
    print(f"  Users (current)   : {s['total_current']}")
    print(f"  Added             : {s['added']}")
    print(f"  Removed           : {s['removed']}")
    print(f"  Changed           : {s['changed']}")
    print("=" * 60)

    if diff["added"]:
        print("\n[ADDED USERS]")
        for u in diff["added"]:
            uid = _user_id(u)
            rest = {k: v for k, v in u.items() if k not in ("id", "userId", "user_id", "uuid")}
            print(f"  + {uid}  {rest}")

    if diff["removed"]:
        print("\n[REMOVED USERS]")
        for u in diff["removed"]:
            uid = _user_id(u)
            rest = {k: v for k, v in u.items() if k not in ("id", "userId", "user_id", "uuid")}
            print(f"  - {uid}  {rest}")

    if diff["changed"]:
        print("\n[CHANGED USERS]")
        for entry in diff["changed"]:
            print(f"  ~ {entry['id']}")
            for field, delta in entry["changes"].items():
                print(f"      {field}: {delta['previous']!r} -> {delta['current']!r}")

    if not any([diff["added"], diff["removed"], diff["changed"]]):
        print("\n  No changes detected.")

    print()


# =============================================================================
# MAIN
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Feedly enterprise users, strip PII, save a timestamped snapshot, "
            "and report any changes since the last run."
        )
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to a YAML config file (default: none — uses env vars / .env file)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=None,
        help="Directory for snapshot files (default: ./snapshots, or value from config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and display results without writing any snapshot files",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print extra debug information",
    )
    parser.add_argument(
        "--no-diff",
        action="store_true",
        help="Skip the diff comparison and only save the new snapshot",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    now = datetime.now(timezone.utc)

    # CLI arg > config file > default
    output_dir = Path(
        args.output_dir
        or config.get("output", {}).get("snapshot_dir", "./snapshots")
    )

    api_key = resolve_api_key(config)
    if api_key == "APIKEYHERE":
        print(
            "Error: No API key found.\n"
            "Options:\n"
            "  1. Set the FEEDLY_API_KEY environment variable\n"
            "  2. Create a .env file with: FEEDLY_API_KEY=your_token_here\n"
            "  3. Set feedly.api_token in your config.yaml"
        )
        sys.exit(1)

    client = FeedlyEnterpriseClient(api_key=api_key, verbose=args.verbose)

    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] Fetching enterprise users...")
    raw_users = client.get_enterprise_users()
    print(f"  Retrieved {len(raw_users)} user(s).")

    anonymized = anonymize_users(raw_users)

    stripped_fields = set()
    for u in raw_users:
        stripped_fields.update(k for k in u if k in _PII_FIELDS)
    if stripped_fields and args.verbose:
        print(f"  [verbose] PII fields stripped: {sorted(stripped_fields)}")

    current_filename = snapshot_filename(now)

    if args.dry_run:
        print("\n[DRY RUN — no files written]\n")
        print(json.dumps({"user_count": len(anonymized), "users": anonymized}, indent=2))
        return

    current_path = save_snapshot(anonymized, output_dir, now)
    print(f"  Snapshot saved: {current_path}")

    if args.no_diff:
        return

    previous_path = find_previous_snapshot(output_dir, current_filename)
    if previous_path is None:
        print("  No previous snapshot found — this is the baseline run.")
        return

    previous_snapshot = load_snapshot(previous_path)
    if previous_snapshot is None:
        print(f"  Could not load previous snapshot ({previous_path.name}). Skipping diff.")
        return

    previous_users = previous_snapshot.get("users", [])
    diff = diff_snapshots(previous_users, anonymized)
    print_diff_report(diff, previous_path.name, current_filename)

    # Exit with code 1 when changes are detected — useful for alerting in cron jobs.
    if any([diff["added"], diff["removed"], diff["changed"]]):
        sys.exit(1)


if __name__ == "__main__":
    main()
