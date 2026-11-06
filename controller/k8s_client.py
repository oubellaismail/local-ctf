"""Kubernetes client initialization with in-cluster and local fallback."""

from kubernetes import client, config
from kubernetes.client.rest import ApiException


def init_k8s_client() -> tuple:
    """Initialize Kubernetes API clients.

    Returns:
        Tuple of (AppsV1Api, CoreV1Api, NetworkingV1Api)

    Raises:
        Exception: If neither in-cluster nor local config can be loaded
    """
    try:
        # Try in-cluster config first (running inside a K8s pod)
        config.load_incluster_config()
    except config.ConfigException:
        # Fallback to local kubeconfig (~/.kube/config)
        config.load_kube_config()

    return (
        client.AppsV1Api(),
        client.CoreV1Api(),
        client.NetworkingV1Api()
    )
