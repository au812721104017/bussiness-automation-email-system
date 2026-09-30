import os
from typing import Optional
from pydantic import BaseSettings, validator

class Settings(BaseSettings):
    app_name: str = "AI Email Auto-Reply Agent"
    app_version: str = "1.0.0"
    phase: str = "15_final"
    approval_required: bool = True
    gmail_send_scopes: str = "https://www.googleapis.com/auth/gmail.send"
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 60
    scheduler_max_retries: int = 3
    scheduler_retry_backoff_seconds: float = 1.0
    # Declared before scheduler_query: pydantic v1 validators only see earlier fields.
    # {window} is replaced with scheduler_newer_than_days so the recency window is configurable
    # without editing the query. Gmail-side only — inbox, never sent/spam/trash, read-state agnostic.
    scheduler_newer_than_days: int = 2
    scheduler_query: str = "in:inbox -in:sent -in:spam -in:trash newer_than:{window}d"
    admin_token: Optional[str] = None
    rate_limit_per_minute: int = 60
    cors_allow_origins: str = ""  # comma-separated; empty = no CORS (safe default)
    # Production flag — when true, disables weak defaults
    production_hardening: bool = False
    llm_provider: str = "mock"
    llm_model: str = "mock-reply-v1"
    rag_top_k: int = 3
    rag_chunk_size: int = 500
    rag_chunk_overlap: int = 50
    rag_knowledge_dir: str = "./knowledge"
    rag_embedding_provider: str = "mock"
    env: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = "sqlite:///./email_automation.db"
    openai_api_key: Optional[str] = None
    gmail_client_id: Optional[str] = None
    gmail_client_secret: Optional[str] = None
    gmail_redirect_uri: str = "http://localhost:8000/gmail/auth/callback"
    gmail_credentials_path: str = "./credentials.json"
    gmail_token_path: str = "./token.json"
    gmail_scopes: str = "https://www.googleapis.com/auth/gmail.readonly"
    secret_key: Optional[str] = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "allow"

    @validator("scheduler_newer_than_days")
    def validate_newer_than_days(cls, v):
        if v < 1:
            raise ValueError("SCHEDULER_NEWER_THAN_DAYS must be >= 1")
        return v

    @validator("scheduler_query")
    def apply_newer_than_window(cls, v, values):
        window = values.get("scheduler_newer_than_days")
        if window is None or "{window}" not in v:
            return v
        return v.replace("{window}", str(window))

    @validator("database_url")
    def validate_database_url(cls, v):
        if not v or not isinstance(v, str):
            raise ValueError("DATABASE_URL must be a non-empty string")
        allowed_prefixes = ("sqlite://", "postgresql://", "postgres://", "mysql://")
        if not any(v.startswith(p) for p in allowed_prefixes):
            raise ValueError(f"DATABASE_URL must start with one of {allowed_prefixes}")
        return v

    @validator("port")
    def validate_port(cls, v):
        if not 1 <= v <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        return v

    @validator("log_level")
    def validate_log_level(cls, v):
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}")
        return upper

    @validator("env")
    def validate_env(cls, v):
        allowed = {"development", "testing", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"ENV must be one of {allowed}")
        return v


def get_settings() -> Settings:
    return Settings()
