# Async coordinator/worker mode

Yokai supports two deployment modes:

1. **Monolithic** (`yokai run`): the legacy single-process mode. One
   process polls the tracker, runs agents, opens PRs. Simple but
   limited: a slow agent run blocks polling, and if the process
   crashes mid-job the work is lost.

2. **Async** (`yokai coordinator` + `yokai worker` + `yokai result-handler`):
   three-role split with a persistent queue (SQLite by default).
   Resilient to crashes, scalable to multiple workers, and lets you
   take a worker down for maintenance without losing in-flight jobs.

This document covers the async mode.

## Architecture

```
+----------------+    enqueue     +-----------------+
| Coordinator    | -------------> |                 |
| (polls Jira)   |                |  Job Queue      |
+----------------+                |  (SQLite/Redis) |
                                  |                 |
+----------------+    dequeue     |                 |
| Worker(s)      | <------------- |                 |
| (run agent)    |                |                 |
+----------------+    write       |                 |
       |          ------------->  |  Result Store   |
       |                          |                 |
+----------------+    read        |                 |
| ResultHandler  | <------------- |                 |
| (commit + PR + |                |                 |
|  Jira comment) |                +-----------------+
+----------------+
```

Three independent processes:

- **Coordinator**: polls `IssueTracker.search_pending_stories()` every N
  seconds, routes each story to a repo, enqueues a `Job` into the
  `JobQueue`. Uses a leader-election lock so only one coordinator
  actually polls (others stand by, ready to take over if the leader
  dies).

- **Worker**: dequeues one `Job` at a time, prepares a repo checkout
  on a fresh branch, runs the coding agent, writes a `JobResult`. Uses
  a visibility lease: if the worker dies, the job is reclaimed by the
  coordinator after the lease expires and goes back into the queue.

- **ResultHandler**: polls for completed `JobResult`s, commits the
  agent's changes, pushes the branch, opens the PR, comments on the
  Jira story, marks the job DONE.

## Configuration

Add a `queue:` section to your `config.yaml`. Run `yokai init` for an
annotated example.

```yaml
queue:
  backend: sqlite        # sqlite | memory
  db_path: ~/.yokai/queue.db
  coordinator:
    poll_interval_seconds: 30
    lease_duration_seconds: 90
    reclaim_interval_seconds: 60
    max_attempts_per_job: 3
  worker:
    poll_interval_seconds: 2
    agent_timeout_seconds: 1800
    lease_duration_seconds: 1800
    heartbeat_interval_seconds: 15
    retry_backoff_base_seconds: 5
    retry_backoff_cap_seconds: 300
  result_handler:
    poll_interval_seconds: 5
    batch_size: 10
```

If you omit the `queue:` section, only `yokai run` (legacy mode) is
available.

## Backends

### SQLite (default)

Single file, no external services. Good for single-host deployments
where coordinator + worker + result-handler all run on the same
machine. The file is locked via SQLite's WAL mode; multiple processes
on the same host coordinate correctly.

Limitations: not suitable for workers on multiple hosts (the file
is local). For that, use Redis.

### Redis

Multi-host capable. Coordinator on one machine, workers on multiple
machines, result-handler anywhere - all talking to a single Redis
instance.

Install the optional dependency:

```
pip install yokai-cli[redis]
```

Configure:

```yaml
queue:
  backend: redis
  redis_url: redis://localhost:6379/0
  # Or with auth:
  # redis_url: redis://:mypassword@redis.example.com:6379/0
  # Or via TLS:
  # redis_url: rediss://user:pass@redis.example.com:6380/0
```

The redis_url is registered as a secret with the logging filter, so
the password is redacted from log output.

#### Production Redis configuration

The default Redis config is fine for testing but unsafe for
production. At minimum:

1. **Enable AOF persistence**: in `redis.conf` set `appendonly yes`.
   Without AOF, in-flight jobs are lost on Redis restart.

2. **Set eviction policy to noeviction**: `maxmemory-policy noeviction`.
   The default `noeviction` is correct - if maxmemory is reached the
   queue stops accepting new jobs rather than silently losing them.

3. **Use a dedicated database number** (the `/0` in the URL): isolate
   yokai keys from other apps using the same Redis. All yokai keys
   are prefixed `yokai:*` for additional safety.

4. **Backups**: regular RDB snapshots (`save 900 1`) plus AOF.

5. **High availability**: use Redis Sentinel for automatic failover.
   The yokai backend uses standard redis-py, which handles Sentinel
   transparently if you connect via a sentinel-aware URL.

#### Cluster mode (NOT supported yet)

The current backend uses Lua scripts that assume all keys hash to
the same slot. Redis Cluster splits keys across nodes by hash slot,
which would break the scripts.

For Cluster, all yokai keys would need to share a hash tag (e.g.
`yokai:{shard1}:job:...`). Not implemented in this release. Use
single-node Redis or Sentinel-based replication instead.

#### Failure scenarios

- **Redis goes down**: Coordinator and Workers will start logging
  ConnectionError on every operation. They do not crash, they keep
  retrying. Once Redis comes back, work resumes from where it left
  off (jobs persist in Redis).
- **Network partition**: A worker that loses Redis connectivity
  cannot heartbeat or update job status. Its lease will expire on
  the Redis side, and another worker will reclaim its job.
- **Job in-flight when Redis dies without AOF**: lost. Enable AOF.

### In-memory

Loses all state on process restart and not shared between processes.
Useful only for tests and single-process experiments. Do not use in
production.

## Operating

### Start everything on one host

In three separate terminals (or via systemd / tmux / screen):

```
yokai coordinator -c config.yaml
yokai worker -c config.yaml
yokai result-handler -c config.yaml
```

Each process will run until killed (Ctrl-C or SIGTERM). On
SIGTERM/SIGINT, each component finishes its current task and exits
cleanly.

### Scale workers

Start more worker processes pointing at the same config. Each will
get a unique `worker_id` (hostname + random suffix) and will
compete fairly for jobs.

```
# terminal 1
yokai worker -c config.yaml
# terminal 2
yokai worker -c config.yaml
# ...
```

### Inspect queue state

```
yokai queue-status -c config.yaml
```

Shows job counts by status, live workers, and recent dead-lettered
jobs.

### Retry a failed job

```
yokai queue-retry -c config.yaml <job-id>
```

Re-enqueues a `DEAD_LETTERED` or `FAILED` job under a new job_id with
fresh attempts. Useful when the failure was due to a transient issue
(Bitbucket down, agent flake) and you want to retry.

### Recovery after a crash

If a worker crashes mid-job, the job stays in `PICKED_UP` /
`AGENT_RUNNING` with a lease. After `lease_duration_seconds` elapses,
the coordinator's periodic reclaim moves the job back to `QUEUED`. A
new worker then picks it up.

If the coordinator crashes, another standing-by coordinator takes
over after the leader-election lease expires. If no other coordinator
is running, just start one - the queue continues to be processable
by workers in the meantime (workers do not depend on the coordinator
being alive to make progress on already-queued jobs).

## State machine

```
PENDING -> QUEUED -> PICKED_UP -> AGENT_RUNNING -> AGENT_COMPLETED -> POSTPROCESSING -> DONE
                       |              |                                    |
                       v              v                                    v
                     QUEUED        AGENT_FAILED                          FAILED
                  (reclaimed)         |
                                      v
                                   QUEUED (retry)  or  DEAD_LETTERED (max attempts)
```

Terminal states (no further transitions):
- `DONE` - all good, PR opened, comments posted
- `FAILED` - postprocessing failed (commit/push/PR/comment), needs
  manual intervention
- `DEAD_LETTERED` - agent failed `max_attempts_per_job` times, needs
  manual intervention

## Tuning

- `coordinator.poll_interval_seconds`: how often Jira is polled. Lower
  = faster pickup, more API calls. 30s is sane.
- `coordinator.lease_duration_seconds` / `coordinator.reclaim_interval_seconds`:
  if a coordinator dies, how long until another takes over.
  Lease 90s + reclaim 60s = at most ~2.5 min downtime.
- `worker.lease_duration_seconds` and `worker.agent_timeout_seconds`
  should be the same (default 1800s = 30 min). The lease must outlive
  the longest expected agent run.
- `worker.retry_backoff_base_seconds` / `cap_seconds`: exponential
  backoff between retries. Default 5s -> 10 -> 20 -> 40 ... capped at
  300s. Prevents thundering herd if many jobs fail at once.

## Known limitations

- SQLite backend is single-host only. Multi-host requires Redis.
- Redis Cluster mode is not supported (Lua scripts assume all keys on
  one shard). Use single-node Redis or Sentinel.
- The `HostingTrackerPostprocessor` re-clones the repo to do the
  commit/push/PR. This works only if worker and result-handler share
  the same `workspace_dir` (typically same host or NFS-mounted dir).
  A future adapter will support fully decoupled deployments by
  re-cloning from the pushed branch.
- Crash safety has been tested via unit tests and clean shutdowns,
  but has not been validated against real `kill -9` scenarios on
  long-running production workloads.
- Redis backend is tested with `fakeredis` in CI. Behaviour against a
  real Redis instance should be validated in a staging environment
  before relying on it in production.
