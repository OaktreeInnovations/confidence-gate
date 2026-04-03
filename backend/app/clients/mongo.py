from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
import structlog

logger = structlog.get_logger(__name__)


class MongoClient:
    def __init__(
        self,
        dsn: str,
        database: str,
        max_pool_size: int = 50,
        min_pool_size: int = 10,
        server_selection_timeout_ms: int = 5000,
        connect_timeout_ms: int = 5000,
        socket_timeout_ms: int = 30000,
    ):
        self._dsn = dsn
        self._database_name = database
        self._max_pool_size = max_pool_size
        self._min_pool_size = min_pool_size
        self._server_selection_timeout_ms = server_selection_timeout_ms
        self._connect_timeout_ms = connect_timeout_ms
        self._socket_timeout_ms = socket_timeout_ms
        self._client: AsyncIOMotorClient | None = None

    async def connect(self) -> None:
        logger.info(
            "mongo.connecting",
            host=self._dsn.split("@")[-1],
            max_pool=self._max_pool_size,
            min_pool=self._min_pool_size,
        )
        self._client = AsyncIOMotorClient(
            self._dsn,
            maxPoolSize=self._max_pool_size,
            minPoolSize=self._min_pool_size,
            serverSelectionTimeoutMS=self._server_selection_timeout_ms,
            connectTimeoutMS=self._connect_timeout_ms,
            socketTimeoutMS=self._socket_timeout_ms,
        )
        await self._client.admin.command("ping")
        logger.info("mongo.connected")

    async def close(self) -> None:
        if self._client:
            self._client.close()
            logger.info("mongo.disconnected")

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._client is None:
            raise RuntimeError("MongoClient is not connected. Call connect() first.")
        return self._client[self._database_name]

    async def ensure_indexes(self) -> None:
        """Create indexes for frequently queried fields."""
        db = self.db

        # test_cases indexes
        await db.test_cases.create_index([("org_id", 1), ("status", 1), ("created_at", -1)])
        await db.test_cases.create_index([("org_id", 1), ("project_id", 1)])

        # test_runs indexes
        await db.test_runs.create_index([("org_id", 1), ("batch_id", 1)])
        await db.test_runs.create_index([("org_id", 1), ("test_case_id", 1)])
        await db.test_runs.create_index([("org_id", 1), ("status", 1), ("created_at", -1)])

        # projects indexes
        await db.projects.create_index([("org_id", 1)])

        # users indexes
        await db.users.create_index("firebase_uid", unique=True)

        # execution_telemetry indexes
        await db.execution_telemetry.create_index("test_run_id", unique=True)
        await db.execution_telemetry.create_index([("org_id", 1), ("created_at", -1)])
        # TTL index: auto-expire telemetry after 90 days
        await db.execution_telemetry.create_index(
            "created_at", expireAfterSeconds=90 * 24 * 3600, name="ttl_90d"
        )

        # execution_profiles indexes
        await db.execution_profiles.create_index(
            [("test_case_id", 1), ("org_id", 1)], unique=True
        )
        await db.execution_profiles.create_index([("org_id", 1), ("last_updated", -1)])

        # selector_memory indexes
        await db.selector_memory.create_index(
            [("test_case_id", 1), ("step_number", 1), ("org_id", 1)], unique=True
        )

        # evidence_metadata indexes (retention lifecycle)
        await db.evidence_metadata.create_index(
            [("org_id", 1), ("test_run_id", 1)]
        )
        await db.evidence_metadata.create_index([("expires_at", 1)])
        await db.evidence_metadata.create_index(
            [("org_id", 1), ("created_at", -1)]
        )

        # step_code_cache indexes (code reuse guard)
        await db.step_code_cache.create_index(
            [("test_case_id", 1), ("step_number", 1), ("org_id", 1)], unique=True
        )

        # failure_graph indexes (one per org)
        await db.failure_graph.create_index("org_id", unique=True)

        # release_validations indexes
        await db.release_validations.create_index([("org_id", 1), ("project_id", 1)])
        await db.release_validations.create_index([("org_id", 1), ("status", 1), ("created_at", -1)])

        # 1.6 selector_blacklist indexes — TTL auto-expire after 7 days
        await db.selector_blacklist.create_index(
            [("test_case_id", 1), ("step_number", 1), ("target_key", 1), ("org_id", 1)],
            unique=True,
            name="blacklist_unique",
        )
        await db.selector_blacklist.create_index(
            "expires_at", expireAfterSeconds=0, name="blacklist_ttl"
        )

        # 3.2 visual_baselines indexes
        await db.visual_baselines.create_index(
            [("test_case_id", 1), ("step_number", 1), ("org_id", 1)],
            unique=True,
            name="visual_baseline_unique",
        )

        # 3.4 benchmark_stats indexes
        await db.benchmark_stats.create_index([("org_id", 1), ("computed_at", -1)])

        # 3.1 test_run_steps — full step results externalized from test_runs documents
        await db.test_run_steps.create_index(
            [("test_run_id", 1), ("org_id", 1)],
            name="test_run_steps_lookup",
        )
        await db.test_run_steps.create_index(
            "test_run_id", unique=True,
            name="test_run_steps_unique",
        )

        # org_learned_thresholds — per-org calibrated decision thresholds (2.2)
        await db.org_learned_thresholds.create_index("org_id", unique=True)

        # 5.3 test_run_steps TTL — auto-expire after 90 days (matches evidence retention)
        await db.test_run_steps.create_index(
            "created_at", expireAfterSeconds=90 * 24 * 3600, name="test_run_steps_ttl_90d"
        )

        # 5.2 project_alerts — regression signals per project
        await db.project_alerts.create_index(
            [("project_id", 1), ("alert_type", 1)], unique=True, name="project_alerts_unique"
        )
        await db.project_alerts.create_index([("org_id", 1), ("updated_at", -1)])

        # 9A.8 score_mutations — immutable audit log of confidence_score changes
        await db.score_mutations.create_index([("validation_id", 1), ("timestamp", 1)])

        # 9B.1 deployment_events — ingested deployment/incident signals for outcome inference
        await db.deployment_events.create_index([("project_id", 1), ("org_id", 1), ("processed", 1)])
        await db.deployment_events.create_index([("project_id", 1), ("event_at", -1)])

        logger.info("mongo.indexes_ensured")

    async def ping(self) -> bool:
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:
            return False
