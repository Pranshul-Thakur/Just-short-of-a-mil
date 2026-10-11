from contextlib import asynccontextmanager

import asyncpg
import orjson
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import models  # noqa: F401  (registers the User table on Base.metadata)
from cache import redis_client
from database import Base, engine, DATABASE_URL

local_cache = TTLCache(maxsize=100_000, ttl=30)
pg_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pg_pool

    # create_all runs in every replica; ignore the race if another one wins
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        pass

    pg_pool = await asyncpg.create_pool(
        DATABASE_URL.replace("+asyncpg", ""),
        min_size=5,
        max_size=10,
    )
    yield
    await pg_pool.close()


app = FastAPI(title="High-Throughput REST API", lifespan=lifespan)


class UserCreate(BaseModel):
    name: str


class UserUpdate(BaseModel):
    name: str


@app.get("/")
async def home():
    return {"message": "API is running"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    # 1. In-process cache (no network call)
    if (hit := local_cache.get(user_id)) is not None:
        return Response(content=hit, media_type="application/json")

    # 2. Redis (one pre-serialized JSON string per user)
    key = f"u:{user_id}"
    body = await redis_client.get(key)

    # 3. Postgres on a miss
    if body is None:
        row = await pg_pool.fetchrow(
            "SELECT id, name FROM users WHERE id = $1", user_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        body = orjson.dumps({"user_id": row["id"], "name": row["name"]}).decode()
        await redis_client.set(key, body, ex=300)

    local_cache[user_id] = body
    return Response(content=body, media_type="application/json")


@app.post("/users")
async def create_user(user_data: UserCreate):
    row = await pg_pool.fetchrow(
        "INSERT INTO users (name) VALUES ($1) RETURNING id, name",
        user_data.name,
    )
    return {"user_id": row["id"], "name": row["name"]}


@app.put("/users/{user_id}")
async def update_user(user_id: int, user_data: UserUpdate):
    row = await pg_pool.fetchrow(
        "UPDATE users SET name = $1 WHERE id = $2 RETURNING id, name",
        user_data.name,
        user_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    await redis_client.set(
        f"u:{user_id}",
        orjson.dumps({"user_id": row["id"], "name": row["name"]}).decode(),
        ex=300,
    )
    local_cache.pop(user_id, None)

    return {"user_id": row["id"], "name": row["name"]}