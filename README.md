# 招股书智能审核系统（Open Source Edition）

一个面向 IPO 招股书的智能审核系统，聚焦三类出资/披露核查场景：

1. **价格波动披露**（price_fluctuation）
2. **持股 5% 以上股东披露完整性**（shareholder_5pct）
3. **股份质押冻结声明**（pledge_freeze_decl）

---

## 功能概览

- PDF 上传与在线预览
- 多模块选择执行（可串行 / 并行）
- 任务队列 + 运行进度 + 历史结果
- 一键终止运行中任务
- 结果页码联动定位
- 夜间模式切换

---

## 技术栈

- Frontend: **Vue 3 + TypeScript + Vite**
- Backend: **Flask**
- 文档抽取: `pdfplumber`
- 数据模型: `pydantic`
- 可选队列: `redis`
- 可选 LLM 增强: OpenAI 兼容 API（通过环境变量配置）

---

## 目录结构

```text
backend/
  app/
    server.py                         # 任务编排/API
    modules/capital_defect/
      price_fluctuation/
      shareholder_5pct/
      pledge_freeze/
  requirements.txt

frontend/
  src/

docs/
  full_technical_manual.md
  price_fluctuation_module_tech_report.md
  shareholder_5pct_module_tech_report.md
  pledge_freeze_part1_tech_report.md
```

---

## 快速开始

### 1) 安装后端依赖

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2) 配置环境变量

在项目根目录或 `backend/` 目录创建 `.env`（按需）：

```env
# 可选：LLM（若不配则会走规则兜底，部分功能效果会下降）
ARK_API_KEY=
ARK_BASE_URL=
ARK_MODEL=

# 可选：任务队列
REDIS_URL=
REDIS_QUEUE_KEY=prospectus:task_queue

# 默认执行模式（true 并行 / false 串行）
MODULES_PARALLEL=true
```

### 3) 启动后端

```bash
cd backend
python -m app.server
```

默认地址：`http://localhost:9000`

### 4) 启动前端

```bash
cd frontend
npm install
npm run dev
```

默认地址：`http://localhost:8432`

---

## API 简表

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

## 当前能力边界

- 不同券商/项目的标题风格差异较大，章节定位规则需持续迭代
- 极复杂跨页表格仍可能有解析噪声
- 质押冻结“已解除/未解除”语义判断目前以提示词+规则为主

---

## 开源建议

发布前建议你补齐：

- `LICENSE`（推荐 MIT 或 Apache-2.0）
- `.env.example`
- `.gitignore`（排除 `backend/artifacts/`, `backend/uploads/`, `node_modules/`, `.venv/`）
- `docs/benchmark_cases.md`（放 2~3 个回归样本）

---

## 致谢

感谢所有用于测试与反馈的公开招股书样本与贡献者。