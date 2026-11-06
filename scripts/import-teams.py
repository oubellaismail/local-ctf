#!/usr/bin/env python3
"""
import-teams.py — Bulk create CTFd teams and players from a CSV file.

Idempotent: existing teams and users are skipped, not duplicated.

Each CSV row represents one player. Rows are grouped by team_name:
  - The team is created once (on first row for that team)
  - Each player gets their own user account
  - The first player listed for each team becomes captain

CSV columns required: team_name, player_name, email, password
Optional column:      affiliation  (sets the team's affiliation/school field)

Credentials are read from environment variables (auto-set by setup-ctfd.sh)
or passed as CLI arguments. Environment takes priority over CLI args.

Usage (auto — called by setup-ctfd.sh):
  CTFD_URL=... CTFD_ADMIN_TOKEN=... python3 scripts/import-teams.py data/teams.csv

Usage (manual):
  python3 scripts/import-teams.py data/teams.csv \\
    --ctfd-url https://192.168.1.50 \\
    --admin-token ctfd_xxxxx

Usage (dry run — validate CSV only, no API calls):
  python3 scripts/import-teams.py data/teams.csv --dry-run
"""

import argparse
import csv
import os
import sys
from collections import OrderedDict

import requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def validate_row(row, idx):
    errors = []
    if not row.get("team_name", "").strip():
        errors.append("team_name is empty")
    if not row.get("player_name", "").strip():
        errors.append("player_name is empty")
    email = row.get("email", "").strip()
    if not email:
        errors.append("email is empty")
    elif "@" not in email or "." not in email.split("@")[-1]:
        errors.append("email format invalid")
    pwd = row.get("password", "").strip()
    if not pwd:
        errors.append("password is empty")
    elif len(pwd) < 8:
        errors.append(f"password too short ({len(pwd)} chars, need ≥8)")
    return errors


def api(session, method, url, **kwargs):
    """Thin wrapper: always includes Content-Type (CTFd requires it even on GETs)."""
    resp = session.request(method, url, timeout=15, **kwargs)
    return resp


def group_by_team(rows):
    """Group CSV rows by team_name, preserving order."""
    teams = OrderedDict()
    for row in rows:
        team_name = row["team_name"].strip()
        teams.setdefault(team_name, []).append(row)
    return teams


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bulk import CTFd teams and players from CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("csv_file", help="CSV file (columns: team_name, player_name, email, password)")
    parser.add_argument("--ctfd-url",    default=os.environ.get("CTFD_URL", ""),
                        help="CTFd base URL (default: $CTFD_URL)")
    parser.add_argument("--admin-token", default=os.environ.get("CTFD_ADMIN_TOKEN", ""),
                        help="CTFd admin API token (default: $CTFD_ADMIN_TOKEN)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate CSV without making any API calls")
    args = parser.parse_args()

    if not args.dry_run:
        if not args.ctfd_url:
            print("ERROR: CTFd URL not set. Pass --ctfd-url or set CTFD_URL.", file=sys.stderr)
            return 2
        if not args.admin_token:
            print("ERROR: Admin token not set. Pass --admin-token or set CTFD_ADMIN_TOKEN.", file=sys.stderr)
            return 2

    # -----------------------------------------------------------------------
    # Read + validate CSV
    # -----------------------------------------------------------------------
    try:
        with open(args.csv_file) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print(f"ERROR: CSV is empty: {args.csv_file}", file=sys.stderr)
                return 2
            missing = {"team_name", "player_name", "email", "password"} - set(reader.fieldnames)
            has_affiliation = "affiliation" in (reader.fieldnames or [])
            if missing:
                print(f"ERROR: CSV missing columns: {missing}", file=sys.stderr)
                return 2
            rows = list(reader)
    except FileNotFoundError:
        print(f"ERROR: File not found: {args.csv_file}", file=sys.stderr)
        return 2

    if not rows:
        print("ERROR: CSV has no data rows.", file=sys.stderr)
        return 2

    teams = group_by_team(rows)
    print(f"Processing {args.csv_file} ({len(rows)} players across {len(teams)} teams)...\n")

    # Dry run: just validate rows
    if args.dry_run:
        invalid = 0
        for idx, row in enumerate(rows, 1):
            errors = validate_row(row, idx)
            player = row.get("player_name", "(unnamed)").strip()
            team = row.get("team_name", "(unnamed)").strip()
            if errors:
                print(f"[{idx}/{len(rows)}] {team}/{player} — INVALID: {', '.join(errors)}")
                invalid += 1
            else:
                print(f"[{idx}/{len(rows)}] {team}/{player} — OK")
        print(f"\n[DRY RUN] {len(rows) - invalid} valid, {invalid} invalid.")
        print(f"          {len(teams)} teams, {len(rows)} players.")
        return 0 if invalid == 0 else 1

    # -----------------------------------------------------------------------
    # Set up session with auth headers
    # -----------------------------------------------------------------------
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Token {args.admin_token}",
        "Content-Type": "application/json",
    })
    base = args.ctfd_url.rstrip("/")

    # -----------------------------------------------------------------------
    # Preflight: verify connectivity and token
    # -----------------------------------------------------------------------
    try:
        probe = api(session, "GET", f"{base}/api/v1/users/me")
        if probe.status_code == 401:
            print("ERROR: Admin token is invalid (401).", file=sys.stderr)
            return 3
        if probe.status_code != 200:
            print(f"ERROR: CTFd API returned HTTP {probe.status_code}.", file=sys.stderr)
            return 3
    except requests.RequestException as e:
        print(f"ERROR: CTFd unreachable: {e}", file=sys.stderr)
        return 3

    # -----------------------------------------------------------------------
    # Fetch existing teams and users ONCE (not per-row)
    # -----------------------------------------------------------------------
    try:
        existing_teams = set()
        page = 1
        while True:
            resp = api(session, "GET", f"{base}/api/v1/teams?page={page}").json()
            batch = resp.get("data", [])
            if not batch:
                break
            existing_teams.update(t["name"] for t in batch)
            if not resp.get("meta", {}).get("pagination", {}).get("next"):
                break
            page += 1

        existing_users = set()
        page = 1
        while True:
            resp = api(session, "GET", f"{base}/api/v1/users?page={page}").json()
            batch = resp.get("data", [])
            if not batch:
                break
            existing_users.update(u["name"] for u in batch)
            if not resp.get("meta", {}).get("pagination", {}).get("next"):
                break
            page += 1

    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch existing data: {e}", file=sys.stderr)
        return 3

    print(f"Found {len(existing_teams)} existing teams, {len(existing_users)} existing users.\n")

    # -----------------------------------------------------------------------
    # Import teams and players
    # -----------------------------------------------------------------------
    teams_created = teams_skipped = players_created = players_skipped = players_failed = 0

    for team_name, members in teams.items():
        first_member = members[0]
        team_label = f"[Team] {team_name}"

        # ----- Create or look up team -----
        if team_name in existing_teams:
            print(f"{team_label} — skipped (already exists)")
            teams_skipped += 1
            # Look up team ID for adding new members
            try:
                r = api(session, "GET", f"{base}/api/v1/teams?q={requests.utils.quote(team_name)}")
                matches = [t for t in r.json().get("data", []) if t["name"] == team_name]
                if not matches:
                    print(f"{team_label} — FAILED (exists but ID not found)")
                    players_failed += len(members)
                    continue
                team_id = matches[0]["id"]
            except requests.RequestException as e:
                print(f"{team_label} — FAILED (team lookup: {e})")
                players_failed += len(members)
                continue
        else:
            # Create team using first member's email as team email
            try:
                team_payload = {
                    "name":     team_name,
                    "email":    first_member["email"].strip(),
                    "password": first_member["password"].strip(),
                }
                affiliation = first_member.get("affiliation", "").strip() if has_affiliation else ""
                if affiliation:
                    team_payload["affiliation"] = affiliation
                r = api(session, "POST", f"{base}/api/v1/teams", json=team_payload)
                if r.status_code not in (200, 201):
                    errs = r.json().get("errors", r.text)
                    print(f"{team_label} — FAILED (team creation: {errs})")
                    players_failed += len(members)
                    continue
                team_id = r.json()["data"]["id"]
                existing_teams.add(team_name)
                teams_created += 1
                print(f"{team_label} — created (id={team_id})")
            except requests.RequestException as e:
                print(f"{team_label} — FAILED (team creation: {e})")
                players_failed += len(members)
                continue

        # ----- Create players and add to team -----
        captain_set = False
        for pidx, member in enumerate(members):
            player_name = member["player_name"].strip()
            email = member["email"].strip()
            pwd = member["password"].strip()
            player_label = f"  [{pidx+1}/{len(members)}] {player_name}"

            errors = validate_row(member, pidx)
            if errors:
                print(f"{player_label} — INVALID: {', '.join(errors)}")
                players_failed += 1
                continue

            if player_name in existing_users:
                print(f"{player_label} — skipped (user already exists)")
                players_skipped += 1
                continue

            # Create user
            try:
                r = api(session, "POST", f"{base}/api/v1/users", json={
                    "name": player_name, "email": email,
                    "password": pwd, "type": "user",
                })
                if r.status_code not in (200, 201):
                    try:
                        errs = r.json().get("errors", r.text)
                    except Exception:
                        errs = r.text or f"HTTP {r.status_code} (empty response)"
                    print(f"{player_label} — FAILED (user creation: {errs})")
                    players_failed += 1
                    continue
                user_id = r.json()["data"]["id"]
                existing_users.add(player_name)
            except Exception as e:
                print(f"{player_label} — FAILED (user creation: {e})")
                players_failed += 1
                continue

            # Add user to team
            try:
                api(session, "POST", f"{base}/api/v1/teams/{team_id}/members",
                    json={"user_id": user_id})
            except requests.RequestException as e:
                print(f"{player_label} — FAILED (team membership: {e})")
                players_failed += 1
                continue

            # Set first player as captain
            if not captain_set:
                try:
                    api(session, "PATCH", f"{base}/api/v1/teams/{team_id}",
                        json={"captain_id": user_id})
                    captain_set = True
                except requests.RequestException:
                    pass  # Non-fatal: captain is cosmetic

            print(f"{player_label} — created")
            players_created += 1

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print(f"\nSummary:")
    print(f"  Teams:   {teams_created} created, {teams_skipped} skipped")
    print(f"  Players: {players_created} created, {players_skipped} skipped, {players_failed} failed")
    if players_failed > 0:
        print(f"  WARNING: {players_failed} player(s) failed — re-run import to retry.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
