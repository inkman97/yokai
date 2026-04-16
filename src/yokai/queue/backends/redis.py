"""Redis-backed implementation of all four queue interfaces.

Storage model:
  yokai:job:{job_id}                HASH    job fields (status, payload, ...)
  yokai:queue:queued                ZSET    job_ids in QUEUED, score = created_at epoch
  yokai:lease:{job_id}              STRING  worker_id, with TTL = lease_duration
  yokai:story:{story_key}           STRING  job_id (dedupe: 1 in-flight job per story)
  yokai:status:{status}             SET     job_ids in that status (for stats / list_by_status)
  yokai:result:{job_id}             HASH    JobResult fields
  yokai:results:pending             SET     job_ids of success results awaiting postprocessing
  yokai:worker:{worker_id}          HASH    worker info, with TTL refreshed by heartbeat
  yokai:workers:all                 SET     worker_ids (for list_alive / list_dead scans)
  yokai:coord:lock                  STRING  owner_id, with TTL = lease_duration

Atomicity: multi-step operations (enqueue + dedupe, dequeue + lease,
update_status with status set bookkeeping) use Lua scripts evaluated
server-side. Redis runs Lua single-threaded, so each script is atomic
across all clients.

Connection model: a single redis.Redis client is reused. The redis-py
client is thread-safe for normal commands and uses a connection pool.

Production caveats (NOT covered by the test suite which uses fakeredis):
- Redis Cluster: this backend is NOT cluster-aware. Lua scripts assume
  all keys hash to the same slot. For Cluster, hash-tag the keys
  (e.g. "yokai:{shard1}:job:..."). Single-node Redis or Redis Sentinel
  is fine.
- AOF persistence: enable `appendonly yes` for crash safety. Without
  AOF, in-flight jobs can be lost on Redis restart.
- Maxmemory eviction policy: jobs/results may be evicted under memory
  pressure. Use `maxmemory-policy noeviction` for queue durability.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import redis

from yokai.queue.exceptions import (
    DuplicateJobError,
    JobNotFound,
    LeaseExpiredError,
    QueueBackendError,
)
from yokai.queue.interfaces import (
    CoordinatorLock,
    JobQueue,
    ResultStore,
    WorkerRegistry,
)
from yokai.queue.models import (
    TERMINAL_STATES,
    Job,
    JobResult,
    JobStatus,
    WorkerInfo,
)
from yokai.queue.state_machine import needs_recovery, transition

def _k_job(job_id: str) -> str:
    return f"yokai:job:{job_id}"


def _k_lease(job_id: str) -> str:
    return f"yokai:lease:{job_id}"


def _k_story(story_key: str) -> str:
    return f"yokai:story:{story_key}"


def _k_status(status: JobStatus) -> str:
    return f"yokai:status:{status.value}"


def _k_result(job_id: str) -> str:
    return f"yokai:result:{job_id}"


def _k_worker(worker_id: str) -> str:
    return f"yokai:worker:{worker_id}"


K_QUEUE = "yokai:queue:queued"
K_RESULTS_PENDING = "yokai:results:pending"
K_WORKERS_ALL = "yokai:workers:all"
K_COORD_LOCK = "yokai:coord:lock"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt is not None else ""


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


# ---------- Lua scripts ---------- #

# enqueue:
#   KEYS[1] = yokai:story:{story_key}
#   KEYS[2] = yokai:job:{job_id}
#   KEYS[3] = yokai:queue:queued
#   KEYS[4] = yokai:status:{status}
#   ARGV    = job_id, story_key, repo_slug, payload_json, status, attempts,
#             max_attempts, created_at_iso, updated_at_iso, score
# Returns: 1 if enqueued, 0 if duplicate (existing job_id is returned in result)
LUA_ENQUEUE = """
local existing = redis.call('GET', KEYS[1])
if existing then
    return {0, existing}
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('HSET', KEYS[2],
    'job_id', ARGV[1],
    'story_key', ARGV[2],
    'repo_slug', ARGV[3],
    'payload', ARGV[4],
    'status', ARGV[5],
    'attempts', ARGV[6],
    'max_attempts', ARGV[7],
    'created_at', ARGV[8],
    'updated_at', ARGV[9])
redis.call('ZADD', KEYS[3], ARGV[10], ARGV[1])
redis.call('SADD', KEYS[4], ARGV[1])
return {1, ARGV[1]}
"""

# dequeue: pop oldest job from QUEUED zset whose not_before <= now.
# We cannot easily filter zset members by hash field, so we iterate the
# top of the zset and check each one's not_before. The zset is small in
# practice (jobs awaiting dispatch).
#   KEYS[1] = yokai:queue:queued
#   ARGV[1] = worker_id
#   ARGV[2] = lease_duration_seconds
#   ARGV[3] = picked_up_at_iso
#   ARGV[4] = now_iso (for updated_at)
#   ARGV[5] = now_epoch (for not_before comparison)
#   ARGV[6] = picked_up status string
# Returns: nil if none available, otherwise job_id
LUA_DEQUEUE = """
local now_epoch = tonumber(ARGV[5])
local cursor = 0
local checked = 0
local max_check = 100
while checked < max_check do
    local items = redis.call('ZRANGE', KEYS[1], cursor, cursor)
    if #items == 0 then
        return nil
    end
    local job_id = items[1]
    local job_key = 'yokai:job:' .. job_id
    local nb = redis.call('HGET', job_key, 'not_before')
    local eligible = true
    if nb and nb ~= '' then
        local nb_dt = redis.call('HGET', job_key, 'not_before_epoch')
        if nb_dt and tonumber(nb_dt) > now_epoch then
            eligible = false
        end
    end
    if eligible then
        local current_status = redis.call('HGET', job_key, 'status')
        local current_attempts = tonumber(redis.call('HGET', job_key, 'attempts')) or 0
        redis.call('ZREM', KEYS[1], job_id)
        redis.call('SREM', 'yokai:status:' .. current_status, job_id)
        redis.call('SADD', 'yokai:status:' .. ARGV[6], job_id)
        redis.call('HSET', job_key,
            'status', ARGV[6],
            'worker_id', ARGV[1],
            'picked_up_at', ARGV[3],
            'updated_at', ARGV[4],
            'attempts', current_attempts + 1)
        redis.call('SET', 'yokai:lease:' .. job_id, ARGV[1], 'EX', ARGV[2])
        return job_id
    end
    cursor = cursor + 1
    checked = checked + 1
end
return nil
"""

# update_status: validate worker_id holds lease (if provided), update
# status set membership, update fields, manage lease and not_before.
#   KEYS[1] = yokai:job:{job_id}
#   KEYS[2] = yokai:lease:{job_id}
#   ARGV[1] = job_id
#   ARGV[2] = new_status
#   ARGV[3] = required_worker_id (empty if no check)
#   ARGV[4] = error (or empty)
#   ARGV[5] = updated_at_iso
#   ARGV[6] = is_terminal "1"/"0"
#   ARGV[7] = clear_worker_lease "1"/"0" (when going back to QUEUED)
#   ARGV[8] = not_before_iso (or empty)
#   ARGV[9] = not_before_epoch (or empty)
#   ARGV[10] = score (epoch of created_at, used to re-add to ZSET)
#   ARGV[11] = story_key (used to clean up dedupe key on terminal)
# Returns: {ok_code, current_status_or_error}
#   ok_code: 1 = success, 0 = job not found, -1 = lease check failed
LUA_UPDATE_STATUS = """
local exists = redis.call('EXISTS', KEYS[1])
if exists == 0 then
    return {0, 'not found'}
end
local current_status = redis.call('HGET', KEYS[1], 'status')
if ARGV[3] ~= '' then
    local current_worker = redis.call('HGET', KEYS[1], 'worker_id')
    if current_worker ~= ARGV[3] then
        return {-1, current_worker or ''}
    end
end
redis.call('SREM', 'yokai:status:' .. current_status, ARGV[1])
redis.call('SADD', 'yokai:status:' .. ARGV[2], ARGV[1])
redis.call('HSET', KEYS[1], 'status', ARGV[2], 'updated_at', ARGV[5])
if ARGV[4] ~= '' then
    redis.call('HSET', KEYS[1], 'last_error', ARGV[4])
end
if ARGV[6] == '1' then
    redis.call('HSET', KEYS[1], 'completed_at', ARGV[5])
    redis.call('DEL', KEYS[2])
    redis.call('HDEL', KEYS[1], 'not_before', 'not_before_epoch')
    redis.call('ZREM', 'yokai:queue:queued', ARGV[1])
    redis.call('DEL', 'yokai:story:' .. ARGV[11])
end
if ARGV[7] == '1' then
    redis.call('HSET', KEYS[1], 'worker_id', '')
    redis.call('DEL', KEYS[2])
    if ARGV[8] ~= '' then
        redis.call('HSET', KEYS[1], 'not_before', ARGV[8], 'not_before_epoch', ARGV[9])
    else
        redis.call('HDEL', KEYS[1], 'not_before', 'not_before_epoch')
    end
    redis.call('ZADD', 'yokai:queue:queued', ARGV[10], ARGV[1])
end
return {1, ARGV[2]}
"""

# reclaim_expired_leases: find jobs in PICKED_UP/AGENT_RUNNING whose
# lease key has expired (no longer exists), then either requeue or
# dead-letter based on attempts vs max_attempts.
#   ARGV[1] = updated_at_iso
#   ARGV[2] = picked_up_status
#   ARGV[3] = agent_running_status
#   ARGV[4] = queued_status
#   ARGV[5] = agent_failed_status
#   ARGV[6] = dead_lettered_status
# Returns: list of reclaimed job_ids
LUA_RECLAIM = """
local reclaimed = {}
local statuses = {ARGV[2], ARGV[3]}
for _, st in ipairs(statuses) do
    local jobs = redis.call('SMEMBERS', 'yokai:status:' .. st)
    for _, job_id in ipairs(jobs) do
        local lease_exists = redis.call('EXISTS', 'yokai:lease:' .. job_id)
        if lease_exists == 0 then
            local job_key = 'yokai:job:' .. job_id
            local attempts = tonumber(redis.call('HGET', job_key, 'attempts')) or 0
            local max_attempts = tonumber(redis.call('HGET', job_key, 'max_attempts')) or 3
            local created = redis.call('HGET', job_key, 'created_at')
            local story_key = redis.call('HGET', job_key, 'story_key')
            redis.call('SREM', 'yokai:status:' .. st, job_id)
            if attempts >= max_attempts then
                redis.call('SADD', 'yokai:status:' .. ARGV[6], job_id)
                redis.call('HSET', job_key,
                    'status', ARGV[6],
                    'updated_at', ARGV[1],
                    'completed_at', ARGV[1],
                    'last_error', 'Worker lease expired and retry budget exhausted')
                redis.call('DEL', 'yokai:story:' .. story_key)
            else
                redis.call('SADD', 'yokai:status:' .. ARGV[4], job_id)
                redis.call('HSET', job_key,
                    'status', ARGV[4],
                    'updated_at', ARGV[1],
                    'worker_id', '',
                    'last_error', 'Worker lease expired, returning to queue')
                redis.call('HDEL', job_key, 'not_before', 'not_before_epoch')
                local score = 0
                if created then
                    -- reuse created_at epoch as ZSET score; we lost the
                    -- original score, recompute from ISO timestamp.
                    -- Lua does not parse ISO dates; fall back to a
                    -- sentinel score (now). Acceptable: reclaimed jobs
                    -- have lower priority than freshly enqueued ones.
                    score = tonumber(redis.call('TIME')[1])
                end
                redis.call('ZADD', 'yokai:queue:queued', score, job_id)
            end
            table.insert(reclaimed, job_id)
        end
    end
end
return reclaimed
"""


class RedisBackend(JobQueue, ResultStore, WorkerRegistry, CoordinatorLock):
    """Redis-backed implementation of all four queue interfaces."""

    def __init__(
        self,
        client: redis.Redis | None = None,
        *,
        url: str | None = None,
        worker_ttl_seconds: int = 60,
    ) -> None:
        if client is not None:
            self._r = client
        elif url is not None:
            self._r = redis.Redis.from_url(url, decode_responses=True)
        else:
            raise QueueBackendError(
                "RedisBackend requires either a client or a url"
            )
        # Sanity check: client must decode responses to str
        if isinstance(self._r.connection_pool.connection_kwargs.get(
            "decode_responses"
        ), bool):
            if not self._r.connection_pool.connection_kwargs["decode_responses"]:
                raise QueueBackendError(
                    "RedisBackend requires a redis client with "
                    "decode_responses=True"
                )
        self._worker_ttl = worker_ttl_seconds
        # Register Lua scripts (returns Script objects we can call)
        self._enqueue_script = self._r.register_script(LUA_ENQUEUE)
        self._dequeue_script = self._r.register_script(LUA_DEQUEUE)
        self._update_status_script = self._r.register_script(LUA_UPDATE_STATUS)
        self._reclaim_script = self._r.register_script(LUA_RECLAIM)

    def _hash_to_job(self, h: dict[str, str]) -> Job:
        return Job(
            job_id=h["job_id"],
            story_key=h["story_key"],
            repo_slug=h["repo_slug"],
            payload=json.loads(h.get("payload") or "{}"),
            status=JobStatus(h["status"]),
            attempts=int(h.get("attempts") or 0),
            max_attempts=int(h.get("max_attempts") or 3),
            created_at=_parse(h.get("created_at")),
            updated_at=_parse(h.get("updated_at")),
            picked_up_at=_parse(h.get("picked_up_at")),
            completed_at=_parse(h.get("completed_at")),
            last_error=h.get("last_error") or None,
            worker_id=h.get("worker_id") or None,
            not_before=_parse(h.get("not_before")),
        )

    def enqueue(self, job: Job) -> Job:
        new_status = transition(job.status, JobStatus.QUEUED)
        now = _utcnow()
        score = _epoch(job.created_at)
        result = self._enqueue_script(
            keys=[
                _k_story(job.story_key),
                _k_job(job.job_id),
                K_QUEUE,
                _k_status(new_status),
            ],
            args=[
                job.job_id,
                job.story_key,
                job.repo_slug,
                json.dumps(job.payload),
                new_status.value,
                str(job.attempts),
                str(job.max_attempts),
                _iso(job.created_at),
                _iso(now),
                str(score),
            ],
        )
        if result[0] == 0:
            existing_id = result[1]
            existing = self.get(existing_id)
            raise DuplicateJobError(
                f"Story {job.story_key} already has an in-flight "
                f"job: {existing.job_id} (status={existing.status.value})"
            )
        return self.get(job.job_id)

    def dequeue(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> Job | None:
        now = _utcnow()
        ttl = int(lease_duration.total_seconds())
        if ttl <= 0:
            # Immediate-expiry lease: pick the job but mark its lease
            # as already expired (no key set). The reclaim loop will
            # pick it up on the next cycle.
            ttl = 1  # minimum allowed by Redis EX
        job_id = self._dequeue_script(
            keys=[K_QUEUE],
            args=[
                worker_id,
                str(ttl),
                _iso(now),
                _iso(now),
                str(_epoch(now)),
                JobStatus.PICKED_UP.value,
            ],
        )
        if job_id is None:
            return None
        if int(lease_duration.total_seconds()) <= 0:
            # Force-expire the lease key right after creation so the
            # reclaim test can fire immediately.
            self._r.delete(_k_lease(job_id))
        return self.get(job_id)

    def update_status(
        self,
        job_id: str,
        new_status: JobStatus,
        worker_id: str | None = None,
        error: str | None = None,
        not_before: datetime | None = None,
    ) -> Job:
        # Pre-fetch current to validate transition (Lua does not know
        # the state machine). If the job does not exist, surface the
        # right exception.
        current = self.get(job_id)
        validated_status = transition(current.status, new_status)
        is_terminal = validated_status in TERMINAL_STATES
        going_back_to_queued = (
            validated_status == JobStatus.QUEUED
            and current.status != JobStatus.PENDING
        )

        result = self._update_status_script(
            keys=[_k_job(job_id), _k_lease(job_id)],
            args=[
                job_id,
                validated_status.value,
                worker_id or "",
                (error or "")[:2000],
                _iso(_utcnow()),
                "1" if is_terminal else "0",
                "1" if going_back_to_queued else "0",
                _iso(not_before) if not_before else "",
                str(_epoch(not_before)) if not_before else "",
                str(_epoch(current.created_at)),
                current.story_key,
            ],
        )
        if result[0] == 0:
            raise JobNotFound(f"Job {job_id} not found")
        if result[0] == -1:
            raise LeaseExpiredError(
                f"Worker {worker_id} does not hold lease on job "
                f"{job_id} (current owner: {result[1] or 'none'})"
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        h = self._r.hgetall(_k_job(job_id))
        if not h:
            raise JobNotFound(f"Job {job_id} not found")
        return self._hash_to_job(h)

    def list_by_status(
        self,
        status: JobStatus,
        limit: int = 100,
    ) -> list[Job]:
        ids = self._r.smembers(_k_status(status))
        jobs: list[Job] = []
        for job_id in ids:
            try:
                jobs.append(self.get(job_id))
            except JobNotFound:
                continue  # raced with deletion
        jobs.sort(key=lambda j: j.created_at)
        return jobs[:limit]

    def reclaim_expired_leases(self) -> list[Job]:
        ids = self._reclaim_script(
            args=[
                _iso(_utcnow()),
                JobStatus.PICKED_UP.value,
                JobStatus.AGENT_RUNNING.value,
                JobStatus.QUEUED.value,
                JobStatus.AGENT_FAILED.value,
                JobStatus.DEAD_LETTERED.value,
            ],
        )
        out: list[Job] = []
        for job_id in ids:
            try:
                out.append(self.get(job_id))
            except JobNotFound:
                continue
        return out

    def stats(self) -> dict[JobStatus, int]:
        out: dict[JobStatus, int] = {}
        for status in JobStatus:
            out[status] = self._r.scard(_k_status(status))
        return out

    def put(self, result: JobResult) -> None:
        with self._r.pipeline() as pipe:
            pipe.hset(
                _k_result(result.job_id),
                mapping={
                    "job_id": result.job_id,
                    "success": "1" if result.success else "0",
                    "agent_output": result.agent_output or "",
                    "error": result.error or "",
                    "traceback": result.traceback or "",
                    "duration_seconds": str(result.duration_seconds),
                    "branch_name": result.branch_name or "",
                    "commit_sha": result.commit_sha or "",
                    "completed_at": _iso(result.completed_at),
                },
            )
            if result.success:
                pipe.sadd(K_RESULTS_PENDING, result.job_id)
            else:
                pipe.srem(K_RESULTS_PENDING, result.job_id)
            pipe.execute()

    def get_result(self, job_id: str) -> JobResult | None:
        h = self._r.hgetall(_k_result(job_id))
        if not h:
            return None
        return JobResult(
            job_id=h["job_id"],
            success=h["success"] == "1",
            agent_output=h.get("agent_output") or "",
            error=h.get("error") or None,
            traceback=h.get("traceback") or None,
            duration_seconds=float(h.get("duration_seconds") or 0),
            branch_name=h.get("branch_name") or None,
            commit_sha=h.get("commit_sha") or None,
            completed_at=_parse(h.get("completed_at")) or _utcnow(),
        )

    def pending_for_postprocessing(
        self, limit: int = 50
    ) -> list[JobResult]:
        candidate_ids = list(self._r.smembers(K_RESULTS_PENDING))
        out: list[JobResult] = []
        for job_id in candidate_ids:
            try:
                job = self.get(job_id)
            except JobNotFound:
                # Stale entry, clean up
                self._r.srem(K_RESULTS_PENDING, job_id)
                continue
            if job.status != JobStatus.AGENT_COMPLETED:
                continue
            r = self.get_result(job_id)
            if r is None or not r.success:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        # Sort by completed_at to keep behaviour parity with sqlite
        out.sort(key=lambda r: r.completed_at)
        return out

    def register(self, worker: WorkerInfo) -> None:
        with self._r.pipeline() as pipe:
            pipe.hset(
                _k_worker(worker.worker_id),
                mapping={
                    "worker_id": worker.worker_id,
                    "hostname": worker.hostname,
                    "pid": str(worker.pid),
                    "started_at": _iso(worker.started_at),
                    "last_heartbeat_at": _iso(worker.last_heartbeat_at),
                    "current_job_id": worker.current_job_id or "",
                },
            )
            pipe.expire(_k_worker(worker.worker_id), self._worker_ttl)
            pipe.sadd(K_WORKERS_ALL, worker.worker_id)
            pipe.execute()

    def heartbeat(
        self, worker_id: str, current_job_id: str | None
    ) -> None:
        if not self._r.exists(_k_worker(worker_id)):
            return  # noop for unknown workers (matches memory backend)
        with self._r.pipeline() as pipe:
            pipe.hset(
                _k_worker(worker_id),
                mapping={
                    "last_heartbeat_at": _iso(_utcnow()),
                    "current_job_id": current_job_id or "",
                },
            )
            pipe.expire(_k_worker(worker_id), self._worker_ttl)
            pipe.execute()

    def deregister(self, worker_id: str) -> None:
        with self._r.pipeline() as pipe:
            pipe.delete(_k_worker(worker_id))
            pipe.srem(K_WORKERS_ALL, worker_id)
            pipe.execute()

    def _list_workers(self) -> list[WorkerInfo]:
        ids = list(self._r.smembers(K_WORKERS_ALL))
        out: list[WorkerInfo] = []
        for wid in ids:
            h = self._r.hgetall(_k_worker(wid))
            if not h:
                # TTL expired, clean up the membership set
                self._r.srem(K_WORKERS_ALL, wid)
                continue
            out.append(
                WorkerInfo(
                    worker_id=h["worker_id"],
                    hostname=h["hostname"],
                    pid=int(h["pid"]),
                    started_at=_parse(h["started_at"]),
                    last_heartbeat_at=_parse(h["last_heartbeat_at"]),
                    current_job_id=h.get("current_job_id") or None,
                )
            )
        return out

    def list_alive(self, max_age: timedelta) -> list[WorkerInfo]:
        cutoff = _utcnow() - max_age
        return [w for w in self._list_workers() if w.last_heartbeat_at >= cutoff]

    def list_dead(self, max_age: timedelta) -> list[WorkerInfo]:
        cutoff = _utcnow() - max_age
        return [w for w in self._list_workers() if w.last_heartbeat_at < cutoff]

    def acquire(
        self,
        owner_id: str,
        lease_duration: timedelta,
    ) -> bool:
        ttl = int(lease_duration.total_seconds())
        if ttl <= 0:
            # Immediate-expiry semantics: drop any existing lock and
            # do not create a new one. Returns True so callers see the
            # acquire as nominally successful, mirroring memory/sqlite.
            current = self._r.get(K_COORD_LOCK)
            if current == owner_id or current is None:
                self._r.delete(K_COORD_LOCK)
                return True
            return False
        # Try to set if not exists, with TTL.
        if self._r.set(K_COORD_LOCK, owner_id, nx=True, ex=ttl):
            return True
        # Already held - is it us?
        current = self._r.get(K_COORD_LOCK)
        if current == owner_id:
            self._r.expire(K_COORD_LOCK, ttl)
            return True
        return False

    def renew(
        self, owner_id: str, lease_duration: timedelta
    ) -> bool:
        ttl = int(lease_duration.total_seconds())
        if ttl <= 0:
            # Immediate-expiry semantics: if we own it, drop it.
            current = self._r.get(K_COORD_LOCK)
            if current == owner_id:
                self._r.delete(K_COORD_LOCK)
                return True
            return False
        # Atomic compare-and-extend via Lua (small inline script)
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('EXPIRE', KEYS[1], ARGV[2])
        end
        return 0
        """
        result = self._r.eval(script, 1, K_COORD_LOCK, owner_id, ttl)
        return result == 1

    def release(self, owner_id: str) -> None:
        # Atomic compare-and-delete
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        self._r.eval(script, 1, K_COORD_LOCK, owner_id)

    def current_owner(self) -> str | None:
        return self._r.get(K_COORD_LOCK)

    def flush_all_yokai_keys(self) -> int:
        """Delete all yokai:* keys. Useful for tests and for resetting
        a queue cleanly. NEVER call this in production accidentally."""
        deleted = 0
        for key in self._r.scan_iter("yokai:*"):
            self._r.delete(key)
            deleted += 1
        return deleted
