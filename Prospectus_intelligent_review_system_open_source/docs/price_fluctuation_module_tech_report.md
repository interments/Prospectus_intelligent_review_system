# 出资瑕疵-价格波动模块技术报告（v0.3，当前实现）

> 范围：`capital_defect/price_fluctuation` 模块，基于当前代码与前后端联动状态更新。

## 1. 模块目标

识别招股书中“股权转让/增资”事件的相邻价格异常波动，输出可审阅、可定位页码的披露提示。

输出核心：
- `timeline`：标准化事件时间序列
- `alerts`：波动披露提示（含前后事件页码、文本）

---

## 2. 判定逻辑（业务规则）

对相邻两条事件（按时间排序）判定：
- 若时间间隔 `< 6` 个自然月：`abs((p2-p1)/p1) > 5%` 命中
- 若时间间隔 `>= 6` 个自然月：`abs((p2-p1)/p1) > 15%` 命中

说明：
- 使用绝对值变化率
- 同日多事件保留原顺序
- 输出带 `previous_event_page/current_event_page` 供前端双点位跳转

---

## 3. 技术流程

入口：
`app/modules/capital_defect/price_fluctuation/pipeline/run_price_fluctuation.py`

流程：
1. 读取预处理（优先）或执行 PDF 抽取
   - 新增 `--preprocessed` 参数，可复用共享预处理文件
2. 章节分段（DocumentSegmenter）
3. LLM + 规则抽取转让/增资事件
4. 单位归一、汇率换算、价格补算、去噪
5. 构建时间序列并执行波动判定
6. 输出 `result.json` 与调试产物

---

## 4. 数据与去噪

关键清洗规则（PriceCalcService）：
- 实体缺失行剔除：
  - 转让缺 `transferor/transferee`
  - 增资缺 `investor`
- 若无单价但可由 `amount/shares` 计算，则补算
- 过滤不可信单价（已加）：
  - `< 0.01` 或 `> 10000`
- 美元换算：
  - 支持 USD -> CNY
  - 汇率服务失败时固定汇率兜底（7.2）

---

## 5. 输出格式（当前）

`result.json`:
- `timeline[]`：
  - `time,event,transferor,transferee,unit_price_cny_per_share,page,source_event_id`
- `alerts[]`：
  - `message,page,previous_price,current_price,change_ratio`
  - `previous_event_id,current_event_id`
  - `previous_event_page,current_event_page`
  - `previous_event_text,current_event_text`

前端已接：
- 点击告警支持“前事件/后事件”双按钮跳页
- 初步关键词高亮（search hash）

---

## 6. 与系统编排的关系（最新）

后端 `app/server.py` 已支持多模块任务编排：
- 可与 `shareholder_5pct` 同任务执行
- 串行/并行可配置：
  - `.env` 默认：`MODULES_PARALLEL`
  - 运行态接口：`GET/POST /api/v1/runtime`
  - 前端可点击切换“串行/并行”

内存优化（新）：
- 同一任务内共享一次 PDF 预处理结果
- 共享文件：`task_xxx/preprocessed_shared.json`
- 价格波动模块通过 `--preprocessed` 复用，避免重复 `pdfplumber` 解析

---

## 7. 主要产物文件

`task_xxx/price_fluctuation/` 下：
- `result.json`
- `preprocessed.json`
- `chunks.json`
- `chunks_report_period.json`
- `transfer_events_raw.json`
- `increase_events_raw.json`
- `dropped_events.json`
- `logs/run.log`

---

## 8. 当前状态与后续

已完成：
- 业务规则落地
- 前端联动跳页
- 多模块编排接入
- 共享预处理复用

建议继续：
- 提升章节锚定稳健性（复杂目录样本）
- 告警解释增强（命中哪条阈值规则）
- 回归基准集固定化（持续评估误报/漏报）
