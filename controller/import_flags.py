#!/usr/bin/env python3
"""
import_flags.py - Import derived flags for all team × challenge pairs into CTFd.

Idempotent: safe to re-run. Skips flags that already exist.
"""

import os
import sys
import argparse
from typing import Optional
import logging
from typing import Any

import httpx

from flag import derive_flag
from manifest_loader import load_manifests, load_flag_entries
from manifests import expand_flag_entries

logger = logging.getLogger(__name__)


def fetch_teams(ctfd_url: str, admin_token: str) -> list[dict]:
    """
    Fetch all teams from CTFd admin API.

    Args:
        ctfd_url: Base URL of CTFd instance
        admin_token: CTFd admin API token

    Returns:
        List of {id: str, name: str} dicts
    """
    url = f"{ctfd_url}/api/v1/teams?per_page=100"
    headers = {"Authorization": f"Token {admin_token}", "Content-Type": "application/json"}

    try:
        response = httpx.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        teams = data.get("data", [])
        logger.info(f"Fetched {len(teams)} teams from CTFd")
        return [{"id": str(t["id"]), "name": t.get("name", "")} for t in teams]
    except Exception as e:
        logger.error(f"Failed to fetch teams: {e}")
        raise


def fetch_challenges(ctfd_url: str, admin_token: str) -> dict:
    """
    Fetch all challenges and build a mapping of name → ctfd_id.

    Args:
        ctfd_url: Base URL of CTFd instance
        admin_token: CTFd admin API token

    Returns:
        Dict mapping challenge name → ctfd_id
    """
    url = f"{ctfd_url}/api/v1/challenges?view=admin&per_page=100"
    headers = {"Authorization": f"Token {admin_token}", "Content-Type": "application/json"}

    try:
        response = httpx.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        challenges = data.get("data", [])

        # Build name → id mapping
        mapping = {}
        for ch in challenges:
            mapping[ch.get("name", "")] = ch["id"]

        logger.info(f"Fetched {len(challenges)} challenges from CTFd")
        return mapping
    except Exception as e:
        logger.error(f"Failed to fetch challenges: {e}")
        raise


def fetch_existing_flags(ctfd_url: str, admin_token: str, challenge_id: int) -> set[str]:
    """
    Fetch all flags for a specific challenge.

    Args:
        ctfd_url: Base URL of CTFd instance
        admin_token: CTFd admin API token
        challenge_id: CTFd challenge ID

    Returns:
        Set of flag strings already registered
    """
    url = f"{ctfd_url}/api/v1/flags?per_page=100&challenge_id={challenge_id}"
    headers = {"Authorization": f"Token {admin_token}", "Content-Type": "application/json"}

    try:
        response = httpx.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        flags = data.get("data", [])
        flag_set = {f.get("content", "") for f in flags}
        return flag_set
    except Exception as e:
        logger.error(f"Failed to fetch flags for challenge {challenge_id}: {e}")
        raise


def import_flag(ctfd_url: str, admin_token: str, challenge: dict, challenge_id_map: dict) -> str:
    """
    Import one team-bound flag entry per single-flag challenge into CTFd.

    Uses the custom 'team' flag type registered by the controller-plugin.
    CTFd validates on the fly: HMAC(CTF_MASTER_SECRET, "{team_id}:{manifest_id}")[:32]
    Cross-team flag submission is cryptographically rejected — one row covers all teams.

    Flag row:
        type    = "team"
        content = "team-derived"   (placeholder, not used for validation)
        data    = challenge manifest ID  e.g. "pwn-bof"

    Returns:
        "created" or "skipped"
    """
    challenge_name = challenge.get("name", "")
    challenge_manifest_id = challenge["id"]
    ctfd_challenge_id = challenge_id_map.get(challenge_name)

    if not ctfd_challenge_id:
        logger.warning(f"Challenge not found in CTFd: {challenge_name}")
        return "skipped"

    # Skip if a team flag already exists for this challenge
    existing_flags = fetch_existing_flags(ctfd_url, admin_token, ctfd_challenge_id)
    if "team-derived" in existing_flags:
        logger.info(f"Skipped (team flag exists): {challenge_name}")
        return "skipped"

    url = f"{ctfd_url}/api/v1/flags"
    headers = {"Authorization": f"Token {admin_token}", "Content-Type": "application/json"}
    body = {
        "challenge_id": ctfd_challenge_id,
        "type": "team",
        "content": "team-derived",
        "data": challenge_manifest_id,
    }

    try:
        response = httpx.post(url, headers=headers, json=body)
        response.raise_for_status()
        logger.info(f"Created team flag: {challenge_name} (manifest: {challenge_manifest_id})")
        return "created"
    except Exception as e:
        logger.error(f"Failed to import flag for challenge {challenge_name}: {e}")
        raise


def _flag_objects(ctfd_url: str, admin_token: str, challenge_id: int) -> list[dict]:
    url = f"{ctfd_url}/api/v1/flags?per_page=100&challenge_id={challenge_id}"
    headers = {"Authorization": f"Token {admin_token}", "Content-Type": "application/json"}
    response = httpx.get(url, headers=headers, follow_redirects=True)
    response.raise_for_status()
    return response.json().get("data", [])


def sync_flag(ctfd_url: str, admin_token: str, challenge: dict, challenge_id_map: dict) -> str:
    """Make the CTFd flags of a single-flag challenge match its flag_mode.

    per-team: exactly one "team" flag (validated by the plugin per team).
    static:   exactly one case-sensitive "static" flag with the manifest's
              flag; team flags are removed (and vice versa when switching).
    Returns "created", "updated" or "skipped".
    """
    name = challenge.get("name", "")
    ctfd_id = challenge_id_map.get(name)
    if not ctfd_id:
        logger.warning(f"Challenge not found in CTFd: {name}")
        return "skipped"
    headers = {"Authorization": f"Token {admin_token}", "Content-Type": "application/json"}
    flags = _flag_objects(ctfd_url, admin_token, ctfd_id)

    if (challenge.get("flag_mode") or "per-team") == "static":
        keep = lambda f: f.get("type") == "static" and f.get("content") == challenge["flag"]
        wanted = {"challenge_id": ctfd_id, "type": "static", "content": challenge["flag"],
                  "data": "case_sensitive"}
    else:
        keep = lambda f: f.get("type") == "team" and f.get("content") == "team-derived"
        wanted = {"challenge_id": ctfd_id, "type": "team", "content": "team-derived",
                  "data": challenge["id"]}

    removed = 0
    for f in flags:
        if not keep(f):
            httpx.delete(f"{ctfd_url}/api/v1/flags/{f['id']}", headers=headers).raise_for_status()
            removed += 1
    if any(keep(f) for f in flags):
        if removed:
            logger.info(f"Updated flags: {name} (removed {removed} old)")
            return "updated"
        logger.info(f"Skipped (flag exists): {name}")
        return "skipped"
    httpx.post(f"{ctfd_url}/api/v1/flags", headers=headers, json=wanted).raise_for_status()
    kind = "static flag" if wanted["type"] == "static" else "team flag"
    logger.info(f"Created {kind}: {name}" + (f" (removed {removed} old)" if removed else ""))
    return "updated" if removed else "created"


def register_bundle_challenge(ctfd_url: str, admin_token: str, ctfd_challenge_id: int,
                               parent_id: str, components: list[dict]) -> str:
    """
    Register a multi_flag bundle challenge:
      1. Creates ONE team flag row with data=parent_id (for plugin deployment-ID lookup)
      2. Registers all component scoring params via the plugin endpoint

    Returns: "created", "registered", or "skipped"
    """
    headers = {"Authorization": f"Token {admin_token}", "Content-Type": "application/json"}

    # Step 1: ensure the team flag row exists (data = parent_id for deployment lookup)
    existing_flags = fetch_existing_flags(ctfd_url, admin_token, ctfd_challenge_id)
    if "team-derived" not in existing_flags:
        body = {
            "challenge_id": ctfd_challenge_id,
            "type": "team",
            "content": "team-derived",
            "data": parent_id,
        }
        try:
            resp = httpx.post(f"{ctfd_url}/api/v1/flags", headers=headers, json=body)
            resp.raise_for_status()
            logger.info(f"Created team flag for bundle: {parent_id} (ctfd_id={ctfd_challenge_id})")
        except Exception as e:
            logger.error(f"Failed to create team flag for bundle {parent_id}: {e}")
            raise

    # Step 2: register component scoring params
    plugin_url = f"{ctfd_url}/api/plugin/multi-flag-register"
    body = {
        "challenge_id": ctfd_challenge_id,
        "components": components,
    }
    try:
        resp = httpx.post(plugin_url, headers=headers, json=body)
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"Registered {len(components)} components for {parent_id}: {data.get('message', '')}")
            return "registered"
        else:
            logger.warning(f"Component registration returned {resp.status_code} for {parent_id}: {resp.text}")
            return "skipped"
    except Exception as e:
        logger.error(f"Failed to register components for {parent_id}: {e}")
        raise


def main(ctfd_url: str, admin_token: str) -> None:
    """Entry point for flag import workflow (manifests from the mounted dir)."""
    run(ctfd_url, admin_token)


def run(ctfd_url: str, admin_token: str, manifests: Optional[list] = None) -> dict:
    """Register team-bound flags in CTFd. Returns {"created", "skipped", "errors"}."""
    logger.info("Starting flag import (team-bound mode)...")

    # CTF_MASTER_SECRET must be set — used at validation time by CTFd, not here
    if not os.getenv("CTF_MASTER_SECRET"):
        raise ValueError("CTF_MASTER_SECRET env var not set")

    challenge_id_map = fetch_challenges(ctfd_url, admin_token)

    # Split entries into single-flag and bundle groups
    entries = load_flag_entries() if manifests is None else expand_flag_entries(manifests)
    all_entries = [c for c in entries if c.get("id") and c.get("name")]
    logger.info(f"Loaded {len(all_entries)} flag entries (including bundle expansions)")

    # Group bundle entries by parent_id
    bundle_groups: dict[str, list[dict]] = {}
    single_flag_entries: list[dict] = []
    for entry in all_entries:
        parent_id = entry.get("_parent_id")
        if parent_id:
            bundle_groups.setdefault(parent_id, []).append(entry)
        else:
            single_flag_entries.append(entry)

    created = 0
    skipped = 0
    errors = 0

    # Process single-flag challenges (unchanged behavior)
    for challenge in single_flag_entries:
        try:
            result = sync_flag(ctfd_url, admin_token, challenge, challenge_id_map)
            if result in ("created", "updated"):
                created += 1
            elif result == "skipped":
                skipped += 1
        except Exception as e:
            logger.error(f"Error importing flag for {challenge['id']}: {e}")
            errors += 1

    # Process bundle (multi_flag) challenges
    for parent_id, entries in bundle_groups.items():
        bundle_name = entries[0].get("_bundle_name", parent_id)
        ctfd_challenge_id = challenge_id_map.get(bundle_name)
        if not ctfd_challenge_id:
            logger.warning(f"Bundle challenge not found in CTFd: '{bundle_name}' (parent: {parent_id})")
            skipped += 1
            continue
        components = [
            {
                "component_id": e["id"],
                "env_var": e.get("env", ""),
                "initial": int(e.get("initial", 500)),
                "minimum": int(e.get("minimum", 50)),
                "decay": int(e.get("decay", 25)),
            }
            for e in entries
        ]
        try:
            result = register_bundle_challenge(ctfd_url, admin_token, ctfd_challenge_id,
                                               parent_id, components)
            if result in ("created", "registered"):
                created += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error(f"Error registering bundle {parent_id}: {e}")
            errors += 1

    logger.info(f"Done: {created} created/registered, {skipped} skipped, {errors} errors")
    return {"created": created, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Import derived flags into CTFd")
    parser.add_argument("--ctfd-url", required=True, help="CTFd base URL")

    args = parser.parse_args()

    # Get admin token from environment
    admin_token = os.getenv("CTFD_ADMIN_TOKEN")
    if not admin_token:
        print("Error: CTFD_ADMIN_TOKEN environment variable not set")
        sys.exit(1)

    try:
        main(args.ctfd_url, admin_token)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
