#!/bin/sh
# 应用启动入口：应用自身会在 lifespan 中重试等待 PostgreSQL 就绪，
# 并幂等执行 CREATE EXTENSION btree_gist 与建表。
set -e

echo "Starting downtime-attribution API on ${HOST:-0.0.0.0}:${PORT:-8080}"
exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}"
