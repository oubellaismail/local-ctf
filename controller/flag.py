"""Flag derivation module.

Formula: {FLAG_PREFIX}{ HMAC-SHA256(CTF_MASTER_SECRET, "{team_id}:{challenge_id}")[:32] }
FLAG_PREFIX defaults to "infodays" and must match the CTFd plugin's FLAG_PREFIX.
"""

import hmac
import hashlib
import os
import sys
import argparse


def derive_flag(team_id: str, challenge_id: str) -> str:
    """Derive a team's flag for a challenge.

    Args:
        team_id: Team identifier
        challenge_id: Challenge identifier

    Returns:
        Flag string in format: {FLAG_PREFIX}{<hex>}

    Raises:
        KeyError: If CTF_MASTER_SECRET environment variable is not set
    """
    master_secret = os.environ["CTF_MASTER_SECRET"]  # Will raise KeyError if missing

    message = f"{team_id}:{challenge_id}".encode()
    digest = hmac.new(
        master_secret.encode(),
        message,
        hashlib.sha256
    ).hexdigest()[:32]

    prefix = os.environ.get("FLAG_PREFIX", "infodays")
    return f"{prefix}{{{digest}}}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Derive a flag for a team/challenge")
    parser.add_argument("--team", required=True, help="Team ID")
    parser.add_argument("--challenge", required=True, help="Challenge ID")

    args = parser.parse_args()

    try:
        flag = derive_flag(args.team, args.challenge)
        print(flag)
    except KeyError:
        print("ERROR: CTF_MASTER_SECRET environment variable not set", file=sys.stderr)
        sys.exit(1)
