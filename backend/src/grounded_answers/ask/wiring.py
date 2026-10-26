"""
Putting the pieces of POST /ask together for the running app (ADR 0033). Built once in the lifespan, only when
ASK_ENABLED is on. Tests take the parts from `app.state.ask_deps`, change one (a limiter with a fake clock, a
registry with a fake model) and call `deps.service()` for a service that uses them.
"""

from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.ask.providers import ProviderRegistry
from grounded_answers.ask.service import AskConfig, AskService
from grounded_answers.ask.store import DbStore
from grounded_answers.ask.types import Embedder, InFlight, Limiter, MeteringFlag, Store
from grounded_answers.ask.valkey import AskLimiter, InFlightSet, MeteringFlagStore
from grounded_answers.config import Settings
from grounded_answers.embedding.query import QueryEmbedder
from grounded_answers.rate_limit import wall_clock_ms

# How long past the deadline an in-flight entry lives, if its request never releases it (ADR 0033, D2).
IN_FLIGHT_MARGIN_SECONDS = 5.0


@dataclass
class AskDeps:
    store: Store
    limiter: Limiter
    in_flight: InFlight
    flag: MeteringFlag
    embedder: Embedder
    providers: ProviderRegistry
    log_key: bytes
    config: AskConfig

    def service(self) -> AskService:
        return AskService(
            limiter=self.limiter,
            in_flight=self.in_flight,
            flag=self.flag,
            store=self.store,
            embedder=self.embedder,
            providers=self.providers,
            log_key=self.log_key,
            config=self.config,
        )


def build_ask_deps(
    settings: Settings,
    *,
    cache: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    rate_key: bytes,
    log_key: bytes,
    embedder: QueryEmbedder,
    http_client: httpx.AsyncClient,
) -> AskDeps:
    return AskDeps(
        store=DbStore(session_factory),
        limiter=AskLimiter(
            cache,
            rate_key,
            wall_clock_ms,
            tenant_limit=settings.ask_rate_tenant_per_min,
            user_limit=settings.ask_rate_user_per_min,
        ),
        in_flight=InFlightSet(
            cache,
            rate_key,
            wall_clock_ms,
            limit=settings.ask_inflight_per_tenant,
            hold_seconds=settings.ask_deadline_seconds + IN_FLIGHT_MARGIN_SECONDS,
        ),
        flag=MeteringFlagStore(cache, rate_key),
        embedder=embedder,
        providers=ProviderRegistry.from_settings(settings, http_client),
        log_key=log_key,
        config=AskConfig(
            deadline_seconds=settings.ask_deadline_seconds, top_k=settings.retrieval_top_k
        ),
    )
