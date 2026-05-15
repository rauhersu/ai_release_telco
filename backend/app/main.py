"""FastAPI application entry point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TypedDict

logger = logging.getLogger(__name__)

from fastapi import FastAPI
from fastapi_pagination import add_pagination

from app.api.exception_handlers import register_exception_handlers
from app.api.router import api_router
from app.clients.redis import RedisClient
from app.core.config import settings
from app.core.logfire_setup import instrument_app, setup_logfire
from app.core.logging import setup_logging
from app.core.middleware import RequestIDMiddleware
from app.services.rag.embeddings import EmbeddingService
from app.services.rag.vectorstore import BaseVectorStore, MilvusVectorStore


class LifespanState(TypedDict, total=False):
    """Lifespan state - resources available via request.state."""

    redis: RedisClient
    embedding_service: EmbeddingService
    vector_store: BaseVectorStore


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[LifespanState, None]:
    """Application lifespan - startup and shutdown events.

    Resources yielded here are available via request.state in route handlers.
    See: https://asgi.readthedocs.io/en/latest/specs/lifespan.html#lifespan-state
    """
    # === Startup ===
    state: LifespanState = {}
    setup_logfire()
    from app.core.logfire_setup import instrument_asyncpg

    instrument_asyncpg()
    redis_client = RedisClient()
    await redis_client.connect()
    state["redis"] = redis_client
    from app.core.config import settings

    try:
        embedder = EmbeddingService(settings=settings.rag)
        embedder.warmup()
        state["embedding_service"] = embedder
    except Exception as e:
        logger.error(f"Embedding service warmup failed: {e}. RAG will not be available.")
    # Warmup Milvus and verify health
    if "embedding_service" in state:
        try:
            vector_store = MilvusVectorStore(settings=settings.rag, embedding_service=embedder)
            await vector_store.client.list_collections()
            state["vector_store"] = vector_store
        except Exception as e:
            logger.error(f"Milvus connection failed: {e}. Vector store will not be available.")

    # === Slack Adapter (Socket Mode polling for dev, Events API for prod) ===
    from app.core.channel_crypto import decrypt_token as _slack_decrypt
    from app.services.channels import register_adapter as _slack_register
    from app.services.channels.slack import SlackAdapter

    _slack_adapter = SlackAdapter()
    _slack_register(_slack_adapter)
    try:
        from app.db.session import get_db_context

        async with get_db_context() as _slack_db:
            from app.repositories.channel_bot import get_active_polling_bots

            _slack_bots = await get_active_polling_bots(_slack_db, "slack")
        for _sbot in _slack_bots:
            _stoken = _slack_decrypt(_sbot.token_encrypted)
            await _slack_adapter.start_polling(str(_sbot.id), _stoken)
        logger.info("Slack: Socket Mode started for %d bot(s)", len(_slack_bots))
    except Exception as _slack_exc:
        logger.error("Slack: failed to start Socket Mode: %s", _slack_exc)
    yield state

    # === Shutdown ===
    if "redis" in state:
        await state["redis"].close()
    from app.db.session import close_db

    await close_db()
    try:
        if "vector_store" in state:
            await state["vector_store"].client.close()  # type: ignore[attr-defined]
    except Exception:
        pass
    for _sbid in list(_slack_adapter._socket_tasks.keys()):
        await _slack_adapter.stop_polling(_sbid)


# Environments where API docs should be visible
SHOW_DOCS_ENVIRONMENTS = ("local", "staging", "development")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Only show docs in allowed environments (hide in production)
    show_docs = settings.ENVIRONMENT in SHOW_DOCS_ENVIRONMENTS
    openapi_url = f"{settings.API_V1_STR}/openapi.json" if show_docs else None
    docs_url = "/docs" if show_docs else None
    redoc_url = "/redoc" if show_docs else None

    # OpenAPI tags for better documentation organization
    openapi_tags = [
        {
            "name": "health",
            "description": "Health check endpoints for monitoring and Kubernetes probes",
        },
        {
            "name": "auth",
            "description": "Authentication endpoints - login, register, token refresh",
        },
        {
            "name": "users",
            "description": "User management endpoints",
        },
        {
            "name": "conversations",
            "description": "AI conversation persistence - manage chat history",
        },
        {
            "name": "webhooks",
            "description": "Webhook management - subscribe to events and manage deliveries",
        },
        {
            "name": "agent",
            "description": "AI agent WebSocket endpoint for real-time chat",
        },
        {
            "name": "rag",
            "description": "Retrieval Augmented Generation endpoints",
        },
    ]

    # PII redaction in logs (GDPR/compliance)
    setup_logging()

    app = FastAPI(
        title=settings.PROJECT_NAME,
        summary="FastAPI application with Logfire observability",
        description="""
My telco relase FastAPI project

## Features
- **Authentication**: JWT-based authentication with refresh tokens
- **API Key**: Header-based API key authentication
- **Database**: Async database operations
- **Redis**: Caching and session storage
- **Rate Limiting**: Request rate limiting per client
- **Observability**: Logfire integration for tracing and monitoring
- **RAG**: Retrieval Augmented Generation with Milvus and LangChain

## Documentation

- [Swagger UI](/docs) - Interactive API documentation
- [ReDoc](/redoc) - Alternative documentation view
        """.strip(),
        version="0.1.0",
        openapi_url=openapi_url,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_tags=openapi_tags,
        contact={
            "name": "Raúl Hernández",
            "email": "rauherna@redhat.com",
        },
        license_info={
            "name": "MIT",
            "identifier": "MIT",
        },
        lifespan=lifespan,
    )
    # Logfire instrumentation. setup_logfire() is also called from the lifespan
    # for the runtime app, but we call it here too so that import-time test
    # clients (which never run lifespan) silence the "configure first" warning.
    setup_logfire()
    instrument_app(app)

    # Request ID middleware (for request correlation/debugging)
    app.add_middleware(RequestIDMiddleware)

    # Exception handlers
    register_exception_handlers(app)

    # CORS middleware
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=settings.CORS_ALLOW_METHODS,
        allow_headers=settings.CORS_ALLOW_HEADERS,
    )

    # Sentry
    if settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.SENTRY_DSN, enable_tracing=True)

    # Prometheus metrics
    from prometheus_fastapi_instrumentator import Instrumentator

    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_respect_env_var=True,
        should_instrument_requests_inprogress=True,
        excluded_handlers=[
            "/health",
            "/health/ready",
            "/health/live",
            settings.PROMETHEUS_METRICS_PATH,
        ],
        inprogress_name="http_requests_inprogress",
        inprogress_labels=True,
    )
    # Optional Bearer-token guard so the metrics endpoint can be exposed on a
    # public ingress without leaking internals. When PROMETHEUS_AUTH_TOKEN is
    # empty the endpoint is unauthenticated (typical for private networks).
    if settings.PROMETHEUS_AUTH_TOKEN:
        from fastapi import Depends, Header, HTTPException
        from fastapi import status as http_status

        def _verify_metrics_token(authorization: str = Header(default="")) -> None:
            expected = f"Bearer {settings.PROMETHEUS_AUTH_TOKEN}"
            import secrets as _secrets

            if not _secrets.compare_digest(authorization, expected):
                raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED)

        instrumentator.instrument(app).expose(
            app,
            endpoint=settings.PROMETHEUS_METRICS_PATH,
            include_in_schema=settings.PROMETHEUS_INCLUDE_IN_SCHEMA,
            dependencies=[Depends(_verify_metrics_token)],
        )
    else:
        instrumentator.instrument(app).expose(
            app,
            endpoint=settings.PROMETHEUS_METRICS_PATH,
            include_in_schema=settings.PROMETHEUS_INCLUDE_IN_SCHEMA,
        )

    # Rate limiting
    # Note: slowapi requires app.state.limiter - this is a library requirement,
    # not suitable for lifespan state pattern which is for request-scoped access
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from app.core.rate_limit import limiter

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Session middleware (for admin authentication and/or OAuth)
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

    # Admin panel (environment restricted)
    ADMIN_ALLOWED_ENVIRONMENTS = ["development", "local", "staging"]

    if settings.ENVIRONMENT in ADMIN_ALLOWED_ENVIRONMENTS:
        from app.admin import setup_admin

        setup_admin(app)

    # API Version Deprecation (uncomment when deprecating old versions)
    # Example: Mark v1 as deprecated when v2 is ready
    # from app.api.versioning import VersionDeprecationMiddleware
    # app.add_middleware(
    #     VersionDeprecationMiddleware,
    #     deprecated_versions={
    #         "v1": {
    #             "sunset": "2025-12-31",
    #             "link": "/docs/migration/v2",
    #             "message": "Please migrate to API v2",
    #         }
    #     },
    # )

    # Include API router
    app.include_router(api_router, prefix=settings.API_V1_STR)

    # Pagination
    add_pagination(app)

    return app


app = create_app()
