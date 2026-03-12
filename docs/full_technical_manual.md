# 招股书智能审核系统技术手册（当前版本）

> 范围：当前代码基线（Flask 后端 + Vue3/TS 前端），含价格波动、5%股东、质押冻结声明三模块。

## 1. 架构总览

- 后端：Flask（`backend/app/server.py`）
- 前端：Vue3 + TypeScript + Vite（`frontend/src`）
- 模块：
  - `price_fluctuation`（价格波动）
  - `shareholder_5pct`（持股5%以上股东披露）
  - `pledge_freeze_decl`（质押冻结声明）

系统支持：
- 上传 PDF
- 选择模块批量执行
- 串行/并行模式切换
- 历史任务按 task 级聚合查看
- 结果双卡片展示与页码联动

---

## 2. 任务编排与队列

### 2.1 任务模型
每个任务（`task_xxx`）可包含多个模块子任务：
- `task_xxx/price_fluctuation/result.json`
- `task_xxx/shareholder_5pct/result.json`
- `task_xxx/pledge_freeze_decl/result.json`

### 2.2 执行模式
- 串行/并行可配置
- 配置来源：
  - 环境变量默认：`MODULES_PARALLEL`
  - 运行态接口：`GET/POST /api/v1/runtime`
  - 前端可点击“串行/并行”切换

### 2.3 Redis 队列
- 启用条件：`REDIS_URL` 配置且 Python `redis` 包可用
- 否则回退内存队列
- 健康检查：`/api/v1/health` 返回 `redis_enabled`

`.env` 示例：
```env
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_QUEUE_KEY=prospectus:task_queue
MODULES_PARALLEL=false
```

---

## 3. 共享预处理优化（关键）

为避免模块重复 PDF 解析，现已实现任务级共享预处理：

- 共享文件：`task_xxx/preprocessed_shared.json`
- server 在模块执行前仅生成一次
- 两模块通过 `--preprocessed` 复用

收益：
- 降低内存峰值
- 降低并行时 `MemoryError` 风险
- 加快多模块任务总耗时

---

## 4. 模块说明

### 4.1 价格波动模块
路径：`modules/capital_defect/price_fluctuation`

规则：
- 相邻事件，时间差 `<6个月`：阈值 5%
- 相邻事件，时间差 `>=6个月`：阈值 15%
- 告警输出含前后页码与文本（双点位跳转）

### 4.2 持股5%以上股东披露模块
路径：`modules/capital_defect/shareholder_5pct`

逻辑：
- `disclosed_list`（披露集合）
- `expected_list`（应披露集合）
- 差集形成 `alerts/issues`
- 告警文案：`xxx 持股比例 X% 存在股东未披露问题`
- 单页溯源 `page`

### 4.3 质押冻结声明模块
路径：`modules/capital_defect/pledge_freeze`

逻辑（第(一)部分）：
- 先定位父章节：`第X节 发行人/公司基本情况`
- 再定位子标题：5%股东相关段 + 董监高/核心技术人员相关段
- S5/MG 均按 chunk 做 yes/no 召回
- 仅对 yes 命中 chunk 抽取“质押/冻结事件表”
- 输出 `summary + alerts + events`，并给出 chunk 命中统计

---

## 5. API 一览

- `GET /api/v1/health`
- `GET /api/v1/runtime`
- `POST /api/v1/runtime`
- `POST /api/v1/tasks`
- `POST /api/v1/tasks/<task_id>/rerun`
- `POST /api/v1/tasks/<task_id>/cancel`
- `GET /api/v1/tasks`
- `GET /api/v1/tasks/<task_id>`
- `GET /api/v1/results`
- `GET /api/v1/result?path=...`
- `GET /api/v1/file?path=...`

---

## 6. 前端使用说明

1. 上传或拖拽 PDF
2. 选择业务模块（卡片开关）
3. 选择执行模式（串行/并行）
4. 点击“运行已选模块”
5. 右侧查看：
   - 任务队列（固定高度滚动）
   - 价格波动结果卡片（可展开）
   - 5%股东结果卡片（可展开）

历史记录：
- 支持 task 级选择（一次加载同 task 的多个模块结果）
- 支持自动刷新 + 手动“刷新历史”

---

## 7. 常见问题

### Q1: `redis_enabled=false`
- 检查是否安装 `redis` Python 包
- 检查后端是否重启
- 检查 `REDIS_URL` 是否写在正确 `.env`

### Q2: 模块失败并提示查看 log
- 查看 `tasks` 返回中的模块 `log`
- 常见于大 PDF 并行解析内存不足
- 建议切换串行或启用共享预处理（已默认接入）

### Q3: 历史结果不刷新
- 使用“刷新历史”按钮
- 确认后端 `/results` 可见目标 task 的模块 result.json

---

## 8. 文档索引

- 价格波动模块：`docs/price_fluctuation_module_tech_report.md`
- 5%股东模块：`docs/shareholder_5pct_module_tech_report.md`
- 质押冻结模块（第(一)部分）：`docs/pledge_freeze_part1_tech_report.md`
