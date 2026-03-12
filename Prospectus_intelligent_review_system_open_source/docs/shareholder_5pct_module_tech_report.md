# 出资瑕疵-持股5%以上股东披露模块技术报告（v0.1）

> 范围：`capital_defect/shareholder_5pct` 模块，侧重“如何实现”。

## 1. 目标

核查招股书中“持股5%以上股东信息披露”是否完整：
- `disclosed_list`：披露段落中提到的 5%+ 股东
- `expected_list`：股本结构表中应披露的 5%+ 股东
- `missing = expected - disclosed`

输出披露问题：
- `xxx 持股比例 X% 存在股东未披露问题`
- 必带页码 `page`（单页溯源）

---

## 2. 实现入口

`app/modules/capital_defect/shareholder_5pct/pipeline/run_shareholder_5pct.py`

命令：
`python -m app.modules.capital_defect.shareholder_5pct.pipeline.run_shareholder_5pct --pdf <pdf> --workdir <dir> [--preprocessed <shared_preprocessed.json>]`

---

## 3. 核心流程

1) 数据源准备
- 优先读取 `--preprocessed`（共享预处理）
- 否则使用 `PdfRouter(pdfplumber)` 抽取 `text_blocks/table_blocks`

2) 页面锚定（章节/子标题）
- 锚定“发行人股本情况/发行前股本结构”作为 `expected_pages`
- 锚定“持有发行人5%以上/持股5%以上股东”作为 `disclosed_pages`

3) 结构化提取
- 表格优先提取 `name + holding_pct`
- 支持双行表头合并识别（`比例/持股比例`）
- 噪声表排除（客户/供应商/关联方/董事监事职务等）
- 比例阈值过滤：`>= 4.9%`

4) 披露集合增强
- 对披露集合支持 LLM/规则融合归一（简称/全称）

5) 对账与告警
- 集合差集：`missing = expected - disclosed`
- 生成：
  - `issues[]`
  - `alerts[]`（前端直接消费）

---

## 4. 输出结构

`result.json` 包含：
- `summary`
  - `disclosed_count / expected_count / missing_count / status`
- `issues[]`
- `alerts[]`
  - `message,page,shareholder,holding_pct,source_section`
- `disclosed_list[]`
- `expected_list[]`
- `missing_shareholders[]`
- `evidence_pages`

前端行为：
- 5%结果卡片固定显示
- 可展开查看详情
- 点击“定位页码”跳转 PDF

---

## 5. 与系统编排集成

后端任务支持多模块：
- `price_fluctuation`
- `shareholder_5pct`

5%模块支持：
- 串行/并行任务运行
- 共享预处理输入（降低重复解析与内存峰值）

---

## 6. 当前已知边界

- 不同公司招股书标题风格差异较大，页面锚定仍需持续调参
- 公司型股东“穿透披露完整性”（合伙人/出资人+比例）仍可继续增强
- 极端复杂表格（跨页/跨列合并）仍可能有噪声

---

## 7. 后续建议

1. 增加“披露完整性分级”
- 漏披露 / 比例不一致 / 字段不完整

2. 增加规则解释输出
- 告警中标明命中逻辑与证据页来源

3. 固化样本回归
- 08 亿华通、09 利元亨、18 晶晨等基准样本自动回归
