"""Pull recent logs from a failing pod.

A crashed container's logs live in its *previous* instance after a restart, so
we try ``previous=True`` first and fall back to the current instance.
"""

from __future__ import annotations

from kubernetes.client.exceptions import ApiException

from .k8s import core_v1


def fetch_logs(
    pod_name: str,
    namespace: str,
    container: str | None = None,
    tail_lines: int = 50,
) -> str:
    """Return up to ``tail_lines`` of logs, preferring the previous (crashed)
    container instance. Never raises — returns a short note on failure."""
    api = core_v1()

    # NOTE: with the default _preload_content=True, the client mis-deserializes
    # the log endpoint and returns the *repr* of the raw bytes (a literal
    # "b'...'" string). We disable preloading and decode the raw response.
    for previous in (True, False):
        try:
            resp = api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines,
                previous=previous,
                _preload_content=False,
            )
        except ApiException:
            continue
        data = resp.data
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        text = text.strip()
        # kubelet returns this sentinel when the previous container's logs were
        # already rotated/GC'd — treat it as a miss and try the other instance.
        if text and not text.startswith("unable to retrieve container logs"):
            return text

    return "(no logs available)"
