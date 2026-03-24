from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # App
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    backend_log_level: str = "INFO"
    backend_log_format: str = "json"
    cors_origins: list[str] = ["http://localhost:3000"]

    # MongoDB
    mongo_host: str = "mongo"
    mongo_port: int = 27017
    mongo_db: str = "qualora"
    mongo_initdb_root_username: str
    mongo_initdb_root_password: str
    mongo_max_pool_size: int = 50
    mongo_min_pool_size: int = 10
    mongo_worker_pool_size: int = 5
    mongo_server_selection_timeout_ms: int = 5000
    mongo_connect_timeout_ms: int = 5000
    mongo_socket_timeout_ms: int = 30000

    @property
    def mongo_dsn(self) -> str:
        return (
            f"mongodb://{self.mongo_initdb_root_username}:"
            f"{self.mongo_initdb_root_password}@"
            f"{self.mongo_host}:{self.mongo_port}"
        )

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # MinIO / S3
    minio_endpoint: str = "minio:9000"
    minio_root_user: str
    minio_root_password: str
    minio_default_bucket: str = "qualora-artifacts"
    minio_use_ssl: bool = False

    # Firebase
    firebase_project_id: str = "qualora-bd693"

    # OpenAI (AI)
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"

    # Execution rate limiting
    execution_rate_limit_enabled: bool = True
    execution_max_concurrent_per_org: int = 3
    execution_max_queue_depth_per_org: int = 20
    execution_global_max_concurrent: int = 10
    execution_lock_ttl_seconds: int = 900  # 15 min auto-expire for crash safety

    # Evidence lifecycle
    evidence_lifecycle_enabled: bool = True
    evidence_retention_days: int = 90
    evidence_compression_enabled: bool = True

    # Adaptive execution strategy
    execution_strategy_enabled: bool = True

    # Cross-test failure intelligence graph
    failure_graph_enabled: bool = True
    failure_graph_window_hours: int = 72

    # Vision verification (opt-in; reduces AI calls per step by 50%+)
    vision_verification_enabled: bool = False

    # Execution mode: FAST | STANDARD | DEEP
    execution_mode: str = "STANDARD"

    # Release validation
    release_validation_enabled: bool = True
