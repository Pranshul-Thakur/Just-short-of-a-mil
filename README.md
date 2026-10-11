# High-Throughput FastAPI Service

A FastAPI + Redis + PostgreSQL service tuned toward **1,000,000 requests/min (~16,700 RPS)**.

On a single 12-core machine (with the load generator running on the same box):

| Workload | Throughput | p99 latency |
|---|---|---|
| Cached reads, 20,000 users (warm) | **~14.3k RPS (~860k/min)** | ~37 ms |
| Cold start reads, 20,000 users | ~10.6k RPS | ~90 ms |
| Mixed 90% GET / 8% PUT / 2% POST, 20,000 users | **~9-10k RPS (~550-620k/min)** | 75-90 ms |

The starting point was ~1,200 RPS. No errors or timeouts in the final runs.

---

## Architecture

```
wrk (load generator)
   |
   v
nginx  (least_conn, keep-alive to upstream)
   |
   v
8 x FastAPI containers (1 uvicorn worker each)
   |  1. in-process TTLCache   (30 s, stores ready-to-send JSON bytes)
   |  2. Redis                 (one JSON string per user, 300 s TTL)
   |  3. PostgreSQL via asyncpg (only on a miss, and for writes)
```

### Request flows

**`GET /users/{id}`**
1. Check the replica's in-process cache. A hit returns the stored bytes directly (no dict building, no JSON encoding, no network call).
2. On a miss, `GET u:{id}` from Redis. A hit fills the local cache and returns.
3. On a Redis miss, query Postgres with asyncpg. Returns **404** if the user doesn't exist. Otherwise writes the JSON to Redis (`ex=300`), fills the local cache, and returns.

**`PUT /users/{id}`**: single `UPDATE ... RETURNING` via asyncpg, 404 if no row matched, then updates the Redis key and clears the key from the handling replica's local cache.

**`POST /users`**: single `INSERT ... RETURNING` via asyncpg. Does not write to Redis, so the first read of a new user goes to Postgres.

---

## Project layout

```
.
├── main.py              # FastAPI app and endpoints
├── cache.py             # Redis client (blocking connection pool)
├── database.py          # SQLAlchemy engine (startup / create_all only)
├── models.py            # User model
├── Dockerfile
├── docker-compose.yml   # api (8 replicas), nginx, redis, postgres
├── nginx.conf
├── requirements.txt
├── .dockerignore
├── locustfile.py        # legacy, see "Known limitations"
├── random.lua           # wrk: random reads, IDs 1-1000
├── wide.lua             # wrk: random reads, IDs 1-20000
├── mixed.lua            # wrk: 90/8/2 mix, IDs 1-1000
└── mixed_wide.lua       # wrk: 90/8/2 mix, IDs 1-20000
```

---

## Quick start

Requires Docker Desktop (with WSL integration enabled if you use WSL2). On WSL, keep the project in the Linux filesystem (`~/project`), not `/mnt/c/...`.

```bash
cd ~/project
docker compose up -d
sleep 10
docker compose restart nginx      # always after the API containers are (re)created
docker compose ps
```

Check it works:

```bash
curl -s localhost:8000/health
curl -X POST localhost:8000/users -H "Content-Type: application/json" -d '{"name":"test"}'
curl -s localhost:8000/users/1
```

> **Why restart nginx?** nginx resolves the API container IPs once at startup. If the API containers are recreated afterwards, nginx points at stale addresses and returns 502s.

If you edit `nginx.conf`, recreate the container (a plain restart can fail on the bind mount after `sed -i`):

```bash
docker compose up -d --force-recreate nginx
```

---

## Configuration

### nginx
| Setting | Reason |
|---|---|
| `worker_processes auto` | Use all cores (a single worker was one of the busiest containers) |
| `access_log off` | Per-request logging costs throughput |
| `least_conn` | Sends new requests to the least busy replica; lowered p90/p99 |
| upstream `keepalive 128`, HTTP/1.1, empty `Connection` header | Reuse upstream connections |

### API containers
| Setting | Reason |
|---|---|
| 8 replicas via `deploy.replicas: 8` | Python uses one core per process, so scale with containers |
| 1 uvicorn worker per container | Original 4 x 3 workers (12 processes) caused CPU contention |
| `uvicorn[standard]`, `orjson`, `cachetools` | uvloop/httptools speed up the event loop and parsing |
| `--no-access-log` | uvicorn per-request logging costs CPU |
| `--timeout-keep-alive 75` | Keep idle connections open longer than nginx does, to avoid resets |

### In-process cache
`TTLCache(maxsize=100_000, ttl=30)` per replica, storing pre-serialized bytes. The TTL was raised from 5 s to 30 s to reduce periodic stalls when entries expired together.

### Redis
- `BlockingConnectionPool(max_connections=500, timeout=5)`. The original pool (100 connections) raised `MaxConnectionsError` immediately when exhausted, which became HTTP 500s.
- One JSON string per user (`u:{id}`, `ex=300`) instead of a hash.
- `maxmemory 512mb`, `allkeys-lru`.

### PostgreSQL
- `max_connections=300` (default 100 was exceeded by pool sizes x replicas).
- `synchronous_commit=off`: commits don't wait for a disk flush. **Trade-off:** a crash can lose roughly the last half second of commits (no corruption).
- asyncpg pool per replica: min 5, max 10.
- SQLAlchemy is only used at startup for `create_all`; its pool is 5 + 5 with `pool_pre_ping=False`.

---

## Benchmarking

### Method
- **Tool:** [wrk](https://github.com/wg/wrk), 2 threads, 200 connections, 30 s per run.
- **Run it inside Docker's network** (`--network project_default`, target `http://nginx`). Going through `localhost:8000` from WSL adds Windows port forwarding and lowers results (8.2k vs ~11k on the same stack at the time).
- **Run each test twice.** Run 1 warms the caches; run 2 is the number to trust.
- **Expect ~10% variance** between runs on Docker Desktop.
- Locust (`HttpUser`) capped at ~1.2k RPS because the load generator itself was the bottleneck, so it is not used for these tests.

### Commands

```bash
# Seed users (1000 for small tests, 20000 for wide tests)
seq 1 20000 | xargs -P 30 -I{} curl -s -o /dev/null -X POST localhost:8000/users \
  -H "Content-Type: application/json" -d '{"name":"bulk{}"}'

# Mixed workload, 20,000 users
for i in 1 2; do
  docker run --rm --network project_default -v "$PWD/mixed_wide.lua:/mixed_wide.lua" \
    williamyeh/wrk -t2 -c200 -d30s --latency -s /mixed_wide.lua http://nginx \
    | grep -E "Requests/sec|Non-2xx|timeout|50%|99%"
done

# Cold-start test: empty Redis first
docker compose exec redis redis-cli flushall

# Watch the bottleneck (run mid-test, in a second terminal)
docker stats --no-stream
```

If a result shows a `Non-2xx` line, treat the RPS as invalid: failures return fast and inflate throughput.

### wrk script for the 90/8/2 mix (`mixed_wide.lua`)

```lua
math.randomseed(os.time())

request = function()
  local r = math.random(100)
  local id = math.random(1, 20000)

  if r <= 90 then
    return wrk.format("GET", "/users/" .. id)
  elseif r <= 98 then
    return wrk.format("PUT", "/users/" .. id,
      {["Content-Type"] = "application/json"},
      '{"name":"updated' .. math.random(1000000) .. '"}')
  else
    return wrk.format("POST", "/users",
      {["Content-Type"] = "application/json"},
      '{"name":"new' .. math.random(1000000) .. '"}')
  end
end
```

---

## Results timeline

| Stage | RPS | p99 | Notes |
|---|---|---|---|
| Original (Locust) | ~1,200 | n/a | Load generator limit |
| wrk via `localhost`, 6 replicas | 8,223 | 657 ms | Through Windows port forwarding |
| wrk in-network | 11,006 | ~1 s | 254 non-2xx, plus timeouts |
| Redis pool fix | 13,172 | 416 ms | 0 non-2xx |
| TTL 30 s and `least_conn` | 14,353 | 27 ms | 1 hot key |
| Random IDs 1-1,000, 6 replicas | 12.3-12.9k | 31-41 ms | 3 runs |
| Mixed 90/8/2, original write path | ~5.2k | 0.75-1.2 s | 76 timeouts |
| Mixed, single-statement SQL | ~5.4k | ~297 ms | SQLAlchemy CPU-bound |
| Mixed, asyncpg writes | ~10.0k | ~90 ms | Largest write-side gain |
| Mixed, 8 replicas | ~12.0k | ~70 ms | |
| Mixed, cached bytes | ~12.9k | ~65 ms | 1,000 IDs |
| 20k IDs reads, cold, SQLAlchemy miss path | 3.9k | 286 ms | |
| 20k IDs reads, cold, asyncpg miss path | 10.6k | 90 ms | |
| 20k IDs reads, warm | 14.3k | 37 ms | ~860k/min |
| 20k IDs mixed 90/8/2 | 9-10k | 75-90 ms | ~550-620k/min |
| Redis strings (final change) | ~9.3k | ~84 ms | Within noise, kept |

Some changes were applied together (single-statement SQL, `synchronous_commit=off`, `pool_pre_ping=False`), so their individual gains were not isolated.

One result was discarded: an 18.4k RPS run where 31% of responses were errors. The inflated number came from Postgres connection exhaustion on a cold cache (inferred from pool maths; the errors disappeared after the fix).

---

## What changed from the original

**Load testing and environment**
- Replaced Locust with wrk, run inside Docker's network.
- Moved the project from `/mnt/c/...` into the WSL filesystem.
- Added `.dockerignore` (build context 141 MB to ~316 KB).
- Stripped Windows (CRLF) line endings from `docker-compose.yml`.
- Added `deploy.replicas: 8` to the compose file.

**API code**
- In-process cache with pre-serialized bytes and raw `Response` on the read path.
- Redis hash replaced with a JSON string; `PUT` writes the same format.
- `POST`/`PUT` rewritten as single-statement `RETURNING` queries, then moved to asyncpg.
- Cache-miss read moved to asyncpg.
- Missing user now returns **404** instead of `200 {"error": ...}`.
- `cache.py` rewritten with the blocking pool.
- SQLAlchemy pools reduced; `pool_pre_ping` disabled.

**Infrastructure**
- nginx and uvicorn settings listed above, one worker per container.
- Postgres `max_connections=300` and `synchronous_commit=off`.

---

## Bottleneck analysis

In the final mixed tests (mid-run `docker stats`):

| Component | CPU |
|---|---|
| 8 API containers | ~98-100% each (one full core) |
| nginx | ~100-195% |
| wrk | ~50-83% |
| Postgres | 40-80% (earlier runs) |
| Redis | ~11% |

All 12 cores were in use, so the limit is hardware, not code. The last code changes (Redis strings, asyncpg on the miss path) gave single-digit-percent gains on the warm path.

Replica count is workload-dependent: for read-only cached traffic, 6 replicas beat 8 (13.6k vs 10.0k, because extra replicas competed with nginx and wrk for CPU). For the mixed workload, where the API is CPU-bound on writes, 8 beat 6 (~12.0k vs ~10.0k).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker: command not found` in WSL | Docker Desktop not running, or WSL integration off | Start Docker Desktop; Settings > Resources > WSL Integration |
| `port is already allocated` on 8000 | Old stack still running | `docker ps`, stop the old containers |
| 502 from nginx after rebuilding | nginx has stale upstream IPs | `docker compose restart nginx` |
| nginx restart fails with a mount error | `sed -i` replaced the bind-mounted file | `docker compose up -d --force-recreate nginx` |
| `Non-2xx` lines in wrk output | Server errors; check API logs | `docker compose logs api --tail 200 \| grep -E "(Error\|Exception)[:(]"` |
| `MaxConnectionsError` (Redis) | Pool exhausted | Blocking pool (already applied) |
| `too many clients` (Postgres) | Pools x replicas exceed `max_connections` | Smaller pools or higher limit (already applied) |
| `sed` edits silently don't match | CRLF line endings | `sed -i 's/\r$//' <file>` |
| Pasted multi-line commands hang on `>` | Heredoc cut off | Ctrl+C, paste in smaller pieces |

---

## Known limitations

- **Stale reads:** after a `PUT`, only the handling replica's local cache is cleared. Other replicas can serve the old value for up to 30 s. Lower the TTL or add Redis pub/sub invalidation if that is too long.
- **Durability:** `synchronous_commit=off` can lose the last ~0.5 s of commits on a Postgres crash. Remove it if that is unacceptable (write throughput will drop).
- **`POST` does not warm Redis:** the first read of a new user hits Postgres.
- **No stampede protection:** when a very hot key expires under load, many requests hit Redis and Postgres at once. A short lock or jittered TTLs would fix this.
- **Response shape changed:** `GET` no longer includes the `"source"` field.
- **Cleanup items:**
  - `locustfile.py` still uses the old `HttpUser`.
  - `ORJSONResponse` is imported but unused (and deprecated in newer FastAPI).
  - `@app.on_event` is deprecated; use `lifespan`.
  - `create_all` runs in every replica at startup (race); use a migration step such as Alembic.
  - The `index=True` on `name` slows every write if you never query by name.
- **Measurement:** wrk shared the machine with the stack. An attempted remote-load test accidentally ran on the same PC (the wrk container appeared in `docker stats`), so no remote results are reported.

---

## Path to 1,000,000 requests/min

- **Read-heavy traffic** is already at ~86% of target on one box.
- **Mixed traffic** needs roughly **1.6x more compute**: about 16-24 cores for the API tier alone.
- The API replicas are stateless, so adding cores should scale close to linearly.
- Suggested layout:
  - API replicas on one or more machines.
  - Postgres and Redis on their own hosts.
  - A real load balancer in front (or in place of the single nginx container).
  - The load generator on a separate machine to measure the true ceiling.
- Size for **2-3x the average rate** to cover traffic peaks.
- Run a **30+ minute soak test** to catch memory growth and connection leaks (30-second runs hide these).