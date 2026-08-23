# SmartCrusher 流程图 · 节点详细说明

本文件是 `compressor-smart-crusher.drawio` 的配套说明。流程图节点框保持
简洁，关键阈值/机制放在节点旁的灰色"文字说明"框里（图例中对应"文字说明
（灰色备注）"）；本文件集中放完整阈值、参数、JSON 形态、失败模式等细节，
避免节点框承载过多信息。

当前流程图只展示**核心压缩流程**（入口 → 有损/无损分流 → 压缩结果）。
audit_safe 保护、TOIN 学习、CCR 镜像、输出 `CrushResult` 等尾部环节不在
图中，但完整细节见本文档对应小节。

阅读建议：先看图理解控制流，遇到具体阈值/约束再回查本文件对应小节。

---

## 入口

`SmartCrusher.crush(content, query, bias)` — public API。`query` 用于行采样
时的相关性加权；`bias` 用于自适应调整要保留的总行数（`compute_optimal_k`）。

---

## 节点 1 · 规范化拼接 JSON (`id=3`)

**函数**：`headroom.transforms.content_detector.normalize_concatenated_json`

**做什么**：把 web search 类工具返回的"空白分隔 JSON 对象序列"改写为标准
JSON 数组。例如：

```text
{"title":"a","url":"..."} {"title":"b","url":"..."} {"title":"c","url":"..."}
```

→

```json
[{"title":"a","url":"..."},{"title":"b","url":"..."},{"title":"c","url":"..."}]
```

**触发条件**（必须全部满足才返回数组）：
- `content.strip()` 首字符是 `{`
- 用 `JSONDecoder.raw_decode` 在任意偏移处反复解出 ≥2 个 JSON 值
- 所有解出的值都是 `dict`（不混数组/标量）

**为什么必要**：Rust 核心 `crush_array_json` 只压缩真数组；非数组形态会
0% 压缩 passthrough，浪费 token（issue #1741）。

**不满足时**：返回 `None`，`crush()` 不覆盖 `content`，继续走原 JSON 检测
路径。

---

## 节点 2 · 是 JSON 数组？ (`id=4`)

`json.loads` 解析成功，且顶层是 `list`。

**否** → 直接走「原样返回 Passthrough」(`id=5`)。

---

## 节点 3 · 达到压缩门限？ (`id=6`)

**阈值**（Rust 端硬编码）：
- `items >= 5`
- `tokens >= 200`

任一不满足 → 「原样返回 Passthrough」。门限设计是为了避免对微小内容引入
CCR 标记/格式变换的副作用，性价比太低。

---

## 节点 4 · 无损压缩尝试（compactor） (`id=7`)

无损压缩分支的核心步骤（Rust `compactor::compact_inner`），形态分析 + 渲染
一体完成，不是两个独立步骤。

**同构 / 异构判定**（替代早期设计的 variance/uniqueness/similarity 指标）：
- 字段出现在 ≥80% 行 → 判为「核心字段」（`core_field_fraction=0.8`）
- 核心字段数 / 总字段数 < 60% → 判为异构（`heterogeneous_core_ratio=0.6`）
- 异构时尝试找 discriminator（`detect_discriminator`）：字符串型、每行都有、
  去重取值数 ∈ [2, 8]、且 n/total ≤ 0.7（排除 ID 型字段）→ 找到则按
  discriminator 分桶 `Buckets`，找不到则退化为稀疏 `Table`
- 同构 → `Compaction::Table`（含嵌套均匀 flatten、字符串化 JSON 递归、
  opaque blob → CCR 指针）

**渲染格式**：`csv-schema`（默认）/ `json` / `markdown-kv`，构造时固定，
非自动挑选（由 `CompactionStage` 的 formatter 决定）。

> 注：早期设计的三指标 `variance ≤ 2.0 · uniqueness ≥ 0.1 · similarity ≥ 0.8`
> 是**有损 analyzer**（`SmartAnalyzer`）的统计量，不是无损同构/异构判定依据。
> `uniqueness_threshold`/`similarity_threshold` 在现有 Rust 路径实际未使用；
> `variance_threshold=2.0` 仅用于有损分支的变更点检测。

---

## 节点 5 · 行采样（有损降级） (`id=8`)

**只在有损降级分支发生**（`id=9` 判「否」后才进入），不是无损路径的前置步骤。

**做什么**：`SmartAnalyzer` 判模式（`time_series` / `logs` / `search_results`
/ `generic`）+ crushability；`SmartCrusherPlanner` 按策略算出 `keep_indices`
（保留哪些原始行，其余行丢弃、原文存入 CCR store 可恢复）。

**保留行来源**（策略相关，见 `planning.rs`）：
- 变更点窗口（time_series ±2 / smart_sample ±1）
- message 字段聚簇（cluster_sample，md5[:8] 每簇 ≤2 条）
- score 字段 top-N（search_results / top_n）
- query 锚点精确匹配 + 相关性打分（带 query 时）
- 结构离群 / 错误关键词 / 数值异常必留

**约束**：
- 上限 **15 行**（`max_items_after_crush=15`）
- 相邻去重（`dedup_identical_items=true`）

> 早期设计的「首部 30% + 尾部 15%」是旧 AnchorSelector 的说法；现实现按
> 位置权重分配锚点槽位（generic 前 0.5 / 后 0.4 / 中 0.1，search 前 0.75 /
> 后 0.15，logs 前 0.15 / 后 0.75），非固定比例。

---

## 节点 6 · 无损省 ≥ 15%？ (`id=9`)

**无损 / 有损的判分水岭**：比较无损压缩结果相对原文的节省比例与阈值
`lossless_min_savings_ratio`（默认 **0.15**）。

- **是（节省 ≥ 15%）** → 采纳无损结果 → `id=12` 压缩结果
- **否** → 降级有损分支 → `id=8` 行采样（有损降级）

> 无损尝试在 `id=7` 已执行完（`stage.run_with_store` → 渲染），此节点只决定
> 「省得够不够、值不值得采纳」。

---

## 节点 7 · 有损行丢弃 + CCR 哨兵 (`id=10`)

**做什么**：丢弃可丢弃的行（不在 `protected_patterns` 内的），并在结果末尾
追加一个特殊键记录丢弃信息。

**CCR 哨兵格式**：

```json
{"_ccr_dropped": "<<ccr:HASH N行>>"}
```

- `HASH` 指向 `CompressionStore` 中持久化的完整原始内容
- `N行` 是被丢弃的行数
- LLM 看到哨兵即可发起 CCR 检索恢复原文

---

## 节点 8 · 渲染格式（并入 `id=7`，原 `id=11` 已删）

渲染已并入「无损压缩尝试」节点；此处保留格式细节供查阅。

**三种输出格式**（由 `CompactionStage` 构造时固定一个 formatter，非自动选择）：
- `csv-schema` — 默认，同构数组首选，fold 重复键到 schema
- `json` — 保留嵌套结构，删除冗余空格
- `markdown-kv` — 异构 / 小数组回退方案

**采纳门槛**：
- 渲染完成后由 `id=9` 判定：输出比原文节省 ≥15%（`lossless_min_savings_ratio`）
  才采纳，否则降级有损分支（`id=8` / `id=10`）
- 核心字段（出现 ≥80% 行）与异构分桶 2~8 是 compactor 结构侧的启发，决定 IR
  形态（Table / Buckets），不是「是否采纳」的判定条件

---

## 节点 9 · 压缩结果 (`id=12`)

无损或有损路径产生的中间结果，准备进入 audit_safe 检查。

---

## 节点 10 · 启用 audit_safe 保护？ (`id=13`)

仅当 SmartCrusher 实例配置了 `audit_safe=True` 时启用（默认关闭，命中
受保护字段时自动开启）。

- **是** → `id=14` 保护行扫描 + 回填
- **否** → 直接 `id=15` TOIN 学习记录 + CCR 镜像

---

## 节点 11 · 保护行扫描 + 回填 (`id=14`)

**做什么**：扫描压缩结果，检查所有 `protected_patterns` 命中的关键行是否
仍然存在。

**失败模式（fail-closed）**：任何被保护的行在压缩后丢失或变形，直接丢弃
压缩结果，返回**原文**——宁可无效也不漏保护内容。

`protected_patterns` 由路由层根据内容类型/工具注入（如 API key、错误码、
prompt 注入标记等）。

---

## 节点 12 · TOIN 学习记录 + CCR 镜像 (`id=15`)

**两件事并行**：
1. **TOIN 学习系统**（`depD`）— 上报本次压缩决策（是否胜出、采用格式、
   节省字节数）用于路由层后续调参
2. **CCR 镜像到 Python store** — 把 Rust 端生成的 `cache_key`（MD5[:24]）
   与原文持久化到 Python `CompressionStore`，确保标记跨进程/跨请求可恢复

---

## 输出

`CrushResult(compressed, was_modified, strategy)`

- `compressed` — 最终文本
- `was_modified` — 是否实际做了压缩（False 即 passthrough）
- `strategy` — `"passthrough"` | `"csv_schema"` | `"json"` | `"markdown_kv"`
  | `"lossy_drop"` | 等等

---

## 颜色图例

| 颜色 | 含义 |
|------|------|
| 🟦 蓝 (#dae8fc) | 处理步骤（Python 端） |
| 🟨 黄 (#fff2cc) | 决策菱形 |
| 🟩 绿 (#d5e8d4) | 无损路径 / 入口 / 出口 |
| 🟥 红 (#f8cecc) | 有损行丢弃 + CCR 哨兵 |
| 🟧 橙 (#ffe6cc) | Passthrough / 保护回填 |
| 🟪 紫 (#e1d5e7) | 学习/持久化 |
| ⬜ 灰（虚线） | 外部依赖 |