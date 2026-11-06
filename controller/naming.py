"""Kubernetes resource naming conventions for InfoDays CTF."""


def deployment_name(team_id: str, challenge_id: str) -> str:
    """Get Deployment name."""
    return f"team-{team_id}-{challenge_id}"


def secret_name(team_id: str, challenge_id: str) -> str:
    """Get Secret name for flag."""
    return f"team-{team_id}-{challenge_id}-secret"


def service_name(team_id: str, challenge_id: str) -> str:
    """Get Service name."""
    return f"team-{team_id}-{challenge_id}-svc"


def ingress_name(team_id: str, challenge_id: str) -> str:
    """Get Ingress name (for HTTP challenges)."""
    return f"team-{team_id}-{challenge_id}-ingress"


def managed_labels(team_id: str, challenge_id: str) -> dict:
    """Get standard labels for managed resources."""
    return {
        "team": team_id,
        "challenge": challenge_id,
        "managed-by": "ctf-controller"
    }
