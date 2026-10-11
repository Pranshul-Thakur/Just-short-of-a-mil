import os
import redis.asyncio as redis

pool = redis.BlockingConnectionPool(
    host=os.getenv("REDIS_HOST", "redis"),
    port=6379,
    decode_responses=True,
    max_connections=500,
    timeout=5,
)

redis_client = redis.Redis(connection_pool=pool)