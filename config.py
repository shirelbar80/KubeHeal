"""Central configuration for KubeHeal, loaded from environment / .env file.

All tunables live here so no secrets or magic values are scattered through the
code. Import the singleton ``settings`` elsewhere:

    from config import settings
    print(settings.namespace)
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # --- Kubernetes ---
    namespace: str = "kubeheal-demo"
    kubeconfig_path: str = ""

    # --- Local LLM (Ollama) ---
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "granite3.1-dense:2b"

    # --- Slack ---
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_channel: str = ""

    # --- Safety / behavior ---
    # HITL is mandatory; auto-approve is intentionally unsupported and ignored.
    auto_approve: bool = False
    cooldown_seconds: int = 300
    log_tail_lines: int = 50
    verify_timeout_seconds: int = 120

    # --- Optional internal API ---
    health_port: int = 8000


settings = Settings()
