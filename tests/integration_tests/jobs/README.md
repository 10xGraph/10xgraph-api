# Jobs integration tests

Real-service tests for the jobs run stores and transports. They are marked
`integration`, so they skip unless `--integration` is passed and the service answers.

Services (compose project `tenxgraph-jobs-test`, high host ports only):

| Service | Host port |
|---|---|
| jobs-redis (redis 8) | 16379 |
| jobs-redis7 (redis 7) | 16380 |
| jobs-pg (postgres 17) | 15432 |
| jobs-rabbit (rabbitmq 4) | 15673 |

Start and stop:

```
docker compose -p tenxgraph-jobs-test -f tests/integration_tests/jobs/docker-compose.yml up -d --wait
.venv/bin/python -m pytest tests/integration_tests/jobs -q --integration --no-cov
docker compose -p tenxgraph-jobs-test -f tests/integration_tests/jobs/docker-compose.yml down -v
```

Environment variables (defaults match the compose file):

- `JOBS_TEST_REDIS_URL` default `redis://localhost:16379/1`
- `JOBS_TEST_REDIS7_URL` default `redis://localhost:16380/1`
- `JOBS_TEST_PG_DSN` default `postgresql://postgres:jobs_test_pw@localhost:15432/postgres`
- `JOBS_TEST_RABBIT_URL` default `amqp://guest:guest@localhost:15673/`

Isolation: each Redis test gets a unique key prefix and the logical DB is flushed before
and after; each Postgres test gets its own schema `jobs_t_<uuid>`, dropped afterwards;
SQLite tests get a temp file path.

The `fault` fixture (pause, kill and restart containers, signal worker processes) only
acts on containers of project `tenxgraph-jobs-test`. Never point these URLs at a shared
Redis (port 6379).
