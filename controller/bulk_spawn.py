#!/usr/bin/env python3
"""
bulk_spawn.py - Pre-spawn all challenge pods for all teams at replicas=0.

Idempotent: safe to re-run. Parallel execution via ThreadPoolExecutor.
"""

import os
import sys
import argparse
import logging
import base64
import json
import concurrent.futures
from typing import Optional, Any

import httpx
from kubernetes.client import ApiException
from kubernetes.client.models import (
    V1Secret,
    V1ObjectMeta,
    V1Deployment,
    V1DeploymentSpec,
    V1PodTemplateSpec,
    V1PodSpec,
    V1Container,
    V1ContainerPort,
    V1EnvVar,
    V1EnvVarSource,
    V1SecretKeySelector,
    V1ResourceRequirements,
    V1LabelSelector,
    V1Service,
    V1ServiceSpec,
    V1ServicePort,
    V1SecurityContext,
    V1PodSecurityContext,
    V1Capabilities,
    V1SeccompProfile,
    V1Probe,
    V1TCPSocketAction,
    V1Affinity,
    V1NodeAffinity,
    V1PreferredSchedulingTerm,
    V1NodeSelectorTerm,
    V1NodeSelectorRequirement,
)

from k8s_client import init_k8s_client
from flag import derive_flag
from manifest_loader import load_manifests
from naming import deployment_name, secret_name, service_name, managed_labels


def _flag_env_vars(team_id: str, challenge: dict) -> tuple[dict, list]:
    """Build (secret_data, env_var_list) for a challenge's flags.

    Single-flag manifest: {FLAG: derive_flag(team_id, id)}, [EnvVar FLAG]
    Bundle manifest:      {FLAG1: ..., FLAG2: ...},          [EnvVar FLAG1, FLAG2, ...]
    """
    if "flags" in challenge:
        secret_data = {
            entry["env"]: derive_flag(team_id, entry["id"])
            for entry in challenge["flags"]
        }
        env_vars = [
            V1EnvVar(
                name=entry["env"],
                value_from=V1EnvVarSource(
                    secret_key_ref=V1SecretKeySelector(
                        name=secret_name(team_id, challenge["id"]),
                        key=entry["env"],
                    )
                ),
            )
            for entry in challenge["flags"]
        ]
    else:
        secret_data = {"FLAG": derive_flag(team_id, challenge["id"])}
        env_vars = [
            V1EnvVar(
                name="FLAG",
                value_from=V1EnvVarSource(
                    secret_key_ref=V1SecretKeySelector(
                        name=secret_name(team_id, challenge["id"]),
                        key="FLAG",
                    )
                ),
            )
        ]
    return secret_data, env_vars

logger = logging.getLogger(__name__)


def fetch_teams(ctfd_url: str, admin_token: str) -> list[dict]:
    """
    Fetch all teams from CTFd admin API.

    Args:
        ctfd_url: Base URL of CTFd instance (e.g., "http://ctfd-service.ctfd.svc.cluster.local")
        admin_token: CTFd admin API token

    Returns:
        List of active {id: str, name: str} dicts, excluding hidden/banned teams.
    """
    url = f"{ctfd_url}/api/v1/teams?per_page=100"
    # Content-Type required on all CTFd token-auth requests (including GETs);
    # without it CTFd's token middleware skips auth and returns 302/500.
    headers = {"Authorization": f"Token {admin_token}", "Content-Type": "application/json"}

    try:
        response = httpx.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        teams = data.get("data", [])
        active_teams = [
            {"id": str(t["id"]), "name": t.get("name", "")}
            for t in teams
            if not bool(t.get("banned")) and not bool(t.get("hidden"))
        ]
        skipped = len(teams) - len(active_teams)
        logger.info(
            "Fetched %s teams from CTFd (%s active, %s skipped hidden/banned)",
            len(teams),
            len(active_teams),
            skipped,
        )
        return active_teams
    except Exception as e:
        logger.error(f"Failed to fetch teams: {e}")
        raise


def create_secret(core_v1: Any, team_id: str, challenge: dict) -> str:
    """
    Create or skip a Kubernetes Secret containing the challenge flag.

    Args:
        core_v1: CoreV1Api instance
        team_id: Team identifier
        challenge: Challenge manifest dict with 'id' field

    Returns:
        "created" or "skipped"
    """
    challenge_id = challenge["id"]
    sec_name = secret_name(team_id, challenge_id)

    secret_data, _ = _flag_env_vars(team_id, challenge)
    body = V1Secret(
        metadata=V1ObjectMeta(
            name=sec_name,
            namespace="challenges",
            labels=managed_labels(team_id, challenge_id),
        ),
        string_data=secret_data,
    )

    try:
        core_v1.create_namespaced_secret("challenges", body)
        logger.info(f"Created secret: {sec_name}")
        return "created"
    except ApiException as e:
        if e.status == 409:
            logger.debug(f"Skipped (exists): {sec_name}")
            return "skipped"
        raise


def create_deployment(apps_v1: Any, team_id: str, challenge: dict) -> str:
    """
    Create or skip a Kubernetes Deployment for a challenge at replicas=0.

    Args:
        apps_v1: AppsV1Api instance
        team_id: Team identifier
        challenge: Challenge manifest dict

    Returns:
        "created" or "skipped"
    """
    challenge_id = challenge["id"]
    dep_name = deployment_name(team_id, challenge_id)
    labels = managed_labels(team_id, challenge_id)

    _, env_vars = _flag_env_vars(team_id, challenge)
    # container_port overrides port for the pod's actual listening port when
    # the external service port differs (e.g. sshd on 22 exposed as port 2222).
    container_port = challenge.get("container_port", challenge["port"])
    all_ports = [V1ContainerPort(container_port=container_port)]
    for extra in challenge.get("extra_ports", []):
        all_ports.append(V1ContainerPort(container_port=extra["port"]))

    container = V1Container(
        name=challenge_id,
        image=challenge["image"],
        image_pull_policy=os.getenv("CHALLENGE_PULL_POLICY", "Always"),
        ports=all_ports,
        env=env_vars,
        readiness_probe=V1Probe(
            tcp_socket=V1TCPSocketAction(port=container_port),
            initial_delay_seconds=5,
            period_seconds=3,
            failure_threshold=40,  # allows up to ~120s for slow-init challenges
            success_threshold=1,
        ),
        resources=V1ResourceRequirements(
            limits={"cpu": "0.5", "memory": challenge.get("memory_limit", "128Mi")},
            requests={"cpu": "0.05", "memory": "32Mi"},
        ),
        security_context=V1SecurityContext(
            allow_privilege_escalation=bool(challenge.get("allow_privilege_escalation", False)),
            # run_as_root challenges need DAC_OVERRIDE + CHOWN to write system dirs as root;
            # dropping ALL strips those even from uid 0.
            # allow_privilege_escalation challenges also need SETUID/SETGID so suid binaries keep privs.
            capabilities=V1Capabilities(
                drop=["ALL"] if not challenge.get("run_as_root") else [],
                add=["SETUID", "SETGID"] if challenge.get("allow_privilege_escalation") else [],
            ),
            **({} if challenge.get("run_as_root") else {
                "run_as_non_root": True,
                "run_as_user": 1000,
            }),
        ),
    )

    pod_security_context = V1PodSecurityContext(
        seccomp_profile=V1SeccompProfile(type="RuntimeDefault"),
        **({} if challenge.get("run_as_root") else {
            "run_as_non_root": True,
            "run_as_user": 1000,
        }),
    )

    body = V1Deployment(
        metadata=V1ObjectMeta(
            name=dep_name,
            namespace="challenges",
            labels=labels,
            annotations={"ctf/activated-at": ""},
        ),
        spec=V1DeploymentSpec(
            replicas=0,
            selector=V1LabelSelector(match_labels=labels),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=labels),
                spec=V1PodSpec(
                    containers=[container],
                    security_context=pod_security_context,
                    # Prefer the challenge laptop(s) so player pods don't starve
                    # CTFd/MySQL on the core laptop. Falls back to any node.
                    affinity=V1Affinity(
                        node_affinity=V1NodeAffinity(
                            preferred_during_scheduling_ignored_during_execution=[
                                V1PreferredSchedulingTerm(
                                    weight=100,
                                    preference=V1NodeSelectorTerm(
                                        match_expressions=[
                                            V1NodeSelectorRequirement(
                                                key="ctf/role",
                                                operator="In",
                                                values=["challenges"],
                                            )
                                        ]
                                    ),
                                )
                            ]
                        )
                    ),
                    automount_service_account_token=False,
                ),
            ),
        ),
    )

    try:
        apps_v1.create_namespaced_deployment("challenges", body)
        logger.info(f"Created deployment: {dep_name}")
        return "created"
    except ApiException as e:
        if e.status == 409:
            logger.debug(f"Skipped (exists): {dep_name}")
            return "skipped"
        raise


def _service_ports(challenge: dict) -> list:
    """Main port first (the API builds the player URL from ports[0]), then
    extra_ports. Multi-port Services require every port to be named."""
    main = V1ServicePort(
        name="main",
        port=challenge["port"],
        target_port=challenge.get("container_port", challenge["port"]),
    )
    extras = [
        V1ServicePort(name=str(e.get("name", f"p{e['port']}")).lower()[:15], port=e["port"], target_port=e["port"])
        for e in challenge.get("extra_ports", [])
    ]
    return [main, *extras]


def create_service(core_v1: Any, team_id: str, challenge: dict) -> str:
    """
    Create or skip a Kubernetes Service for a challenge.

    Args:
        core_v1: CoreV1Api instance
        team_id: Team identifier
        challenge: Challenge manifest dict

    Returns:
        "created" or "skipped"
    """
    challenge_id = challenge["id"]
    svc_name = service_name(team_id, challenge_id)
    labels = managed_labels(team_id, challenge_id)

    body = V1Service(
        metadata=V1ObjectMeta(
            name=svc_name,
            namespace="challenges",
            labels=labels,
        ),
        spec=V1ServiceSpec(
            type="NodePort" if challenge.get("type") in ("tcp", "http") else "ClusterIP",
            selector=labels,
            ports=_service_ports(challenge),
        ),
    )

    try:
        core_v1.create_namespaced_service("challenges", body)
        logger.info(f"Created service: {svc_name}")
        return "created"
    except ApiException as e:
        if e.status == 409:
            logger.debug(f"Skipped (exists): {svc_name}")
            return "skipped"
        raise


def spawn_one(clients: tuple[Any, Any, Any], team: dict, challenge: dict) -> dict:
    """
    Spawn K8s resources for one team × challenge pair (secret, deployment, service).
    Players reach the service directly on its NodePort.

    Args:
        clients: Tuple of (apps_v1, core_v1, net_v1) API instances
        team: Team dict with 'id' and 'name'
        challenge: Challenge manifest dict

    Returns:
        Summary dict: {team_id, challenge_id, status: "created"|"skipped"|"error", error: str|None}
    """
    apps_v1, core_v1, net_v1 = clients
    team_id = team["id"]
    challenge_id = challenge["id"]

    try:
        # Create secret
        create_secret(core_v1, team_id, challenge)

        # Create deployment
        dep = create_deployment(apps_v1, team_id, challenge)

        # Create service (NodePort for tcp/http)
        create_service(core_v1, team_id, challenge)

        return {
            "team_id": team_id,
            "challenge_id": challenge_id,
            # The deployment is the instance: count it, not its secret/service
            "status": "created" if dep == "created" else "skipped",
            "error": None,
        }
    except Exception as e:
        logger.error(
            f"Error spawning {team_id}/{challenge_id}: {e}",
            exc_info=False,
        )
        return {
            "team_id": team_id,
            "challenge_id": challenge_id,
            "status": "error",
            "error": str(e),
        }


def main(ctfd_url: str, admin_token: str) -> None:
    """Entry point for bulk_spawn workflow (manifests from the mounted dir)."""
    run(ctfd_url, admin_token)


def run(ctfd_url: str, admin_token: str, manifests: Optional[list] = None,
        clients: Optional[tuple] = None) -> dict:
    """Create the stopped per-team instances. Existing ones are skipped.

    manifests: challenge manifests to consider (default: the mounted
    challenges/manifests dir). Returns {"created", "skipped", "errors"}.
    """
    logger.info("Starting bulk_spawn...")

    # Validate env var
    if not os.getenv("CTF_MASTER_SECRET"):
        raise ValueError("CTF_MASTER_SECRET env var not set")

    if clients is None:
        clients = init_k8s_client()

    # Fetch teams from CTFd
    teams = fetch_teams(ctfd_url, admin_token)
    logger.info(f"Fetched {len(teams)} teams")

    # Service challenges only (type http/tcp) — static challenges have no pods
    if manifests is None:
        manifests = load_manifests()
    challenges = [c for c in manifests if c.get("type") in ("http", "tcp")]
    logger.info(f"Loaded {len(challenges)} service challenges")

    created = 0
    skipped = 0
    errors = 0

    # Parallel: secret + deployment + service per team × challenge
    logger.info("Spawning deployments/services/secrets in parallel...")
    futures = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for team in teams:
            for challenge in challenges:
                future = executor.submit(spawn_one, clients, team, challenge)
                futures[future] = (team["id"], challenge["id"])

        for future in concurrent.futures.as_completed(futures, timeout=600):
            try:
                result = future.result()
                if result["status"] == "created":
                    created += 1
                elif result["status"] == "skipped":
                    skipped += 1
                else:
                    errors += 1
            except concurrent.futures.TimeoutError:
                errors += 1
                team_id, challenge_id = futures[future]
                logger.error(f"Timeout spawning {team_id}/{challenge_id}")
            except Exception as e:
                errors += 1
                logger.error(f"Exception collecting result: {e}")

    logger.info(f"Done: {created} created, {skipped} skipped, {errors} errors")
    return {"created": created, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Pre-spawn all challenge pods for all teams")
    parser.add_argument("--ctfd-url", required=True, help="CTFd base URL")
    parser.add_argument("--token", required=True, help="CTFd admin API token")

    args = parser.parse_args()

    try:
        main(args.ctfd_url, args.token)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
