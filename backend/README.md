# Backend Skeleton

Python backend scaffold for the Prospectus Intelligent Review System.

Current focus:
- `app/modules/capital_defect/price_fluctuation/`

## Quick start (local)

```bash
cd backend
pip install -r requirements.txt
export ARK_API_KEY=xxx
python -m app.modules.capital_defect.price_fluctuation.pipeline.run_price_fluctuation \
  --pdf ../Datasets/000005_20190322_CQZK.pdf \
  --workdir ./artifacts/000005
```

Artifacts:

- `preprocessed.json`：逐页文本与表格抽取结果
- `chunks.json`：章节定位后的事件分段
- `result.json`：时间序列 + 价格波动告警

Queue persistence (new):

- 默认仍可用内存队列
- 若配置 `REDIS_URL=redis://localhost:6379/0`，自动启用 Redis 持久化队列
- 任务元数据会持久化到 `backend/artifacts/tasks.json`
- 重启后会自动把 `queued/running` 任务恢复为 `queued` 并重新入队

Notes:

- 未配置 API Key 时，自动启用启发式抽取（便于离线打通流程）
- 配置 `ARK_API_KEY` 或 `OPENAI_API_KEY` 后，启用 LLM 抽取
