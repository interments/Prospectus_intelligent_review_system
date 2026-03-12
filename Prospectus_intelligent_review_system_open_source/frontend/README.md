# Frontend（Vue 3 + TypeScript + Vite）

已重构为 Vue 3 + TypeScript 工程化前端，保留并增强以下能力：

- 上传 PDF 并发起后端任务
- 实时轮询任务状态
- 读取历史结果
- 披露卡片支持“前事件 / 后事件”双页跳转
- 方案A初步高亮：通过 PDF hash `search` 参数尝试关键词高亮

---

## 启动方式

### 1) 启动后端（9000）

```bash
cd /home/node/.openclaw/workspace/Prospectus_intelligent_review_system/backend
../.venv/bin/python -m pip install -r requirements.txt
../.venv/bin/python -m app.server
```

### 2) 启动前端（5173）

```bash
cd /home/node/.openclaw/workspace/Prospectus_intelligent_review_system/frontend
npm install
npm run dev
```

浏览器打开：

```text
http://localhost:5173
```

---

## 生产构建

```bash
cd /home/node/.openclaw/workspace/Prospectus_intelligent_review_system/frontend
npm run build
npm run preview
```

---

## 目录结构（核心）

```text
frontend/
  ├─ src/
  │  ├─ App.vue
  │  ├─ main.ts
  │  └─ style.css
  ├─ index.html
  ├─ package.json
  ├─ tsconfig.json
  └─ vite.config.ts
```
