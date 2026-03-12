# Prospectus Intelligent Review System - 发布级项目组织方案（v1）

## 1. 目标

将当前实验型实现重构为可发布项目：
- 一键启动（本地/服务器）
- 前后端分层清晰
- 任务队列、结果存储、日志、错误处理完整
- 可测试、可观测、可维护

## 2. 建议目录结构

```text
Prospectus_intelligent_review_system/
  apps/
    backend/
      app/
      tests/
      requirements.txt
      .env.example
      Dockerfile
    frontend/
      src/
      public/
      package.json
      Dockerfile
  services/
    worker/                 # 可拆分异步worker（后续）
  storage/
    uploads/
    artifacts/
    logs/
  docs/
    api_contract.md
    release_project_plan.md
  scripts/
    run_dev.ps1
    run_dev.sh
  docker-compose.yml
  README.md
```

## 3. 后端发布基线

- API 层：Flask/FastAPI（建议后续切 FastAPI）
- 任务层：
  - MVP：进程内队列
  - 发布版：Redis + RQ/Celery
- 存储层：
  - 上传文件（uploads）
  - 结果文件（artifacts）
  - 运行日志（logs）
- 配置层：统一 `.env`
- 错误模型：统一返回 `{code, message, detail}`
- OpenAPI 文档：自动生成

## 4. 前端发布基线

- 页面：
  - 上传/任务列表
  - 结果查看（PDF联动跳页）
  - 历史结果管理（查看/删除）
- 状态管理：任务轮询 + 失败重试提示
- 统一 API client
- 构建产物：`dist/`，支持 Nginx 部署

## 5. 质量与发布

- 单元测试：
  - 章节定位
  - 单位归一
  - 去噪逻辑
  - 波动判定
- 集成测试：上传->分析->结果展示链路
- CI：lint + test + build
- 版本策略：SemVer

## 6. 立即落地步骤（本周）

1) 整理项目目录（apps/backend + apps/frontend）
2) 固化 API 契约文档
3) 补充脚本：Windows/Linux 一键启动
4) 增加结果管理 API（list/get/delete）
5) 前端接“任务->结果”完整流程
6) 补一套最小测试用例
