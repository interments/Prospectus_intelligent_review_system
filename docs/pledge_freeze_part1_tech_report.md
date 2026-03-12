# 出资瑕疵-股份质押冻结声明模块技术文档（第(一)部分）

> 范围：`capital_defect/pledge_freeze` 模块中“(一)持股5%以上股东和董监高及核心技术人员股份质押冻结声明”能力。

## 1. 业务目标

校验招股书在重点章节是否完成“质押/冻结”相关披露，包括：
- 存在事件披露（有质押/冻结）
- 不存在事件声明（明确无质押/冻结）

本部分产出结论分两层：
1) **是否有披露**（披露完整性）
2) **若有披露，是否提取到未解除的事件**（风险提示）

---

## 2. 代码入口与执行方式

入口：
- `backend/app/modules/capital_defect/pledge_freeze/pipeline/run_pledge_freeze.py`

命令：
```bash
python -m app.modules.capital_defect.pledge_freeze.pipeline.run_pledge_freeze \
  --pdf <pdf> \
  --workdir <workdir> \
  [--preprocessed <task_xxx/preprocessed_shared.json>] \
  [--chunk-size 1800] \
  [--chunk-overlap 200]
```

---

## 3. 当前实现流程（第(一)部分）

### Step A. 章节定位（父章节优先）

先定位父章节：
- `第X节 发行人基本情况`
- `第X节 公司基本情况`

再在父章节范围内找两个子标题：
- S5：`五/六、持有发行人5%以上...` 或 `持股5%以上...主要股东及实际控制人...`
- MG：`七/八、董事、监事、高级管理人员与/及核心技术人员(的简要情况)`

若父章节内未命中，回退全文检索兜底。

产物：
- `located_sections.json`
  - `s5_text/s5_pages`
  - `mg_text/mg_pages`

### Step B. 分块判定（Yes/No）

对 S5 与 MG 文本均做分块：
- `chunk_size=1800`
- `chunk_overlap=200`

每个 chunk 调 `_ask_yes_no()`：
- 判定该 chunk 是否含“股份质押/股份冻结相关披露”（含“无此类事项”的声明）

统计：
- `s5_disclosed = any(s5_flags)`
- `mg_disclosed = any(mg_flags)`

### Step C. 命中块抽取事件

仅对 yes 命中的 chunk 调 `_ask_extract_table()`，要求模型输出：
`|序号|人员名称|人员类型|事件情况|`

本地解析 `_parse_markdown_table()` 并过滤：
- 人员类型限定：`董事/监事/高级管理人员/实际控制人`
- 事件限定：`股份质押/股份冻结`
- 去重键：`(name, person_type, event_type)`

### Step D. 结果组织

- 若 `s5_disclosed=False 且 mg_disclosed=False`：
  - `summary.status=fail`
  - reason=`未在目标章节检测到质押冻结相关披露`
- 若检测到披露但未提取到事件：
  - `summary.status=pass`
- 若提取到事件：
  - `summary.status=fail`
  - 逐条生成 alert：`某某存在股份质押/冻结且未检测到解除说明`

---

## 4. 输出说明（result.json）

`result.json.summary` 关键字段：
- `status`：`pass/fail`
- `disclosed_detected`
- `event_count`
- `s5_disclosed / mg_disclosed`
- `s5_chunk_count / s5_hit_chunks`
- `mg_chunk_count / mg_hit_chunks`

并输出：
- `alerts[]`
- `events[]`
- `events.csv`

---

## 5. 与任务编排集成

- 通过 `server.py` 以模块名 `pledge_freeze_decl` 编排执行
- 支持共享预处理复用（`--preprocessed`）
- 支持队列中止：任务取消后模块状态进入 `cancelled`

---

## 6. 已落地的关键改造（本轮）

1. S5 从“整段判定”改为“分块判定+分块抽取”
2. 父章节定位加入“公司基本情况”兼容
3. 子标题正则兼容不同编号与标题变体（五/六、七/八、简要情况后缀）
4. `summary` 增加 chunk 命中统计，便于回归评估

---

## 7. 边界与注意事项（第(一)部分）

- 模块对“已解除”主要依赖提示词语义，不是强规则时序核验
- 页码当前为章节起始页回填，非精确到 chunk 行级定位
- LLM 表格输出格式偏差会影响事件入库（已有本地解析过滤）

---

## 8. 建议的验收口径

最少验证三类样本：
1. 明确声明“无质押/冻结”
2. 有质押/冻结且未解除
3. 有历史质押/冻结但已解除

验收时同时查看：
- `located_sections.json`（是否命中目标章节）
- `summary` 的 chunk 统计（是否有召回）
- `events/alerts`（是否与原文一致）
