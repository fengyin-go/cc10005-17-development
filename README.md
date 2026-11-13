# 分布式网页采集调度系统

面向多站点网页抽取、代理池调度、反爬对抗、增量采集、失败重试与采集结果校验的分布式采集调度系统。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 站点管理 | `site` | 采集站点 | 站点编号、域名地址、站点类型 |
| 抽取模板 | `template` | 网页模板 | 模板编号、入口地址、列表选择器 |
| 代理池管理 | `proxy` | 代理节点 | 节点编号、代理地址、代理协议 |
| 采集任务 | `spider` | 采集作业 | 作业编号、目标站点、抽取模板 |
| 调度策略 | `schedule` | 调度规则 | 规则编号、关联站点、调度表达式 |
| 反爬对抗 | `antibot` | 对抗策略 | 策略编号、验证码类型、识别服务 |
| 限速控制 | `ratelimit` | 限速配置 | 配置编号、目标站点、每秒请求上限 |
| 失败重试 | `retry` | 重试策略 | 策略编号、关联任务、最大重试次数 |
| 字段映射 | `extractor` | 映射规则 | 规则编号、源字段名称、目标字段名称 |
| 采集流水线 | `pipeline` | 流水线步骤 | 步骤编号、步骤名称、输入数据源 |
| 结果校验 | `validator` | 校验规则 | 规则编号、校验对象、校验类型 |
| 数据落盘 | `storage` | 存储配置 | 配置编号、存储类型、输出路径 |
| 采集监控 | `monitor` | 监控指标 | 指标编号、关联任务、指标名称 |
| 数据清洗 | `cleanse` | 清洗规则 | 规则编号、目标字段、空值替换 |
| 增量采集 | `delta` | 增量游标 | 游标编号、目标站点、增量字段 |
| 数据分发 | `export` | 分发通道 | 通道编号、输出数据集、目标系统 |
| RPA流程 | `rpaflow` | 自动化流程 | 流程编号、目标页面、操作序列 |
| RPA检查点 | `rpa_checkpoint` | 检查点续做台 | 步骤卡、网页证据、执行权、版本 |
| 登录凭证 | `auth` | 凭证配置 | 凭证编号、目标站点、认证方式 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA

## RPA检查点续做台

入口在「RPA流程 → 检查点续做」。核心围绕“动作已发出但副作用未知”组织排障：

- **步骤卡**：每个检查点拆成若干张卡，卡上带动作、等待条件、结果提取、回滚定义和当前状态。
- **证据核对区**：执行失败或等待条件超时后，卡进入 `待核对`，不自动重试。核对区展示
  页面快照、与本次 `request_id` 匹配的回执、页面审计末尾、等待结果与提取值。
- **三种结论**：`页面已接受` → 按结果提取补记并推进下一卡；`页面未接受` → 才允许重试该卡；
  `无法判明` → 流程停在当前卡，接口返回原因（重试可能重复提交、跳过会丢结果），
  在作出新结论前禁止推进。
- **中断与恢复**：未决卡会保留；`恢复续做` 从当前卡继续，不会从第一步重放，也不会越过未决卡。
- **登录凭证**：凭证在认证卡被消费（记为已失效并写回消费检查点/批次）；过期凭证阻止
  领取下一张认证卡；重新绑定只恢复可用性，不解除检查点里未决卡的副作用核对。
- **并行续做**：同一检查点只颁发一份执行权（`/lease`），其它执行者的动作在发出前即被拦下；
  失权执行者的迟到回执通过 `/late-receipts` 进入旧批次证据区，只留档、不推进步骤。
- **编辑流程**：`/edit` 生成新版本；在途卡（当前未完成的卡）继续沿用旧版的等待条件与回滚定义，
  后续卡按新版本执行。裁掉在途卡会被拒绝。
- **异常回滚**：回滚本身也是副作用动作。回滚若出现“已发出但无回执”，同样进入回滚证据核对；
  确认页面已接受后不再允许重试回滚，避免再次改变演示页（作废次数继续累加）。

本地演示页面（`demo-page-1` ~ `demo-page-4`）接受登录、填表、确认、作废等可观察动作，
每次确认都会生成新回执、每次作废都会增加作废次数；演示页接口在 `/api/rpa-checkpoint/pages/...`。

检查点相关接口：

```text
GET  /api/rpa-checkpoint                         续做台总览（计数、执行权、检查点）
GET  /api/rpa-checkpoint/{id}                    检查点详情（卡片/证据/版本/旧批次回执）
POST /api/rpa-checkpoint/{id}/lease              领取或释放执行权
POST /api/rpa-checkpoint/{id}/resume             中断后恢复
POST /api/rpa-checkpoint/{id}/execute            执行当前步骤卡
POST /api/rpa-checkpoint/{id}/cards/{seq}/review            证据核对结论
POST /api/rpa-checkpoint/{id}/cards/{seq}/rollback          执行异常回滚
POST /api/rpa-checkpoint/{id}/cards/{seq}/rollback-review   回滚证据核对
POST /api/rpa-checkpoint/{id}/edit               编辑流程形成新版本
POST /api/rpa-checkpoint/{id}/late-receipts      提交迟到回执到旧批次证据区
GET  /api/rpa-checkpoint/pages/{page_id}         读取本地演示页状态
POST /api/rpa-checkpoint/pages/{page_id}/actions 在演示页上执行可观察动作
```

### 测试

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/test_checkpoint.py -q
```
