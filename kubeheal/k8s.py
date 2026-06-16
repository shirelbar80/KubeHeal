"""Kubernetes client bootstrap, shared by all components.

Loads kube config (explicit path, default kubeconfig, or in-cluster) once and
exposes ready-to-use API clients.
"""

from __future__ import annotations

from functools import lru_cache

from kubernetes import client, config

from config import settings


@lru_cache(maxsize=1)
def _load() -> None:
    """Load configuration exactly once."""
    if settings.kubeconfig_path:
        config.load_kube_config(config_file=settings.kubeconfig_path)
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def core_v1() -> client.CoreV1Api:
    _load()
    return client.CoreV1Api()


def apps_v1() -> client.AppsV1Api:
    _load()
    return client.AppsV1Api()
