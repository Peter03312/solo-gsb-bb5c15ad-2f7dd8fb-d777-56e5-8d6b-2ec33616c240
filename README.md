# 设备停机归因服务（FastAPI + PostgreSQL）

按**班次**对设备停机事件做分钟级归因的 HTTP 服务：事件带时区、精确到整分钟，
事件与班次均按**半开区间 `[start, end)`** 处理；同一分钟被多个事件覆盖时
**保养 > 故障 > 待判**，每分钟只归因一次。支持事件幂等上报、连续版本校验、
迟到更正自动重算已结算班次，并完整保留历史修订与差额。

---

## 1. 一键启动（Docker Compose）

前置：安装 Docker 与 Docker Compose Plugin。

```bash
# 可选：复制并修改端口/数据库账号
cp .env.example .env

# 构建镜像、安装依赖、启动 PostgreSQL 并由应用自动建库建表
docker compose up --build -d

# 查看状态与日志
docker compose ps
docker compose logs -f api
```

启动后：

| 用途 | 地址 |
| --- | --- |
| API 基地址 | `http://localhost:8080` |
| 健康检查 | `GET http://localhost:8080/healthz` |
| 交互式文档（Swagger UI） | `http://localhost:8080/docs` |
| OpenAPI Schema | `http://localhost:8080/openapi.json` |

> 端口可通过 `.env` 中的 `API_PORT` 修改（容器内固定监听 8080）。

停止与清理：

```bash
docker compose down          # 停止（保留数据库卷）
docker compose down -v       # 停止并删除数据卷
```

### 不使用容器的本地启动

```bash
pip install -r requirements.txt
# 准备一个可连接的 PostgreSQL，然后
export DATABASE_URL="postgresql+psycopg2://downtime:downtime@127.0.0.1:5432/downtime"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

应用启动时会等待数据库就绪，自动执行 `CREATE EXTENSION btree_gist`、建表与
排他约束，**无需手工建库脚本**。

---

## 2. 配置项（环境变量 / `.env`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://downtime:downtime@127.0.0.1:5432/downtime` | SQLAlchemy 连接串；Compose 中指向 `db` 服务 |
| `HOST` | `0.0.0.0` | API 监听地址 |
| `PORT` | `8080` | API 监听端口 |
| `API_PORT` | `8080` | Compose 宿主机映射端口 |
| `POSTGRES_USER/PASSWORD/DB` | `downtime` | PostgreSQL 首次初始化账号与库名 |
| `DB_CONNECT_RETRIES` | `30` | 启动等待数据库的重试次数 |
| `DB_CONNECT_RETRY_INTERVAL` | `1.0` | 重试间隔秒数 |

---

## 3. 核心业务规则

### 3.1 时间与区间

* 所有时间必须携带时区偏移（如 `2026-10-02T08:00:00+08:00`），服务统一按 UTC 存储；
* 必须精确到**整分钟**（秒/微秒为 0），否则返回 `422`；
* 班次与事件都是半开区间 `[start, end)`：结束分钟本身**不计入**；
  例如 `[08:00, 09:00)` 恰为 60 分钟，`[09:00, 10:00)` 与其相切但不重叠；
* `end <= start` 属于非法区间，返回 `422`。

### 3.2 班次

* 同一设备的班次时间区间**不允许重叠**，由 PostgreSQL GiST 排他约束
  `EXCLUDE USING gist (device_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&)`
  在数据库层保证，违反时返回 `409 SHIFT_OVERLAP`。

### 3.3 停机事件与版本链

* 事件按 `source`（来源编号）独立维护一条从 **1** 开始的**连续递增**版本链；
* 同 `(source, version)` + **同内容**（设备、起止、原因的规范化哈希一致）重复上报：
  **幂等**返回，HTTP 200 且 `status="replayed"`，不会触发任何重算；
* 同 `(source, version)` 但**内容不同**：拒绝，`409 VERSION_CONTENT_MISMATCH`；
* 版本跳号（含来源首版不是 1）：拒绝，`409 VERSION_GAP`；
* 原因取值：`保养/maintenance`、`故障/fault`、`待判/pending`（中英文均可）。

### 3.4 归因

* 班次内每分钟独立归因，且**每分钟只归因一次**；
* 优先级：**保养(maintenance) > 故障(fault) > 待判(pending)**；
* 未被任何事件覆盖的分钟计入 `normal`；
* 输出结果包含各原因分钟数与 `basis`——每条实际贡献过分钟的事件的
  `source / version / 原因 / 贡献分钟数 / 贡献区间`，即“所依据的事件版本”。

### 3.5 结算、迟到更正与修订

* `settle` 对班次做首次结算，生成**修订号 1**（`kind=settlement`）并永久保留；
* 结算后到达的事件（迟到更正）若与某已结算班次的事件时间重叠，服务按事件时间
  **自动重算所有受影响的已结算班次**：
  * 结果有变化 → 生成下一号修订（`kind=correction`），携带
    `trigger_event`（触发事件的来源/版本）与 `delta`（相对上一修订的分钟差额，
    新值 − 旧值）；
  * 结果无变化（例如更正被高优先级事件完全遮蔽）→ 不产生空修订；
* 历史修订**永不被覆盖**；查询结果可用 `?revision=N` 指定任意修订；
* 未结算班次查询结果返回 `409 SHIFT_NOT_SETTLED`。

### 3.6 错误响应格式

所有业务错误统一为：

```json
{ "error": "ERROR_CODE", "message": "中文说明", "details": {} }
```

| HTTP | error | 触发场景 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 未知设备 / 班次 / 修订号 |
| 409 | `SHIFT_OVERLAP` | 同设备班次重叠 |
| 409 | `VERSION_GAP` | 版本跳号或首版非 1 |
| 409 | `VERSION_CONTENT_MISMATCH` | 同来源同版本但内容不同 |
| 409 | `SHIFT_NOT_SETTLED` | 班次尚未结算即查询结果 |
| 409 | `DEVICE_CODE_EXISTS` | 设备编号已存在但名称不同 |
| 422 | （请求体校验） | 时间无时区、未对齐整分钟、区间非法、原因非法等 |

---

## 4. HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/devices` | 登记设备（同编号同名幂等） |
| `POST` | `/api/devices/{device_code}/shifts` | 创建班次 |
| `GET` | `/api/devices/{device_code}/shifts` | 班次列表 |
| `POST` | `/api/devices/{device_code}/shifts/{shift_id}/settle` | 结算班次（修订号 1） |
| `GET` | `/api/devices/{device_code}/shifts/{shift_id}/result?revision=N` | 查询归因结果（默认最新修订） |
| `GET` | `/api/devices/{device_code}/shifts/{shift_id}/revisions` | 列出全部修订 |
| `POST` | `/api/devices/{device_code}/events` | 上报停机事件（幂等/校验/触发重算） |
| `GET` | `/api/devices/{device_code}/events` | 查询设备下的事件 |
| `GET` | `/healthz` | 健康检查 |

---

## 5. 调用示例（curl）

```bash
BASE=http://localhost:8080
DEV=CN-01-LINE-A

# 1) 登记设备
curl -sS -XPOST "$BASE/api/devices" -H 'Content-Type: application/json' \
  -d '{"code":"CN-01-LINE-A","name":"一号产线A"}'

# 2) 创建班次 08:00-12:00（+08:00，整分钟，半开区间）
curl -sS -XPOST "$BASE/api/devices/$DEV/shifts" -H 'Content-Type: application/json' \
  -d '{"start":"2026-10-02T08:00:00+08:00","end":"2026-10-02T12:00:00+08:00"}'
# 返回中的 id 记为 SHIFT_ID

# 3) 上报事件（来源 mes-cn01 的 v1：待判 09:00-11:00）
curl -sS -XPOST "$BASE/api/devices/$DEV/events" -H 'Content-Type: application/json' \
  -d '{"source":"mes-cn01","version":1,
       "start":"2026-10-02T09:00:00+08:00","end":"2026-10-02T11:00:00+08:00",
       "reason":"待判"}'

# 4) 结算 -> 修订号 1
curl -sS -XPOST "$BASE/api/devices/$DEV/shifts/$SHIFT_ID/settle"

# 5) 迟到更正：同来源 v2，保养 10:00-10:30（保养优先于待判）
#    响应 new_revisions[] 中直接给出修订号 2 与 delta
curl -sS -XPOST "$BASE/api/devices/$DEV/events" -H 'Content-Type: application/json' \
  -d '{"source":"mes-cn01","version":2,
       "start":"2026-10-02T10:00:00+08:00","end":"2026-10-02T10:30:00+08:00",
       "reason":"保养"}'

# 6) 查询最新修订 / 指定历史修订 / 全部修订
curl -sS "$BASE/api/devices/$DEV/shifts/$SHIFT_ID/result"
curl -sS "$BASE/api/devices/$DEV/shifts/$SHIFT_ID/result?revision=1"
curl -sS "$BASE/api/devices/$DEV/shifts/$SHIFT_ID/revisions"
```

### 归因结果示例（迟到更正后的最新修订）

```json
{
  "revision_id": 2,
  "revision_no": 2,
  "kind": "correction",
  "minutes": { "maintenance": 30, "fault": 0, "pending": 90, "normal": 120 },
  "total_minutes": 240,
  "basis": [
    { "source": "mes-cn01", "version": 2, "reason": "maintenance", "minutes": 30,
      "segments": [ {"start":"2026-10-02T02:00:00+00:00",
                     "end":"2026-10-02T02:30:00+00:00","minutes":30} ] },
    { "source": "mes-cn01", "version": 1, "reason": "pending", "minutes": 90,
      "segments": [ {"start":"2026-10-02T01:00:00+00:00",
                     "end":"2026-10-02T02:00:00+00:00","minutes":60},
                    {"start":"2026-10-02T02:30:00+00:00",
                     "end":"2026-10-02T03:00:00+00:00","minutes":30} ] }
  ],
  "trigger_event": { "source": "mes-cn01", "version": 2 },
  "delta": { "maintenance": 30, "fault": 0, "pending": -30, "normal": 0 }
}
```

> 响应中的时间统一输出为 UTC（`+00:00`）；`basis.segments` 表示该事件实际
> 赢得的分钟区间，分钟总数与 `minutes` 严格一致。

也可以直接运行端到端示例脚本（服务启动后）：

```bash
python examples/walkthrough.py
```

---

## 6. 本地测试

测试不依赖 PostgreSQL（归因算法为纯函数；服务层/HTTP 层用 SQLite 替身）：

```bash
python -m unittest discover -s tests -v
# test_attribution      —— 半开区间、整分钟校验、优先级、时区归一、确定性
# test_services_sqlite  —— 版本链、幂等、结算、迟到更正重算与差额
# test_api_sqlite       —— HTTP 状态码、404/409/422 错误格式
```

---

## 7. 项目结构

```
app/
  config.py          # 环境配置（pydantic-settings）
  db.py              # SQLAlchemy 表结构、btree_gist + 班次排他约束、建库
  attribution.py     # 归因核心纯函数（半开区间/优先级/每分钟一次）
  schemas.py         # 入参校验（时区、整分钟、原因别名）
  services.py        # 版本链/幂等/结算/修订重算/差额
  errors.py          # 统一业务错误
  main.py            # FastAPI 路由与启动建库
examples/walkthrough.py
tests/
Dockerfile  docker-compose.yml  entrypoint.sh  requirements.txt
```
