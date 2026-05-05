# Feedly Enterprise Users Audit

A Python script that fetches your Feedly Enterprise user list weekly, strips all personally identifiable information (PII), and tracks permission and role changes over time using timestamped snapshots.

## Overview

Each run:
1. Calls the [Feedly Enterprise Users API](https://developers.feedly.com/reference/listenterpriseusers)
2. Strips all PII from every record — only the user UUID and non-identifying fields (role, plan, status, etc.) are retained
3. Saves an anonymized JSON snapshot to a local directory
4. Diffs the new snapshot against the most recent previous one and prints a change report

The script exits with code `1` when changes are detected, making it straightforward to wire into alerting workflows from a cron job.

## Prerequisites

- Python 3.8+
- A Feedly Enterprise API token with permission to list users
- `requests` library (`pip install requests`)
- `pyyaml` library if using a config file (`pip install pyyaml`)

## Installation

```bash
git clone https://github.com/AMFeedlyCustomerScripts/feedly-enterprise-users-audit.git
cd feedly-enterprise-users-audit
pip install -r requirements.txt
```

## Configuration

### Option 1 — Environment variable (recommended for cron)

```bash
export FEEDLY_API_KEY="your_token_here"
```

### Option 2 — `.env` file

Create a `.env` file in the same directory as the script:

```
FEEDLY_API_KEY=your_token_here
```

### Option 3 — YAML config file

```bash
cp config.yaml.template config.yaml
# Edit config.yaml and set feedly.api_token
```

Then pass it at runtime:

```bash
python feedly_enterprise_users_audit.py --config config.yaml
```

> **Priority order:** environment variable > `.env` file > `config.yaml`

## Usage

```bash
# Standard run — saves snapshot and prints change report
python feedly_enterprise_users_audit.py

# Use a config file and custom snapshot directory
python feedly_enterprise_users_audit.py --config config.yaml --output-dir /var/data/feedly_snapshots

# Dry run — fetch and print anonymized data without saving anything
python feedly_enterprise_users_audit.py --dry-run

# Save snapshot only, skip the diff report
python feedly_enterprise_users_audit.py --no-diff

# Verbose output (shows stripped PII fields, API response shape, etc.)
python feedly_enterprise_users_audit.py --verbose
```

### All options

| Flag | Short | Description |
|------|-------|-------------|
| `--config` | `-c` | Path to YAML config file |
| `--output-dir` | `-o` | Directory for snapshot files (default: `./snapshots`) |
| `--dry-run` | | Print results without writing any files |
| `--no-diff` | | Save snapshot only, skip comparison |
| `--verbose` | `-v` | Extra debug output |

## Setting Up a Weekly Cron Job

```cron
# Run every Monday at 06:00 UTC
0 6 * * 1 /path/to/venv/bin/python /path/to/feedly_enterprise_users_audit.py \
    --config /path/to/config.yaml \
    --output-dir /var/data/feedly_snapshots \
    >> /var/log/feedly_audit.log 2>&1
```

Because the script exits with code `1` when changes are detected, you can use standard cron alerting (e.g. `MAILTO`) or wrap the call in a shell conditional to trigger a notification only when something has changed:

```bash
python feedly_enterprise_users_audit.py --config config.yaml
if [ $? -eq 1 ]; then
    # send alert, post to Slack, etc.
fi
```

## Output

### Snapshot files

Each run writes a file like `snapshots/enterprise_users_20260505_060000.json`:

```json
{
  "snapshot_timestamp": "2026-05-05T06:00:00+00:00",
  "user_count": 42,
  "users": [
    {
      "id": "a1b2c3d4-...",
      "role": "viewer",
      "plan": "enterprise",
      "status": "active"
    }
  ]
}
```

Names, emails, avatars, and all other PII are never written to disk.

### Change report (stdout)

```
============================================================
FEEDLY ENTERPRISE USER CHANGE REPORT
============================================================
  Previous snapshot : enterprise_users_20260428_060000.json
  Current snapshot  : enterprise_users_20260505_060000.json
  Users (previous)  : 41
  Users (current)   : 42
  Added             : 1
  Removed           : 0
  Changed           : 1
============================================================

[ADDED USERS]
  + f9e8d7c6-...  {'role': 'viewer', 'status': 'active'}

[CHANGED USERS]
  ~ a1b2c3d4-...
      role: 'viewer' -> 'admin'
```

## PII Fields Stripped

The following fields are removed from every user record before any data is written or printed:

`email`, `name`, `firstName`, `lastName`, `fullName`, `displayName`, `givenName`, `familyName`, `picture`, `avatar`, `avatarUrl`, `profileImage`, `imageUrl`, `photoUrl`, `locale`, `timeZone`, `timezone`, `phone`, `phoneNumber`

All other fields returned by the API (role, plan, status, timestamps, etc.) are retained to support change tracking.

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — no changes detected |
| `1` | Changes detected (added, removed, or modified users) |
| `1` | Error (bad API key, network failure, etc.) |

## License

© 2025 Feedly, Inc. All rights reserved. See the script header for full disclaimers and terms of use.
