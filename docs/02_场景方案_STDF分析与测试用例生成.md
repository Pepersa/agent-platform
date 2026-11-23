# 中科飞测 STDF 数据分析与测试用例生成 —— AI Agent 技术方案与评测指标体系

> 版本：v1.0 ｜ 作者：AI Agent 方向新员工入职技术方案 ｜ 适用范围：半导体测试设备（Tester）后端数据分析 + 测试用例工程化
> 适用对象：研发/工艺/测试/质量工程师、产线 FA（失效分析）工程师、测试程序（TP）开发工程师、测试用例评审专家

---

## 0. 摘要（TL;DR）

本方案面向中科飞测（国产半导体测试设备厂商）两类最高频、最痛的 AI 落地场景，给出一套**共用同一套 Agent 底座**的落地路径：

| 场景 | 一句话定义 | 核心产出 | 6 个月目标 |
|---|---|---|---|
| **场景 A：STDF 测试数据分析 Agent** | 把 Tester 产出的 STDF 原始文件，变成"人话可问、可解释、可告警"的分析结论 | 问数回答、失败聚类归因、wafer map 缺陷根因、趋势告警解释、自动报告 | 分析耗时下降 ≥70%，Top1 归因命中率 ≥60% |
| **场景 B：测试用例自动生成 Agent** | 把规格书/Datasheet/历史用例/测试程序，变成结构化、可评审、可追溯的测试用例 | 结构化测试用例集 + 质量自检报告 + 覆盖的失效模式清单 | 用例编写工时下降 ≥50%，专家盲评通过率 ≥3.8/5 |

**共用底座（Core Platform）** 由五层组成，自下而上为：

1. **数据层**：STDF 解析 → 标准化（Parquet/列存）→ 数据湖（Iceberg/Hudi）+ 明细关系库（PostgreSQL）+ 时序库（TimescaleDB/InfluxDB）
2. **工具层（Tool Registry）**：统一的 MCP/Function-Calling 工具契约，含权限级别与 HITL 门禁
3. **检索层**：向量库（pgvector/Milvus）+ BM25 混合检索 + 结构化元数据过滤 + RRF 融合 + 重排（Reranker）
4. **推理层**：主控 Agent（Planner + Tool Router）+ 领域 Skill 子 Agent（NL2SQL / 归因 / wafer map / 用例生成 / 报告）+ 记忆与上下文管理
5. **评测与治理层**：四层指标体系（系统/检索/生成/业务）+ 回归评测流水线 + LLM-as-Judge + 审计日志 + 内网私有化部署

**关键工程原则**：
- **检索优先于生成**：数值问题默认走 SQL 语义层（Text-to-SQL → 校验 → 执行 → 数值直出），LLM 不做算术；
- **证据链强制**：任何结论必须能回溯到 STDF 记录 ID / SQL 结果 / 知识库条目 ID，不满足则拒答或降级为"仅假设"；
- **写操作全部 HITL**：Agent 默认只读，涉及改 TP、发通知、写报告归档需人工确认；
- **可离线评测**：所有能力必须落在可回归的评测集上，Prompt/模型/检索任一变更触发自动回归，低于阈值阻断发布。

---

## 1. 背景与领域知识

### 1.1 STDF 是什么

STDF（Standard Test Data Format，标准测试数据格式）是 SEMI 定义的半导体测试数据交换标准（现行版本 SEMI E1331-92 / E1332-97 等），由 ATE（Automated Test Equipment）设备在测试过程中输出。几乎所有主流 Tester（Advantest V93000/T5500、Teradyne UltraFLEX/J750、科休、中国大陆多家国产机台）都支持导出 STDF。

STDF 的核心价值：**它是测试机台与"数据智能"之间唯一稳定的、结构化的、跨厂商通用的接口**。中科飞测自研机台若能保证 STDF 输出质量与语义完整性（记录完整、字段规范、HDMR 正确），就等于把数据资产出口打通，后续所有 AI 能力都可以基于它构建。

**STDF 的物理结构**：一个 STDF 文件是**记录序列（record sequence）**的线性流，每条记录由 2 字节长度 + 记录类型 + 载荷组成（rec_typ 为 1 字节，长度含头尾）。记录分两大类：

- **信息记录（Informational Records）**：文件头 + 文件尾，描述整个文件的元信息；
- **结果记录（Result Records）**：描述一次测试过程的一个"逻辑单元"（如一个 site 的一个 part、一次测试程序执行、一次汇总）。

一次典型的测试执行（Test Execution）在 STDF 中体现为一条 **EXCR（EXEC Record）** 记录串，里面依次包含该次执行涉及的所有记录：

```
PIR (Program Information Record)      → 测试程序信息
  PRD (Program Description Record)     → 程序名、版本、执行时间
HDR (Header Record)                   → 运行头信息（客户、时间戳）
  HMR (Hardware Map Record)           → 硬件配置
  HBR (Hardware Bin Record)           → 硬件 bin 定义
  PMR (Pin Map Record)                → 引脚映射
  PLR (Pin List Record)               → 引脚列表
  PGR (Pin Group Record)              → 引脚组
  RDR (Retest Descriptor Record)      → 重测描述
  SDR (Sensor Descriptor Record)      → 传感器描述
  SDRR (Sensor Datarecord Record)     → 传感器数据
  PIR / MPR (Master Program Record)   → 主程序
MRR (Master Results Record)           → 总体结果汇总（最���出现）
PCRR (Part Count Results Record)      → Part / Bin 计数
TSR (Test Synopsis Record)            → 测试项汇总
  PFR (Pin Function Record)           → 引脚功能结果（Pass/Fail/状态）
  PIR/PTR (Test/Parametric Record)    → 参数测试结果（含上下限）
  PSR (Pin State Record)              → 引脚状态
TRR (Test Results Record)             → 单个测试项结果（含期望值、实测值）
FMR (Fail Map Record)                 → 失效位图（可选）
```

### 1.2 关键记录类型速览（工程视角）

| 记录 | 全称 | 关键字段 | 在 AI 场景中的用途 |
|---|---|---|---|
| `FDR` | File Header Record | 文本头，STDF 版本、厂商、创建时间 | 确认来源机台与版本，解析入口 |
| `PIR` | Program Information Record | 程序名、文件名 | 关联 TP（测试程序），知识库锚点 |
| `PRD` | Program Description Record | 程序版本、执行时间、lot/wafer 名、Tester SN、站点数、硬件配置 | 关联 `lot`/`wafer` 维度、批次趋势分析 |
| `HMR` | Hardware Map Record | 硬件 site → 硬件配置映射 | 定位 site 硬件差异（多 site fail 归因） |
| `PMR` | Pin Map Record | 引脚组 → DUT 引脚映射 | 多 site / 单 site 失效的引脚级分析 |
| `PCRR` | Part Count Results Record | Part 数、bin 分布、reject 原因 | 良率计算、bin 分布问题主输入 |
| `TRR` | Test Results Record | 测试项名、期望值、实测值、结果号、测试编号 | **失败模式聚类的最小单元**（哪个测试项失败） |
| `PFR` | Pin Function Record | 引脚功能 pass/fail、状态 | 引脚级失效（接触不良、驱动弱）分析 |
| `PTR` | Test Parametric Record | 参数实测值、上下限、单位 | 参数漂移、量纲一致性检查 |
| `TSR` | Test Synopsis Record | 测试项总数、通过/失败数 | 冗余测试项识别、测试成本分析 |
| `MRR` | Master Results Record | 总 part 数、总失效数、最终 bin、测试时间 | 文件级汇总，良率口径校验 |
| `HBR` | Hardware Bin Record | 硬件 bin 定义（失败即归入的 bin） | 硬件/程序错误区分依据 |
| `RDR` | Retest Descriptor Record | 重测标记 | 重测率分析（重测是否掩盖了初测失败） |
| `HDR` | Header Record | 运行时间戳、客户、测试条件 | 时间维度对齐 |

### 1.3 测试程序（TP）与 STDF 的关联

TP（Test Program，通常是 C/C++ 或图形化的测试流程）定义了 **测什么、怎么测、怎么判**。STDF 是 **TP 执行后产生的观测结果**。二者的关联链条：

```
TP 源文件 (TP_xxx.v / .tcl)
   └─> TP 版本号 / 测试项清单 / 限值 (limit table)
          └─> STDF PIR/PRD 里的程序名与版本字段  ◄── 关键 join key
                 └─> TRR/PTR 里的 test_num / test_name / 结果
```

**工程结论（直接决定 Agent 设计）**：
1. 必须建立 **TP 资产库**：每个 TP 版本对应一份"测试项清单 + 限值 + 预期失效模式"的结构化描述，作为 RAG 知识库主干；
2. STDF 中的 `PIR.PGM_NAME` + `PRD` 中的版本/时间戳，构成 **"这次数据是哪个 TP 版本产生的"** 的唯一可靠依据；
3. 归因分析必须先做 TP 版本对齐 —— 否则同一测试项名在不同 TP 版本语义不同，聚类会全错。

---

## 2. 总体架构（场景 A / B 共用底座）

### 2.1 架构全景

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          交互层 (Interaction)                           │
│  Web Chat │ 企业微信/钉钉机器人 │ IDE 插件(VSCode/JetBrains) │ CLI │ API │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│                     Agent 编排层 (Orchestration)                          │
│  ┌────────────┐  ┌─────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Planner    │  │ Router  │  │ Memory/Context│  │ Guardrails/HITL  │  │
│  │ 任务分解    │  │ 技能路由 │  │ 会话+短期记忆 │  │ 权限/审计/脱敏   │  │
│  └────────────┘  └─────────┘  └──────────────┘  └──────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │ Skill Agents: NL2SQL │ RCA(归因) │ WaferMap │ TrendAlert │      │   │
│  │                TestCaseGen │ ReportWriter │ KnowledgeQA         │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│                        工具层 (Tool Registry)                            │
│  MCP Server: stdf_parse │ sql_query │ wafer_map_query │ tp_lookup │      │
│              kb_search │ stat_test │ defect_detect │ doc_parse │        │
│              case_gen │ case_lint │ notify │ report_export              │
│  每个工具: JSON Schema 入参/出参 + 权限级别 + HITL 标记 + 超时/重试      │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│                        检索层 (Retrieval)                               │
│  混合检索: BM25 (Elasticsearch) ⊕ 向量 (pgvector/Milvus) ⊕ 元数据过滤     │
│  → RRF 融合 → Reranker (bge-reranker-v2-m3) → Top-k 证据包              │
│  知识源: TP库 | 规格书 | SOP/手册 | 故障知识库(FMEA) | 历史STDF摘要      │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│                          数据层 (Data)                                   │
│  Tester → STDF文件 → 解析服务 → 原始区(S3/MinIO)                        │
│        → 明细库(PostgreSQL) + 列存(Iceberg/Parquet) + 时序(TimescaleDB) │
│        → 指标层(Doris/ClickHouse) → 语义层(指标定义 + NL2SQL 目录)      │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│                     治理层 (Governance)                                  │
│  评测平台(回归流水线) │ LLM-as-Judge │ 审计日志 │ 脱敏 │ 私有化部署       │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 模型选型与理由

| 层级 | 选型建议 | 理由 |
|---|---|---|
| 主控 Agent / 复杂推理（归因、用例生成） | 内网部署 32B 级中文能力模型（Qwen3-32B / GLM-4-32B 系列）为主，云端强模型仅在脱敏后用于离线批量任务 | 归因与用例生成需要多步推理，14B 以下明显掉点；32B 是效果/成本拐点；全内网满足客户数据合规 |
| NL2SQL / 信息抽取 | 7B~14B 小模型 + Few-shot + Schema Linking | 该任务模型饱和度低，小模型可满足，准确率主要取决于 Schema 质量与候选值检索 |
| Reranker | bge-reranker-v2-m3（中文效果好、CPU 可跑） | 检索层收益最确定的一环 |
| Embedding | bge-m3（中英混合、8192 上下文、支持 dense+sparse） | 一套模型同时覆盖 BM25 稀疏与 dense，避免两套向量体系维护成本 |
| 多模态（wafer map） | 视觉主干（ConvNeXt / ViT）+ 缺陷图案模板库；必要时 VLM（Qwen-VL）做自然语言解释 | 缺陷图案识别用专用 CNN/模板匹配更准更便宜；VLM 只做"把已识别缺陷翻译成人话" |
| Judge 模型 | 强模型（≥70B 或云端旗舰）做主观评分，规则 + 小模型做客观校验 | LLM-as-Judge 本身有偏，需强模型 + 人工校准 |

**原则**：所有模型通过统一 LLM Gateway 调用（OpenAI 兼容接口），支持多模型路由、灰度切换、限流、Token 计量与成本核算 —— 这是成本类指标可被采集的前提。

---

## 3. 场景 A：STDF 测试数据分析 Agent

### 3.1 数据流转链路

```
Tester 产出 STDF
   │  (SFTP/HTTP 上传到 MinIO landing 区，命名规范：{lot}_{wafer}_{tp_ver}_{timestamp}.stdf)
   ▼
[STDF 解析服务]  semistd / py-stdf / 自研 (Rust 解析器 + Python 编排)
   │  产出：FDR/PIR/PRD/HDR/HMR/PMR/PCRR/TRR/PFR/PTR/TSR/MRR 结构化行
   ▼
[标准化 & 校验]  Schema 校验、记录完整度检查、HDMR 缺失告警、量纲归一
   ▼
[存储]  原始区 MinIO/S3 (Parquet)  +  明细 PostgreSQL  +  指标层 Doris  +  时序 TimescaleDB
   ▼
[清洗]  重测去重（按 RDR + part_id 判定）、无效 site 剔除、异常值标记
   ▼
[特征工程] wafer map 位图 / 失败测试项签名（signature）/ 空间自相关
   ▼
[分析层]  指标计算 → 归因 → 告警 → 报告
   ▼
[Agent 层]  NL 问数 / 聚类归因 / 空间根因 / 趋势解释 / 报告生成
```

### 3.2 STDF 解析技术选型

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| `semistd`（Python） | 生态好、面向对象记录模型清晰、支持 STDF4 | 纯 Python，大文件吞吐一般（~10-20 MB/s） | **推荐做默认**，易用性优先 |
| `py-pandas` / `pandas` + 自研 | 灵活 | 无标准记录模型，需自建映射 | 仅用于 PoC |
| `py-stdf`（Rust 绑定，KLayout 生态） | 快，接近 C 速度 | 生态小、定制成本高 | **推荐做高性能通道** |
| 自研 C++/Rust 解析器 | 最快、可内嵌机台 | 开发成本高（3-6 人月） | 吞吐 >5GB/天 时再上 |
| 商用 `STDF-X` 类工具 | 兼容性好 | 授权成本、黑盒 | 不采用 |

**推荐架构**：**双通道** —— 常规文件走 `semistd`（快速迭代），大文件/批量回灌走 Rust 通道；二者输出统一 Schema，Agent 层无感知。解析结果落 Parquet + PostgreSQL，**原始 STDF 文件永久保留**（可复现是数据平台的基本要求）。

**解析质量红线（必须在 Agent 上线前闭环）**：

| 检查项 | 阈值 | 说明 |
|---|---|---|
| HDR/HDMR 完整率 | ≥ 99.5% | 缺 HDMR 会导致时间/站点维度无法还原 |
| PCRR 与 MRR 计数一致性 | 100% | 不一致说明解析有 bug 或文件损坏 |
| TRR 的 test_num 与 TP 清单匹配率 | ≥ 98% | 决定后续 TP 对齐准确度 |
| 解析吞吐 | ≥ 50 MB/s（Rust 通道） | 满足 8h 产线节拍 |

### 3.3 数据建模（建议 DDL）

```sql
-- ============ 维度表 ============
CREATE TABLE dim_wafer (
    wafer_id        BIGINT PRIMARY KEY,
    lot_no          VARCHAR(64)  NOT NULL,     -- 批次号
    wafer_slot      SMALLINT    NOT NULL,     -- 片内序号
    product_code    VARCHAR(64)  NOT NULL,     -- 料号
    device          VARCHAR(64),               -- 芯片型号
    fab_site        VARCHAR(64),               -- 厂区/产线
    line_no         VARCHAR(32),               -- 产线，如 "3号线"
    step_code       VARCHAR(32),               -- 工艺步骤
    wafer_size      VARCHAR(16),               -- 6/8/12 inch
    die_rows        SMALLINT,
    die_cols        SMALLINT,
    probe_card_id   VARCHAR(64),
    tester_sn       VARCHAR(64),               -- 机台 SN
    product_id      BIGINT,                    -- 对应 dim_product
    UNIQUE (lot_no, wafer_slot, tester_sn)
);

CREATE TABLE dim_lot (
    lot_id      BIGINT PRIMARY KEY,
    lot_no      VARCHAR(64) NOT NULL UNIQUE,
    product_code VARCHAR(64) NOT NULL,
    customer_code VARCHAR(64),                 -- 客户（脱敏字段，映射 customer_alias）
    lot_start_ts TIMESTAMPTZ,
    lot_end_ts   TIMESTAMPTZ,
    wafer_count  SMALLINT,
    qty_total    INT,
    qty_pass     INT
);

CREATE TABLE dim_product (
    product_id   BIGINT PRIMARY KEY,
    product_code VARCHAR(64) UNIQUE NOT NULL,
    device       VARCHAR(64),
    tp_id        BIGINT,                       -- 当前主用测试程序
    spec_doc_id  BIGINT,                       -- 对应规格书
    spec_version VARCHAR(32)
);

CREATE TABLE dim_test_program (
    tp_id        BIGINT PRIMARY KEY,
    tp_name      VARCHAR(128) NOT NULL,
    tp_version   VARCHAR(64)  NOT NULL,
    tp_hash      CHAR(64)     NOT NULL,        -- 源码指纹，用于版本对齐
    item_count   INT,
    limit_table  JSONB,                       -- 测试项限值快照
    approved_at  TIMESTAMPTZ,
    UNIQUE (tp_name, tp_version)
);

CREATE TABLE dim_test_item (
    test_item_id BIGINT PRIMARY KEY,
    tp_id        BIGINT NOT NULL REFERENCES dim_test_program(tp_id),
    test_num     VARCHAR(32) NOT NULL,
    test_name    VARCHAR(128),
    test_type    VARCHAR(32),                  -- Functional/Parametric/DFT/Scan/...
    unit         VARCHAR(16),
    lower_limit  DOUBLE PRECISION,
    upper_limit  DOUBLE PRECISION,
    expect_value DOUBLE PRECISION,
    fail_action  VARCHAR(32),
    dft_method   VARCHAR(32)                  -- Scan/MBIST/ATPG/BoundaryScan/Timing
);

-- ============ 事实表 ============
CREATE TABLE fact_test_result (            -- 明细：每 part 每 test
    result_id    BIGSERIAL PRIMARY KEY,
    wafer_id     BIGINT NOT NULL REFERENCES dim_wafer(wafer_id),
    part_id      VARCHAR(64) NOT NULL,        -- STDF 内 part 标识
    site_id      SMALLINT NOT NULL,
    test_item_id BIGINT REFERENCES dim_test_item(test_item_id),
    test_num     VARCHAR(32),
    result       SMALLINT NOT NULL,           -- 0=pass 1=fail 2=invalid
    test_value   DOUBLE PRECISION,
    lower_limit  DOUBLE PRECISION,
    upper_limit  DOUBLE PRECISION,
    unit         VARCHAR(16),
    fail_reason  VARCHAR(256),
    is_retest    BOOLEAN DEFAULT FALSE,
    exec_id      BIGINT,
    ts           TIMESTAMPTZ NOT NULL,
    UNIQUE (exec_id, part_id, test_num, is_retest)
) PARTITION BY RANGE (ts);

CREATE TABLE fact_part (                   -- 每 part 汇总
    part_uid     BIGSERIAL PRIMARY KEY,
    wafer_id     BIGINT NOT NULL,
    exec_id      BIGINT NOT NULL,
    part_id      VARCHAR(64) NOT NULL,
    site_id      SMALLINT NOT NULL,
    final_bin    SMALLINT NOT NULL,
    first_fail_test_num VARCHAR(32),
    fail_signature CHAR(40),                 -- 失败测试项集合的哈希
    fail_count   SMALLINT,
    is_retest    BOOLEAN,
    test_time    TIMESTAMPTZ,
    UNIQUE (exec_id, part_id, is_retest)
);

CREATE TABLE fact_wafer_map (              -- wafer 空间分布（位图 + 缺陷特征）
    wafer_id     BIGINT PRIMARY KEY,
    bitmask      BYTEA NOT NULL,             -- 压缩位图
    rows SMALLINT, cols SMALLINT,
    defect_clusters JSONB,                   -- [{shape, cx, cy, area, ratio}]
    dominant_pattern VARCHAR(32),            -- EdgeRing/Center/Scratch/Random/Cluster
    pattern_score DOUBLE PRECISION
);

CREATE TABLE fact_exec (                   -- 每次执行（一个 STDF 文件）
    exec_id      BIGINT PRIMARY KEY,
    stdf_uri     TEXT NOT NULL,              -- MinIO 路径
    wafer_id     BIGINT REFERENCES dim_wafer(wafer_id),
    tp_id        BIGINT REFERENCES dim_test_program(tp_id),
    test_start   TIMESTAMPTZ, test_end TIMESTAMPTZ,
    site_count   SMALLINT,
    part_total   INT, part_pass INT, part_fail INT,
    bin_map      JSONB,                      -- {bin: count}
    header_info  JSONB,                      -- HDR 原始内容
    parse_status VARCHAR(16), parse_version VARCHAR(16)
);

CREATE TABLE fact_trend_daily (             -- 预聚合，供趋势/告警与 NL2SQL
    d            DATE NOT NULL,
    line_no      VARCHAR(32), product_code VARCHAR(64), tp_id BIGINT,
    wafer_id     BIGINT,
    total INT, pass INT, fail INT,
    yield_rate   DOUBLE PRECISION,
    bin_dist     JSONB,
    top_fail_items JSONB,
    retest_rate  DOUBLE PRECISION,
    PRIMARY KEY (d, line_no, product_code, tp_id)
);
```

**语义层**：在物理表之上再建一层 **指标定义表**（`metric_def`），把"良率""bin1 良率""多 site fail 率"定义为可版本化的 SQL 片段 + 业务口径说明，NL2SQL 生成 SQL 时优先复用这些片段，避免每次让 LLM 自由发挥口径。

```sql
CREATE TABLE metric_def (
    metric_code   VARCHAR(64) PRIMARY KEY,
    metric_name   VARCHAR(128) NOT NULL,
    business_def  TEXT NOT NULL,      -- 业务口径（喂给 LLM 的说明）
    sql_template  TEXT NOT NULL,      -- 含 {{start}} {{end}} {{filters}} 占位
    owner         VARCHAR(64),
    version       INT DEFAULT 1,
    tags          JSONB
);
-- 示例
INSERT INTO metric_def VALUES
('yield_bin1','Bin1 良率',
 '该批次所有 wafer 首次测试（不含重测）被判为 bin1 的 die 数 / 总测试 die 数',
 'SELECT 100.0*SUM(CASE WHEN p.final_bin=1 THEN 1 ELSE 0 END)/COUNT(*) FROM fact_part p JOIN dim_wafer w USING(wafer_id) WHERE p.is_retest=false AND {{filters}}',
 'quality','1', '["yield","core"]');
```

### 3.4 语义层：Text-to-SQL 流水线

```
用户提问
  ▼
① 问题归一化：抽取实体/时间/维度/指标（规则 + 小模型）
   "上个月 3 号线 A2 wafer 的 bin 1 良率是多少，比上周下降了多少"
   → metric=yield_bin1, dims=[line_no=3号线, wafer_slot=A2], time=[last_month], compare=[last_week]
  ▼
② Schema Linking（关键，决定 70% 准确率）
   - 表/列名向量检索 + 关键词匹配 → 召回 Top20 表列
   - 值检索：把 "A2" 匹配到 dim_wafer.wafer_slot（做 distinct 值字典 + embedding）
   - 只把 Top 10 表的精简 schema 送入 LLM（防超长 + 降幻觉）
  ▼
③ SQL 生成（few-shot + 语义层优先）
   - 强制：数值计算必须在 SQL 内完成，LLM 不做加减除
   - 强制：只读账号，禁 DDL/DML，多语句解析器拦截
  ▼
④ 静态校验 + 执行
   - SQL 解析（sqlglot）→ 语法/表列存在性/聚合合法性
   - 沙箱执行 + 行数/超时/扫描量限制（SELECT 5% 或分区裁剪）
   - 失败 → 错误信息回喂 LLM 修复，最多 2 次（修 1 次 + 换写法 1 次）
  ▼
⑤ 结果校验（Execution-based）
   - 空结果 → 追问澄清（"没有找到 3 号线的 A2 wafer，是想按 wafer 槽位还是产品型号？"）
   - 与上周对比 → SQL 内用窗口函数一次算出，避免二次往返
   ▼
⑥ 生成自然语言回答（附 SQL、口径说明、数据时间戳）
```

**工具：给 LLM 的表结构呈现方式**（选型：结构化 + 自然语言注释双通道）

```json
{
  "table": "fact_part",
  "comment": "每颗 die（part）一次测试的汇总结果",
  "columns": [
    {"name": "wafer_id", "type": "BIGINT", "comment": "晶圆外键", "fk": "dim_wafer.wafer_id"},
    {"name": "final_bin", "type": "SMALLINT", "comment": "最终 bin 号，1 通常为 good bin"},
    {"name": "is_retest", "type": "BOOLEAN", "comment": "是否重测，良率统计必须过滤为 false"},
    {"name": "fail_signature", "type": "CHAR(40)", "comment": "失败测试项集合哈希，相同即为同一失败模式"}
  ],
  "metrics": [{"code": "yield_bin1", "sql": "..."}]
}
```

**典型问数 SQL（目标输出）**：

```sql
WITH cur AS (
  SELECT 100.0 * SUM(CASE WHEN p.final_bin = 1 THEN 1 ELSE 0 END) / COUNT(*) AS y
  FROM fact_part p
  JOIN dim_wafer w ON w.wafer_id = p.wafer_id
  WHERE p.is_retest = FALSE
    AND w.line_no = '3号线' AND w.wafer_slot = 2
    AND p.ts >= '2026-10-01' AND p.ts < '2026-11-01'
), prev AS (
  SELECT 100.0 * SUM(CASE WHEN p.final_bin = 1 THEN 1 ELSE 0 END) / COUNT(*) AS y
  FROM fact_part p
  JOIN dim_wafer w ON w.wafer_id = p.wafer_id
  WHERE p.is_retest = FALSE
    AND w.line_no = '3号线' AND w.wafer_slot = 2
    AND p.ts >= '2026-09-01' AND p.ts < '2026-10-01'
)
SELECT cur.y AS this_month, prev.y AS last_week,
       cur.y - prev.y AS delta_pct,
       (cur.y - prev.y) / NULLIF(prev.y, 0) * 100 AS delta_ratio_pct
FROM cur, prev;
```

### 3.5 RAG 设计（TP / 手册 / SOP / 故障知识库）

| 知识源 | 内容 | 切分策略 | 元数据（必填，用于过滤） |
|---|---|---|---|
| 测试程序 TP 清单 | 测试项、限值、测试条件 | 按 **测试项** 切分（一个 test_num 一块） | tp_name, tp_version, test_num, product, dft_type |
| 规格书 / Datasheet | AC 特性、典型值、绝对最大额定值 | 按 **表格 + 章节标题** 切分（不跨表） | doc_id, product, doc_version, section |
| SOP / 设备手册 | 设备操作、校准、异常处理步骤 | 按 步骤条 切分 | doc_id, category, equipment_model |
| 故障知识库 FMEA | 历史故障：现象→可能根因→验证步骤→处置 | 按 **一个故障一个块** 切分 | fault_id, machine, symptom, root_cause, verified(是/否) |
| 历史 STDF 摘要 | 批次级结论与人工分析报告 | 按 批次/wafer 切分 | lot, wafer, date, analyst |
| 邮件/工单/8D 报告 | 客户反馈与正式纠正措施 | 按 报告切分 | ticket_id, customer_alias, date |

**检索流程**（务必混合检索，单纯向量在测试项短名上召回差）：

```
query → [查询改写：别名归一 + 中英扩展 + 型号归一] 
      → BM25 (ES)  ─┐
      → Dense (bge-m3, 余弦) ─┼→ RRF 融合 (k=60)
      → 元数据过滤 (product/tp_version/时间窗/权限) ─┘
      → Reranker (bge-reranker-v2-m3) → Top-8 证据包
      → 打包为 context（含 chunk_id + 出处：文件名/章节/行号）
```

**ground-truth 支持率**：回答中每个断言必须带 `[来源:chunk_id]` 引用，无引用的句子在生成后被后置校验器（规则 + NLI 模型）标记为"无支撑"，计入幻觉率。

### 3.6 多模态：Wafer Map 缺陷模式识别

**两段式架构**（不要让 VLM 直接判图）：

**第一段：专用视觉算法（可解释、快、准）**
1. 输入位图 → 解码为 `rows × cols` 矩阵（0=pass, 1=fail, 2=未测）；
2. 形态学去噪（开/闭运算，去除孤立点）；
3. 连通域分析 → 每个缺陷簇的 `(area, bbox, centroid, shape)`；
4. 归一化特征：面积占比、离心率、环状度、相对质心偏移、边缘距离、方向性（PCA 主轴）、是否跨过 wafer 边缘；
5. 与缺陷图案模板库匹配（规则 + LightGBM 分类器）：

| 图案类型 | 几何特征 | 典型根因假设 |
|---|---|---|
| `EdgeRing` | 环状、贴边、面积占比高 | 边缘工艺、薄膜均匀性、Chuck 温度不均 |
| `CenterCluster` | 中心密集团 | 曝光/涂胶中心缺陷、探针卡中心区磨损 |
| `Scratch` | 细长、强方向性、跨区域 | 划伤、搬运机械手/wafer 传输损伤 |
| `Random` | 离散点、无结构 | 单点工艺波动、碎屑、随机失效 |
| `Radial` | 放射状条纹 | 旋转机构、CMP 残留、spin 涂胶不均 |
| `PartialEdge` | 单边扇区 | 单侧探针接触、卡盘倾斜、上下料机械偏差 |

**第二段：VLM 解释层**
把已识别的图案类型 + 关键统计量（fail count、面积占比、涉及 bin、对比正常 wafer 的差异）+ 相关 SOP/故障库条目喂给 VLM，生成自然语言根因假设与建议验证步骤（"建议换用备用探针卡复测 wafer 3 第 2 次"）。
**注意**：VLM **不负责判定图案类型**，只负责表达；把判定责任交给可回归的专用模型，保证评测指标可测。

**与 STDF 联合的交叉验证信号**：

| 信号组合 | 推断 |
|---|---|
| 单 site 集中失败 + wafer map 无空间性 | 探针接触/通道故障（硬件） |
| 全 site 一致失败同一测试项 + 无 wafer 相关性 | 测试程序/限值错误（软件） |
| 同 lot 内多 wafer 同位置同 bin | 工艺批次问题 |
| 空间 EdgeRing + 特定 bin | 工艺边缘均匀性 |
| 同 site 在多台机台同一 wafer 复现 | 测试程序或板卡硬件（可跨机台比对定位） |

### 3.7 失败模式聚类与异常归因（核心算法）

**步骤 1：失败签名（Signature）**
对每个失败 part，收集其失败测试项集合（排序后拼接哈希）：

```
signature = sha1(sorted(failed_test_nums))
signature_text = "T101_VDD_FUNCT|T205_TSHIFT_FUNC|T310_OSC_FUNC"
```

**步骤 2：聚类**
- 首选 **连通分量合并 + 规则**：签名完全相同 → 同簇；相似度 Jaccard ≥ 0.8 且差异项在同一"功能组"（由 TP 元数据定义）→ 合并；
- 规模大时用 **HDBSCAN**（不需要预设簇数，抗噪），输入为二值失败向量或 Jaccard 距离矩阵；
- 簇命名自动生成：`SIG-<hash前6位>` + 主导测试项 + 代表性 bin + 占比。

**步骤 3：失败模式分类（把"不同原因"分开 —— 用户明确要求）**

| 模式 | 判据（可实现） | 根因假设 | 优先级 |
|---|---|---|---|
| **Probe Fail / 接触不良** | 集中在 1~2 个 site；单测项失败率高但同 die 其他项正常；同 wafer 重测 pass 率高；wafer map 呈随机/无结构 | 探针磨损、接触电阻高、wafer 未平整 | 高 |
| **多 Site Fail** | ≥50% site 同时失败同一测试项，且多台机台可复现 | 测试程序错误、限值错误、板卡/仪器故障（如 SMU 通道坏） | 高 |
| **单 Site Fail** | 仅 1 个 site 显著异常，其余正常 | 单通道硬件、该 site 探针针尖异常 | 高 |
| **程序错误（TP Error）** | 同一 TP 版本、跨 lot 跨机台、跨 wafer 稳定失败；失败测试项在 TP 中限值异常（如上下限写反、等于边界） | 限值/条件设置错误、bin 定义错误 | 高 |
| **工艺/材料问题** | 同 lot 内多 wafer 一致，跨机台一致，wafer map 有空间结构 | 前段工艺异常、材料批次、量测漂移 | 中 |
| **数据/解析异常** | PCRR≠MRR、HDMR 缺失、unit 缺失、值超物理范围 | STDF 导出/解析 bug | 中 |
| **重测掩盖** | is_retest 且首测 fail | 复测策略问题，需关注"真良率" | 低 |

**步骤 4：归因置信度与证据**
每个归因结论输出结构化对象：

```json
{
  "cluster_id": "SIG-3f9a21",
  "cluster_desc": "site2 上 T205_TSHIFT_FUNC 集中失败",
  "part_count": 1842, "wafer_count": 11, "share": 0.073,
  "root_cause_type": "probe_fail",
  "root_cause_hypotheses": [
    {"cause": "探针接触电阻偏高", "confidence": 0.78,
     "evidence": ["88% 失败集中于 site2", "重测 pass 率 61%", "wafer map 无空间相关性"]},
    {"cause": "site2 某通道板卡故障", "confidence": 0.15,
     "evidence": ["仅 site2 异常"]}
  ],
  "recommended_actions": [
    {"action": "复测 wafer#3 用备用探针卡", "priority": "P1", "need_hitl": true}
  ],
  "supporting_records": ["exec:88231", "wafer_map:10233", "tp:SIG_tshift_v3"]
}
```

**置信度来源**：规则打分（可解释）+ 历史故障库检索相似度 + 跨机台/跨 lot 一致性统计 + VLM 复核（仅在规则无法区分时）。

### 3.8 趋势异常检测与告警解释

**检测方法**（分层，对应不同周期）：

| 层级 | 方法 | 适用 |
|---|---|---|
| 单点阈值 | 良率 < 阈值、bin1 占比骤降 | 明确红线 |
| 统计过程控制 | Shewhart X-bar/C 控制图（EWMA） | 稳态过程漂移 |
| 时序预测残差 | Prophet / STL 分解 → 残差超 3σ | 季节性波动 |
| 变点检测 | PELT / ADWIN | 工艺切换、探针卡更换 |
| 多维异常 | Isolation Forest / 特征子集 | 多指标组合异常 |
| 同比环比 | 上周/上月/同批次对比 | 业务直接问法 |

**告警解释生成**：把触发规则 + 异常幅度 + 同期关联事件（换卡、换料、TP 变更、加班、设备参数调整）喂入 LLM，输出：
1. **发生了什么**（数值 + 时间窗 + 影响范围）；
2. **影响多大**（影响的 lot/wafer/die 数、是否已流到下游）；
3. **可能原因**（Top3 假设 + 置信度 + 证据）；
4. **建议动作**（含验证步骤、责任角色）；
5. **关联链接**（原始 STDF 记录、wafer map、相关 SOP 条目）。

**数据源关联**：换卡/换料等事件来自 **MES/设备日志**（需对接），否则告警解释只能基于 STDF 内部信号，准确率上限约 60%。

### 3.9 自动测试结论报告

面向两种读者，模板不同：

| 报告类型 | 读者 | 必备内容 | 长度 |
|---|---|---|---|
| 产线日报 | 产线/工艺 | 良率趋势、Top 失败簇、异常 wafer、待处理项 | 1 页 |
| 客户周报 | 客户/销售（脱敏） | 良率达成 vs 规格、异常说明与改善措施、后续计划 | 2-3 页 |
| 专项分析报告 | 内部工程 | 完整证据链、复现路径、TP 缺陷定位、建议补丁 | 5+ 页 |

报告生成模板：
```
# {lot} 测试结论摘要
## 1. 结论（TL;DR）  ← 3 句以内，必须含良率数字与是否达标
## 2. 数据范围与口径  ← wafer 数、die 数、时间窗、TP 版本、数据完整性
## 3. 良率与 bin 分布
## 4. Top-N 失败模式与根因  ← 每条带证据
## 5. 异常与风险
## 6. 建议行动项（含责任与期限）
## 7. 附录：原始记录索引
```
**质量要求**：数字必须来自 SQL 结果直接填充（模板变量注入），LLM 只负责文字组织 —— 这是防止数值幻觉的关键工程手段。

---

## 4. 场景 B：测试用例自动生成 Agent

### 4.1 输入与输出定义

**输入（可组合、可缺省）**：
1. 芯片规格书 / Datasheet（PDF，含表格与图）
2. 产品规格文档（Word/Markdown，含功能需求列表 FR）
3. 历史测试用例（Excel/Word/用例管理平台导出）
4. 测试程序 TP（源码或清单导出）
5. 失效模式库 FMEA / 客户投诉历史 / 8D 报告

**输出（结构化测试用例）**：

```json
{
  "case_id": "TC_FUNC_VDD_001",
  "title": "VDD 电源功能测试-常温常压",
  "category": "功能测试",
  "dft_method": "Functional",
  "priority": "P1",
  "preconditions": [
    {"type": "environment", "value": "T=25±3℃, Humi=30~70%RH"},
    {"type": "instrument", "value": "SMU 量程 ±5V, 精度 ±0.1%"},
    {"type": "fixture", "value": "探针卡 PC-07 已校准，日期 2026-09-30"},
    {"type": "sample", "value": "lot>=3, 每 lot wafer>=3, 每 wafer die>=25"}
  ],
  "test_steps": [
    {"step": 1, "action": "对 DUT 施加 VDD=3.3V, 等待 10ms 建立稳定", "params": {"VDD": "3.3V", "delay": "10ms"}},
    {"step": 2, "action": "读取 DUT 输出引脚电压", "params": {"pin": "DOUT"}},
    {"step": 3, "action": "与期望值比较并记录结果", "params": {"expected": "3.3V ± 5%"}}
  ],
  "test_items": [
    {"test_num": "T101_VDD_FUNCT", "name": "VDD 输出功能", "unit": "V",
     "lower_limit": 3.135, "upper_limit": 3.465, "measure": "DC"}
  ],
  "expected_result": "DOUT 输出 3.3V±5%；判定 PASS，且限值在规格书 Vmin=3.135/Vmax=3.465 范围内",
  "pass_criteria": "全部 test_item 判定 PASS，且无 invalid 结果",
  "fail_criteria": "任一 test_item 判定 FAIL 或结果 invalid",
  "covered_failure_modes": [
    {"mode_id": "FM-012", "mode": "VDD 上电后无输出（开路/短路）", "coverage": "direct"},
    {"mode_id": "FM-018", "mode": "VDD 输出纹波超限", "coverage": "indirect"}
  ],
  "traceability": {
    "spec_ref": [{"doc_id": "SPEC-77", "section": "3.2 Electrical Characteristics", "page": 12}],
    "requirement_ref": ["FR-VDD-001", "FR-PWR-003"],
    "tp_ref": {"tp_name": "TP_CMOS_32G", "tp_version": "v2.4"},
    "similar_cases": ["TC_FUNC_VDD_000(历史)", "TC_FUNC_VDD_003(历史)"]
  },
  "risk_level": "high",
  "generated_by": "agent@testcase-gen v1.0",
  "review_status": "pending_human_review"
}
```

### 4.2 文档解析流水线

```
PDF/Word/Excel
   ▼
[预处理] 去页眉页脚、目录重建、表格框线修复、页码映射
   ▼
[结构化解析] PyMuPDF (文本+坐标) + pdfplumber (表格) + OCR(PaddleOCR / PP-Structure)
   ▼
[语义切块]  按"章节标题"+"表格完整性"切块；表格 → 结构化 list of dict
   ▼
[规格抽取]  LLM 抽取结构化参数：参数名/符号/最小/典型/最大/单位/条件/测试条件
   ▼
[单位归一]  统一到 SI/工程单位，记录原始值与原文位置（可溯源）
   ▼
[入库]  向量库 + 关系表 spec_param(spec_id, param_code, min, typ, max, unit, condition, page, quote)
```

**结构化规格抽取示例（带溯源）**：

```json
{
  "spec_id": "SPEC-77",
  "param_code": "VDD",
  "min": 3.135, "typ": 3.3, "max": 3.465, "unit": "V",
  "condition": "T=25℃, IO=0mA",
  "source": {"doc_id": "SPEC-77", "page": 12, "table": "Table 3-2", "quote": "VDD 3.135 3.300 3.465 V"},
  "test_condition_hint": "DC 静态，10ms 建立时间"
}
```

### 4.3 需求 → 失效模式 → 测试项 推导链

这是场景 B 的**技术核心**。采用"规格参数 + 功能需求 + FMEA"三路融合推理：

```
① 规格参数抽取（每个 AC 参数是一条潜在测试项）
   VDD(min,typ,max) → 测试项"VDD 功能/范围测试"，限值直接来自规格
② 功能需求 → 失效模式（FM）
   FR-VDD-001 "器件应在 VDD 上电后提供稳定输出"
   → FM-012 输出缺失 / FM-018 纹波超限 / FM-021 上下限越界
③ 失效模式 → 测试方法映射（DFT 手段）
   开路/短路 → Boundary Scan / 参数测试（上下限）+ 电流检测
   逻辑功能错 → ATPG 生成向量 + Scan 压缩测试
   存储单元 → MBIST
   时序 → Timing Measurement（setup/hold/min-max）
   静态功耗/漏电 → 参数测试（低电流测量）
④ 测试项模板库匹配 + RAG 增强
   检索"同型号历史用例"+"TP 中已有测试项"+"模板库中对应 FM 的标准用例"
   → 产出候选测试步骤与采样要求
⑤ 组装 + 去重 + 覆盖度计算
```

### 4.4 DFT 知识要点（生成器必须内建）

| DFT 手段 | 目的 | 典型测试项 | 关键参数 | 常见用例要点 |
|---|---|---|---|---|
| **Scan（JTAG）** | 结构互连测试 | Scan chain integrity、stuck-at-0/1、transition | 链长、clock freq、模式数 | 链连接性；覆盖率 ≥ 99%；覆盖率模式计数 |
| **MBIST** | 存储器 BIST | march 算法覆盖、address decode、data pattern | 算法族、地址空间、时间 | 算法数与覆盖率；软/硬修复策略；运行时间上限 |
| **ATPG** | 自动生成测试向量 | 组合/时序逻辑 fault coverage | fault model、覆盖率目标 | fault 覆盖率、不可测 fault 清单、向量数 |
| **Boundary Scan** | 引脚级互连 | connectivity、TXZ、boundary delay | 频率、pattern | 开短路检测；上下拉状态；驱动能力 |
| **参数测试（Parametric）** | DC/静态特性 | VDD、leakage、resistance | 量程、精度、force 条件 | 量程选择、guard、settling time、量测不确定度 |
| **时间测量（Timing）** | 时序特性 | setup/hold、min/max period、tPD | 负载电容、边沿速率 | 负载条件必须写清；PVT 角点；温度角点 |

**用例生成必须遵守的规则（内建为 lint 规则）**：

| 规则 ID | 规则 |
|---|---|
| `R01` | 限值必须来自规格书，禁止 LLM 编造数值；无来源则标注"待确认"并阻断 |
| `R02` | 每个 test_item 必须有 unit；无单位的值判不合格 |
| `R03` | 上下限必须满足 lower ≤ typical ≤ upper，且限值带至少 1 位有效数字（避免 3.3000001） |
| `R04` | 步骤必须可复现：每步需有明确 action + params（电压/电流/时间/温度/负载） |
| `R05` | 必须包含 PVT 条件（至少温度），否则标注条件不完整 |
| `R06` | 采样量必须明确（lot/wafer/die 数量），参考业界（如每 lot ≥3 wafer，每 wafer ≥25 die） |
| `R07` | 每个用例至少覆盖 1 个失效模式；总用例集必须覆盖 FMEA 中 P1/P2 全部条目 |
| `R08` | 重复度阈值：与历史用例语义相似度 > 0.9 且覆盖失效模式不新增 → 标记重复 |
| `R09` | 判定标准必须二值可判定（PASS/FAIL），不得出现"基本正常""大致符合" |
| `R10` | 测试项限值必须与 TP 中同名 test_num 一致；不一致则提示 TP 需更新 |

### 4.5 生成 → 自检 → 人工评审闭环

```
① 检索召回（历史用例 / TP / 规格 / 模板 / FMEA）
        ▼
② 生成（结构化 JSON，Schema 强约束：JSON Schema + Pydantic 校验，失败重试 ≤2）
        ▼
③ 自检 Lint（规则 R01-R10 静态检查 + 语义检查）
        ├─ 硬失败（如 R01/R02/R09）→ 回炉重生成（带错误原因），最多 2 轮
        └─ 软警告（如 R05/R06）→ 标注提示，人工确认
        ▼
④ 覆盖度计算：新用例集 vs FMEA P1/P2 → 覆盖矩阵（CSV/热力图）
        ▼
⑤ 人工评审工作台（Cursor 内嵌 Web UI）
        - 左侧：spec 原文高亮 + TP 对应 test_num（可点击跳转到 TP）
        - 中间：生成用例卡片，逐字段 diff（与历史最相似用例并排）
        - 右侧：覆盖矩阵 + Lint 报告 + 本次新增/修改点
        - 操作：采纳 / 修改后采纳 / 拒绝（拒绝需选原因 → 回流训练/规则）
        ▼
⑥ 落库 → 导出 Excel/Word（飞测模板格式）→ 版本化归档
        ▼
⑦ 反馈回流：采纳率/修改率/拒绝原因进入评测集与 Prompt 优化
```

**人工评审工作台是场景 B 成败的关键**：没有好的 UI，产出会被工程师全量手改，评测里的"实际被采纳率"就无从谈起。最小可用版本：Diff 视图 + 一键采纳/拒绝 + 拒绝原因标签。

### 4.6 测试用例质量自动检查项

| 检查维度 | 指标 | 计算方式 | 阈值（建议） |
|---|---|---|---|
| 字段完整率 | completeness | 非空必填字段数 / 必填字段总数 | ≥ 95% |
| 格式合规率 | format_valid | 通过 JSON Schema + 模板规则的用例数 / 总数 | ≥ 98% |
| 覆盖率 | fm_coverage | 被至少 1 用例覆盖的 P1/P2 失效模式数 / 总数 | ≥ 90% |
| 限值可溯源率 | limit_traceable | 限值能定位到规格书 page/table 的 test_item 比例 | ≥ 98% |
| 单位一致率 | unit_consistent | unit 合法且与规格书一致的 test_item 比例 | ≥ 99% |
| 上下限合理率 | limit_sane | 满足 R03 且在物理合理区间的比例 | ≥ 97% |
| 与历史重复率 | dup_rate | 语义相似度 > 0.9 的用例占比 | ≤ 10% |
| 语义相似度 | sim_vs_expert | 与专家基线用例（BLEU/BERTScore/Embedding 余弦） | ≥ 0.75 |
| 可执行性 | step_executable | 步骤参数完整、可在机台复现的比例 | ≥ 90% |
| 评审通过率 | expert_pass_rate | 专家盲评 1-5 分 ≥4 分的比例 | ≥ 70% |

---

## 5. 工具集设计（Agent Tool Contract）

### 5.1 通用契约

```json
{
  "name": "sql_query",
  "description": "在语义层只读数据仓库上执行只读 SQL，返回结构化结果集",
  "input_schema": {
    "type": "object",
    "required": ["sql", "purpose"],
    "properties": {
      "sql": {"type": "string", "description": "只读 SELECT/WITH 语句，禁止 DDL/DML"},
      "purpose": {"type": "string", "description": "该查询要回答的问题，用于审计与复核"},
      "max_rows": {"type": "integer", "default": 1000},
      "timeout_ms": {"type": "integer", "default": 15000},
      "metric_codes": {"type": "array", "items": {"type": "string"},
                       "description": "引用的指标定义编码，便于口径追溯"}
    }
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "columns": {"type": "array", "items": {"type": "object"}},
      "rows": {"type": "array"},
      "row_count": {"type": "integer"},
      "truncated": {"type": "boolean"},
      "elapsed_ms": {"type": "number"},
      "source_tables": {"type": "array", "items": {"type": "string"}}
    }
  },
  "permission": "READ_ONLY",
  "hitl_required": false,
  "timeout_ms": 20000,
  "retry": {"max": 2, "backoff_ms": 500},
  "audit": true
}
```

### 5.2 工具清单

| 工具名 | 作用 | 入参 JSON Schema（节选） | 返回（节选） | 权限级别 | HITL |
|---|---|---|---|---|---|
| `stdf_parse` | 解析 STDF 文件并入库 | `{file_uri, mode:"full|summary", target:{wafer_id?, lot_no?}, overwrite:false}` | `{parse_id, records:{FDR:1,PCRR:3,TRR:1284}, part_total, bin_dist, warnings[], exec_id}` | WRITE_DATA | ❌（自动） |
| `stdf_get_header` | 读取执行头/元信息 | `{exec_id}` | `{pgm_name, tp_version, tester_sn, site_count, start_ts, end_ts, lot_no}` | READ_ONLY | ❌ |
| `sql_query` | 语义层只读查询 | 见上 | `{columns, rows, row_count, elapsed_ms}` | READ_ONLY | ❌ |
| `metric_get` | 取指标定义（口径） | `{metric_code}` | `{name, business_def, sql_template, version}` | READ_ONLY | ❌ |
| `dim_value_lookup` | 维度值字典检索（Schema Linking 用） | `{column, keyword, limit}` | `{values:[{value, display, count}]}` | READ_ONLY | ❌ |
| `wafer_map_get` | 取 wafer 位图与缺陷簇 | `{wafer_id, format:"bitmap|json"}` | `{rows, cols, bitmask_b64, clusters[], dominant_pattern}` | READ_ONLY | ❌ |
| `wafer_map_detect` | 缺陷图案识别 | `{wafer_id}` | `{pattern, score, features{}, cluster_features[]}` | COMPUTE | ❌ |
| `wafer_map_diff` | 两 wafer 图对比 | `{wafer_id_a, wafer_id_b, mode}` | `{diff_map, newly_failed_dies, jaccard, pattern_change}` | COMPUTE | ❌ |
| `fail_cluster` | 失败签名聚类 | `{scope:{lot_no|wafer_id|product_code|date_range}, method:"hdbscan|rule", min_support}` | `{clusters:[{sig, count, share, top_items[], sample_part_ids[]}]}` | COMPUTE | ❌ |
| `rca_analyze` | 根因归因 | `{cluster_sig, context:{include_history:bool, include_tp:bool}}` | `{root_cause_type, hypotheses[{cause, confidence, evidence[]}], recommended_actions[]}` | COMPUTE | ❌ |
| `trend_detect` | 趋势/异常检测 | `{metric_code, dims{}, window:"30d", methods:["ewma","changepoint","zscore"]}` | `{series[], anomalies[{ts, value, score, method}], changepoints[]}` | COMPUTE | ❌ |
| `event_log_search` | 查设备/MES 事件（换卡、换料） | `{wafer_id|lot_no, types:[], time_range}` | `{events[{ts, type, detail, operator}]}` | READ_ONLY | ❌ |
| `kb_search` | 混合检索知识库 | `{query, top_k:8, filters:{product_code?, tp_version?, doc_type?, time_range?}}` | `{chunks:[{chunk_id, text, score, source:{doc,page,section}, rerank_score}]}` | READ_ONLY | ❌ |
| `kb_read` | 读全文片段 | `{chunk_id}` | `{text, source, neighbors[]}` | READ_ONLY | ❌ |
| `tp_lookup` | 查 TP 测试项与限值 | `{tp_name, tp_version?, test_num?, keyword?}` | `{tp_id, version, items:[{test_num, name, unit, lower, upper, dft_method}]}` | READ_ONLY | ❌ |
| `spec_extract` | 从规格文档抽取参数 | `{doc_id, sections?:[], param_codes?:[]}` | `{params:[{param_code, min, typ, max, unit, condition, source:{page,quote}}], gaps[]}` | COMPUTE | ❌ |
| `doc_parse` | 文档解析/OCR | `{file_uri, pages?}` | `{doc_id, title, version, blocks:[{type, text, page, table?}], warnings[]}` | COMPUTE | ❌ |
| `case_generate` | 生成结构化测试用例 | `{spec_ids[], requirement_ids[], tp_ref?, fm_ids?, count, template?}` | `{cases:[{case_id, title, preconditions[], steps[], test_items[], expected_result, covered_failure_modes[], traceability}]}` | COMPUTE | ❌ |
| `case_lint` | 用例质量静态检查 | `{cases[] 或 case_ids[], rules:["R01".."R10"], compare_history:bool}` | `{report:[{case_id, rule_id, level, message, evidence}], summary:{pass_rate, coverage_matrix}}` | COMPUTE | ❌ |
| `case_dedupe` | 与历史用例去重 | `{cases[], history_set_id, threshold:0.9}` | `{duplicates:[{case_id, similar_case_id, similarity, added_value}]}` | COMPUTE | ❌ |
| `report_render` | 生成报告 | `{template:"daily|weekly|customer|spec", data:{...}, include_evidence:bool}` | `{report_id, uri, preview_md}` | WRITE_DATA | ✅ |
| `notify` | 发送告警/通知 | `{channel, receivers[], title, body, severity, links[]}` | `{message_id, sent_at, status}` | EXTERNAL_SIDE_EFFECT | ✅ |
| `ticket_create` | 创建工单 | `{title, description, severity, assignee, links[]}` | `{ticket_id, status}` | EXTERNAL_SIDE_EFFECT | ✅ |
| `report_export` | 导出 Excel/Word | `{report_id, format}` | `{file_uri}` | WRITE_DATA | ✅ |
| `feedback_submit` | 记录人工反馈 | `{target_type, target_id, action:"adopt|edit|reject", reason_code?, comment?}` | `{logged:true}` | WRITE_DATA | ❌ |

**权限级别定义**：

| 级别 | 含义 | 管控 |
|---|---|---|
| `READ_ONLY` | 只读数据 | 自动执行，记录审计 |
| `COMPUTE` | 纯计算/模型推理 | 自动执行，成本计量 |
| `WRITE_DATA` | 写库/写文件 | 自动执行但需幂等 + 回滚预案 + 审计 |
| `EXTERNAL_SIDE_EFFECT` | 对外发消息/建工单 | **强制 HITL 确认** |
| `ADMIN` | 改配置/权限 | 仅管理员，强制 HITL |

### 5.3 HITL 交互规范

- 确认卡片包含：**将要执行的动作摘要 + 完整参数 + 影响范围（例：向 12 人发告警）**；
- 支持"确认一次，本次会话内全部允许"（会话级授权，带 TTL）；
- 高风险动作（修改 TP、发客户报告）需 **双人确认**（制作者 + 审核者）；
- 所有 HITL 决策进入审计表：`who / when / what / params / decision / model_version / prompt_hash`。

---

## 6. 评测指标体系

### 6.1 总体框架

```
                    ┌─────────────────────────┐
   L4 业务层  ────► │  效率 / 采纳率 / 满意度   │  ← 唯一真正证明价值的层
                    ├─────────────────────────┤
   L3 生成层  ────► │  准确 / 幻觉 / 相似度    │  ← 直接决定 L4
                    ├─────────────────────────┤
   L2 检索层  ────► │  Recall / NDCG / 支持率   │  ← 决定 L3 上限
                    ├─────────────────────────┤
   L1 系统层  ────► │  延迟 / 成本 / 并发      │  ← 决定可用性
                    └─────────────────────────┘
```

**指标治理原则**：
1. 每个指标必须有**唯一计算口径**（写入 `metric_def`，与业务语义层共用同一套定义）；
2. 指标采集**不依赖人工标注**的部分尽量自动化（埋点/日志），人工只用于主观评分与仲裁；
3. 指标必须能**归因到具体环节**（失败率拆解为 检索未召回 / SQL 错 / 模型推理错 / 数据缺失）。

### 6.2 L1 系统层指标

| 指标 | 定义 | 计算公式 | 数据来源 | 目标值（30B 基线 → 优化目标） | 采样量要求 | 归因 / 常见失败原因 |
|---|---|---|---|---|---|---|
| 首 token 延迟 P50 | 用户发送到模型输出第一个 token 的耗时 | `P50(t_first_token - t_request)` | LLM Gateway 埋点 + Nginx 日志 | ≤ 2.0s → ≤ 1.2s | ≥1000 次真实请求/周 | 网关排队、TTFT 受 prefill 长度影响、上下文过大 |
| 首 token 延迟 P95 | 同上，95 分位 | `P95(t_first_token - t_request)` | 同上 | ≤ 8.0s → ≤ 4.0s | ≥1000 次/周 | 冷启动/模型加载、显存不足降级 |
| 端到端延迟 P50 | 请求到完整响应 | `P50(t_end - t_request)` | 同上 | ≤ 10s → ≤ 6s | ≥1000 次/周 | 工具调用链长、串行检索 |
| 端到端延迟 P95 | 同上 | `P95(...)` | 同上 | ≤ 45s → ≤ 25s | ≥1000 次/周 | 多轮工具调用、SQL 全表扫描 |
| 工具调用次数/请求 | 平均工具调用链长度 | `sum(tool_calls)/requests` | Tool Registry 日志 | ≤ 3.5 → ≤ 2.5 | 全部请求 | 工具选错、缺工具、规划发散 |
| 工具调用成功率 | 工具无错返回比例 | `ok_calls/total_calls` | Tool Registry | ≥ 92% → ≥ 97% | ≥2000 次调用 | SQL 语法错、API 超时、参数错 |
| QPS / 并发 | 系统承载能力 | `requests / second`（压测） | 压测报告 | ≥ 5 QPS（8 卡） → ≥ 12 QPS | 阶梯压测 10 分钟/档 | GPU 显存不足、KV cache 争抢 |
| GPU 利用率 | 推理期间 GPU 忙碌占比 | `1 - idle_gpu_time/total_time` | DCGM / nvidia-smi | 60~75% → 80~90% | 连续 24h | 请求稀疏、批处理不足、CPU 预处理瓶颈 |
| 缓存命中率 | 命中语义缓存/前缀缓存的比例 | `cache_hits/total_requests` | 网关缓存层 | ≥ 18% → ≥ 30% | 全量 | 缓存键设计过严（时间戳进键）、语义缓存相似度阈值高 |
| 每请求 Token 成本 | 平均消耗 token 数 | `sum(tokens)/requests`（区分 in/out） | LLM Gateway | ≤ 6.5k in / ≤ 900 out → ≤ 4k in / ≤ 600 out | ≥5000 次 | 上下文塞入过多 chunk、历史消息未裁剪 |
| 每请求成本（元） | 折算 GPU 成本 | `token_cost + gpu_seconds*rate` | 成本台账 | ≤ ¥0.35 → ≤ ¥0.15 | 月度汇总 | 高峰期低效 batch、小模型被大模型替代 |
| 冷启动时间 | 服务启动到可服务 | `t_ready - t_start` | 部署监控 | ≤ 180s → ≤ 90s | 每次发布 | 模型加载慢、依赖初始化 |
| 可用性 | 服务可用时间占比 | `1 - downtime/total` | 监控 | ≥ 99.5% → ≥ 99.9% | 月度 | 单点部署、OOM、依赖服务挂 |

### 6.3 L2 检索层指标

评测基于**标注了 gold chunk 的查询集**（Qrels）。

| 指标 | 定义 | 计算公式 | 数据来源 | 目标值（基线 → 目标） | 采样量 | 归因 / 常见失败原因 |
|---|---|---|---|---|---|---|
| Recall@k | top-k 是否召回全部相关文档 | `|R_k ∩ G| / |G|` | 离线评测集 | Recall@5 ≥ 0.85 → 0.92；Recall@10 ≥ 0.93 → 0.97 | ≥300 条查询 | 切分策略不当、专有名词切碎、缺同义词表 |
| Precision@k | top-k 中相关比例 | `|R_k ∩ G| / |R_k|` | 同上 | P@5 ≥ 0.55 → 0.70 | 同上 | 文档质量差、模板重复文档堆叠 |
| MRR | 首个正确结果排名倒数均值 | `mean(1/rank_first)` | 同上 | ≥ 0.72 → 0.85 | 同上 | 排序模型偏弱、缺 rerank |
| NDCG@10 | 折损累积增益 | `DCG@10/IDCG@10`, `DCG=Σ rel_i/log2(i+1)` | 同上 | ≥ 0.70 → 0.82 | 同上 | 分级相关性标注缺失、位置偏置 |
| ground-truth 支持率 | 回答中每个数值/断言能在检索结果中找到依据的比例 | `supported_assertions/total_assertions` | 生成日志 + NLI 校验器 | ≥ 90% → 96% | ≥500 条回答 | 数值来自 SQL 但口径未引用、模型凭记忆补全 |
| 引用可溯源率 | 引用能定位到真实来源文件且内容匹配 | `valid_citations/total_citations` | 点击/校验脚本 | ≥ 95% → 99% | ≥500 条 | chunk_id 与源文件映射丢失、引用行号漂移 |
| 知识库新鲜度 | 知识条目更新时间滞后 | `now - max(updated_at)` | 元数据 | ≤ 30 天 → ≤ 7 天 | 全库扫描 | 无发布流程、TP 变更未同步 |
| 权限过滤正确率 | 按用户权限过滤后无误召回/漏召回 | 人工抽检 + 权限模拟 | 安全测试 | 100%（不可妥协） | 每月全量 | 过滤条件未下推到检索层 |
| 空召回率 | 检索无结果导致需澄清/拒答 | `empty_retrieval/queries` | 检索日志 | ≤ 12% → ≤ 6% | ≥1000 次 | 用户表达模糊、别名未维护 |

### 6.4 L3 生成层指标

#### 6.4.1 数值问答（NL2SQL）

| 指标 | 定义 | 计算公式 | 数据来源 | 目标值（基线 → 目标） | 采样量 | 归因 / 常见失败原因 |
|---|---|---|---|---|---|---|
| SQL 可执行率 | 生成的 SQL 能在沙箱跑通 | `executable_sql/total_sql` | 执行日志 | ≥ 85% → 96% | ≥500 条问数 | 列名猜错、缺 join、方言不匹配 |
| SQL 语义正确率 | 跑通且语义与标准答案一致（人工/LLM 判定等价） | `semantic_correct/executable_sql` | 专家标注 + Judge | ≥ 78% → 90% | ≥300 条 | 时间口径错（自然月 vs 30 天）、重测未过滤、维度映射错 |
| 执行准确率 EX | 最终答案数值与标准答案完全一致 | `exact_match/answers` | 评测集 | ≥ 65% → 85% | ≥300 条 | 语义正确但数值精度/舍入不一致 |
| 容差准确率 | 相对误差 ≤ 阈值即算对 | `abs(pred-gold)/abs(gold) ≤ 0.001` | 同上 | ≥ 80% → 93% | 同上 | 百分比 vs 百分点混淆（下降 3% vs 下降 3 个百分点） |
| 多跳问题正确率 | 需 2 步以上推理的问题正确率 | 分层统计 | 同上 | ≥ 55% → 78% | ≥100 条 | 分解错误、上下文丢失 |
| 时间口径准确率 | 生成的 SQL 时间窗与用户意图一致 | 人工判定 | 同上 | ≥ 80% → 93% | ≥200 条 | "上个月"=自然月/滚动月歧义、时区 |
| 澄清正确率 | 应澄清时正确发起澄清 | `correct_clarify/needed_clarify` | 评测集 | ≥ 70% → 88% | ≥150 条 | 过度澄清（用户烦）、不澄清直接错答 |
| 拒答正确率 | 无数据时正确拒答而非编造 | `correct_refuse/unanswerable` | 对抗集 | ≥ 95% → 99% | ≥100 条 | 检索为空仍强行作答 |

#### 6.4.2 用例生成

| 指标 | 定义 | 计算公式 | 数据来源 | 目标值（基线 → 目标） | 采样量 | 归因 / 常见失败原因 |
|---|---|---|---|---|---|---|
| 字段完整率 | 必填字段非空比例 | `non_null_required_fields/required_fields` | `case_lint` | ≥ 82% → 95% | ≥300 用例 | 模板缺字段、未结合规格 |
| 格式合规率 | 通过 Schema 与格式规则 | `valid_cases/total` | `case_lint` | ≥ 88% → 98% | 同上 | JSON 截断、字段名不一致 |
| 限值可溯源率 | 限值能定位到规格出处 | `traceable_limit_items/total_items` | `case_lint` | ≥ 70% → 98% | 同上 | 模型编造数值（R01 违例） |
| 单位一致率 | 单位合法且与规格一致 | `consistent_unit_items/total_items` | `case_lint` | ≥ 90% → 99% | 同上 | 单位换算（pF/nF）、大小写 |
| 上下限合理率 | 满足 lower≤typ≤upper 且物理合理 | `sane_items/total_items` | `case_lint` | ≥ 85% → 97% | 同上 | 单边限值被填成双边、精度过多 |
| 与专家用例语义相似度 | 与专家基线用例的相似度 | `mean(max_cos(emb(gen), emb(expert)))` | 专家标注 | ≥ 0.62 → 0.78 | ≥200 对 | 步骤粒度不同（专家更简）、术语不一致 |
| 专家盲评通过率 | 专家在不知来源情况下打分 ≥4 分的比例 | `scores>=4/total` | 评审工作台 | ≥ 55% → 75% | ≥150 用例（≥3 名专家） | 评审疲劳、界面信息不足 |
| 专家盲评均分 | 1-5 分平均 | `mean(score)` | 同上 | ≥ 3.2 → ≥ 3.9 | 同上 | 提示词冗长、缺少 PVT 条件 |
| 覆盖失效模式数 | 单用例平均覆盖的 FM 数 | `sum(fm_covered)/n_cases` | `case_lint` | ≥ 1.6 → ≥ 2.4 | ≥200 用例 | 失效模式推导不全 |
| FMEA 覆盖率 | P1+P2 失效模式被覆盖比例 | `covered_fm/total_p1p2_fm` | 覆盖矩阵 | ≥ 72% → 92% | 每次生成任务 | 检索未召回对应 FM、FM 库不全 |
| 与历史重复率 | 相似度 >0.9 且无新增价值的比例 | `dup_cases/total` | `case_dedupe` | ≤ 25% → ≤ 10% | 同上 | 模板化生成、未读历史用例 |
| 自修复收敛率 | 被 lint 打回后修复成功的比例 | `fixed_after_retry/retry_cases` | 生成日志 | ≥ 55% → 85% | ≥200 用例 | 错误信息未回喂、模型能力不足 |

#### 6.4.3 归因分析

| 指标 | 定义 | 计算公式 | 数据来源 | 目标值（基线 → 目标） | 采样量 | 归因 / 常见失败原因 |
|---|---|---|---|---|---|---|
| 根因命中率 Top1 | 首选根因与专家一致 | `top1_correct/total` | 专家标注（FA/维修） | ≥ 42% → 65% | ≥120 案例 | 特征不足、缺 MES 事件、历史故障库为空 |
| 根因命中率 Top3 | 前三候选含正确根因 | `hit_in_top3/total` | 同上 | ≥ 68% → 85% | 同上 | 候选生成池太小 |
| 与 FA 工程师一致率 | Agent 结论与 FA 最终结论一致 | `agree/total` | FA 工单对照 | ≥ 50% → 72% | ≥80 案例 | FA 结论本身事后修正 |
| 聚类纯度 | 簇内主导根因一致的比例 | `Σ max_cluster_count/total` | 人工抽检 | ≥ 0.70 → 0.85 | ≥30 簇 | 相似度阈值不当、特征缺失 |
| 错误归因率 | 给出错误根因（而非"不确定"）的比例 | `wrong_confident/total` | 同上 | ≤ 18% → ≤ 6% | ≥120 案例 | 过度自信、未校准置信度 |
| 正确弃权率 | 应弃权时输出"需人工/证据不足" | `correct_abstain/should_abstain` | 对抗案例集 | ≥ 60% → 88% | ≥80 案例 | 缺少不确定度校准机制 |
| 归因证据完整率 | 结论附带可验证证据的比例 | `cases_with_evidence/total` | 生成日志 | ≥ 85% → 98% | ≥120 案例 | 提示词未强制证据格式 |
| 漏报率 | 实际存在的 Top 模式未被识别 | `missed_top_patterns/total_top_patterns` | 专家标注 | ≤ 30% → ≤ 12% | ≥100 案例 | 聚类粒度不当、阈值过严 |

#### 6.4.4 幻觉率（三类分别统计）

| 指标 | 定义 | 计算公式 | 数据来源 | 目标值（基线 → 目标） | 采样量 | 归因 / 常见失败原因 |
|---|---|---|---|---|---|---|
| 数值幻觉率 | 回答中的数值在数据/检索中不存在 | `hallucinated_numbers/total_numbers` | 数值回溯校验器 | ≤ 8% → ≤ 1% | ≥500 回答 | 模型自由算数、SQL 结果被改写、缓存污染 |
| 引用幻觉率 | 引用了不存在的来源/页码 | `fake_citations/total_citations` | 引用校验器 | ≤ 10% → ≤ 2% | ≥500 回答 | chunk_id 编造、PDF 页码错位 |
| 逻辑幻觉率 | 结论与数据矛盾（数值对但推论错） | `contradictory_claims/total_claims` | NLI 校验器 + 抽检 | ≤ 12% → ≤ 4% | ≥500 回答 | 归因模板套用、忽视反例 |
| 综合幻觉率 | 至少含一类幻觉的回答占比 | `answers_with_hallucination/total` | 同上 | ≤ 18% → ≤ 5% | ≥500 回答 | — |
| 无依据断言率 | 无引用支撑的事实性断言占比 | `unsupported_claims/total_factual_claims` | 校验器 | ≤ 12% → ≤ 3% | ≥500 回答 | 模板化套话 |

### 6.5 L4 业务层指标

| 指标 | 定义 | 计算公式 | 数据来源 | 目标值（基线 → 目标） | 采样量 | 归因 / 常见失败原因 |
|---|---|---|---|---|---|---|
| 分析耗时下降比 | 同一任务人工作业 vs Agent 辅助 | `1 - t_agent_workflow/t_manual_baseline` | 任务计时埋点 + 用户问卷 | ≥ 45% → **≥ 70%** | ≥80 个真实任务 | 用户不信任结果仍手工复核；需并行操作工具缺失 |
| 用例编写工时下降比 | 用例编写耗时下降 | `1 - t_agent_case/t_manual_baseline` | 同上 | ≥ 30% → **≥ 50%** | ≥60 个用例任务 | 评审与修改工时未计入；生成质量不足导致返工 |
| 专家返工率 | 生成结果被大幅修改的比例 | `cases_heavily_edited/total_adopted` | Diff 埋点 | ≤ 40% → ≤ 18% | ≥200 用例 | 生成模板化，与专家习惯差距大 |
| 实际被采纳率 | 采纳（含小改后采纳）的比例 | `adopted_cases/total_generated` | 评审工作台 | ≥ 45% → **≥ 75%** | ≥200 用例 | 一键采纳率低、缺少 diff 视图 |
| 首次通过率 | 无需任何修改直接通过 | `passed_first_try/total` | 同上 | ≥ 25% → ≥ 55% | ≥200 用例 | 限值幻觉、步骤不可复现 |
| 问数自助率 | 用户问题无需 FA/工程师介入 | `self_served_requests/total` | 会话日志 | ≥ 55% → ≥ 80% | ≥1000 会话 | 权限不足、语义不清、口径不匹配 |
| 周活跃用户数 | 每周使用 Agent 的工程师数 | `distinct_active_users/week` | 会话日志 | ≥ 25 → ≥ 80 | 连续 8 周 | 入口不明显、未解决真实痛点 |
| 用户留存（次周） | 上周使用者本周继续使用比例 | `retained_users/active_users` | 同上 | ≥ 45% → ≥ 70% | 连续 8 周 | 首体验差、结果不可信 |
| NPS | 净推荐值 | `%推荐(9-10) - %不推荐(0-6)` | 季度问卷 | ≥ 10 → ≥ 40 | 每季度 ≥ 30 份问卷 | 样本偏差、内部员工占比高 |
| 满意度（5 分制） | 用户主观评分均值 | `mean(score)` | 问卷 + 应用内评分 | ≥ 3.4 → ≥ 4.2 | ≥ 30 份/季 | 与 L3 指标脱节、界面未优化 |
| 告警有效率 | 告警中经确认为真异常的比例 | `true_positive_alerts/total_alerts` | 告警回执 + 人工确认 | ≥ 35% → ≥ 60% | ≥150 条告警 | 阈值过松、缺上下文 |
| 漏报重大异常数 | 重大质量事故中未被系统预警的次数 | 绝对数 | 事故记录对比 | ≤ 2/季度 → 0 | 季度 | 数据延迟、基线不稳定 |
| 单问题成本节省 | 相比人工/原系统节省 | `baseline_cost - agent_cost` | 成本台账 | ≥ ¥800/人天 → ≥ ¥3000/人天 | 月度 | 需计入模型推理与运维成本 |

### 6.6 指标归因分解（失败闭环）

每个 L3 指标失败时必须能拆解到以下之一，纳入周会：

```
错误答案
 ├─ 检索未召回        → L2: Recall@k 未达标
 ├─ 召回了但排序靠后  → L2: MRR/NDCG 未达标
 ├─ 召回了但模型误用  → L3: 上下文选择/指令遵循问题 → Prompt/模型
 ├─ SQL 本身错        → L3: SQL 语义正确率未达标 → Schema Linking/规则
 ├─ 数据本身缺失/错    → 数据治理问题（绕过 Agent 层）
 └─ 用户意图理解错    → 澄清率/意图分类问题
```

---

## 7. 评测集建设方案

### 7.1 数据来源

| 来源 | 内容 | 量级 | 采集方式 |
|---|---|---|---|
| 历史工单/邮件/8D | 专家已解决的真实问题 + 标准答案 | 300+ 条 | 导出后人工脱敏与重写 |
| FA 报告 | 根因分析案例（现象→根因→处置） | 150+ 例 | FA 工程师整理 |
| MES/产线日报 | 良率异常事件时间线 | 200+ 例 | 数据平台导出 |
| 专家出题 | 领域专家按模板设计的高难度题 | 300+ 条 | 命题会（每周） |
| 对抗集 | 越权、诱导编造、口径歧义、无数据问题 | 150+ 条 | 红队构造 |
| 真实日志 | 线上用户提问（脱敏后） | 持续增量 | 会话日志回流 |

### 7.2 规模与分层

| 阶段 | 时间 | 规模 | 用途 |
|---|---|---|---|
| MVP | 第 1-3 月 | **200 条** | 打通链路、基线测量、Prompt 迭代 |
| 增强 | 第 4-6 月 | **500 条** | 覆盖更多机型/产线/失效模式，可支撑 A/B |
| 规模化 | 第 7-12 月 | **2000 条** | 支撑回归门禁、统计显著性、多机型泛化 |

**分层配比（500 条阶段）**：

| 层级 | 条数 | 占比 | 说明 |
|---|---|---|---|
| L1 简单问数（单表/单指标） | 130 | 26% | 单维度直接聚合 |
| L2 多跳推理（2+ 表/对比/同比） | 120 | 24% | 如"比上周下降多少" |
| L3 归因分析 | 90 | 18% | 给定簇或异常，要求根因 |
| L4 用例生成 | 80 | 16% | 给规格/需求，评生成质量 |
| L5 知识问答（RAG） | 50 | 10% | TP/手册/SOP 问答 |
| L6 对抗/安全 | 30 | 6% | 越权、幻觉诱导、口径歧义 |

### 7.3 标注规范与质量控制

- **双人独立标注 + 第三方仲裁**：每条由 2 名专家独立标注，不一致由第 3 人仲裁；
- **一致性门槛**：Cohen's Kappa ≥ **0.75** 方可入库；0.60~0.75 进入复核队列讨论；<0.60 需重写标注规范后重标；
- **标注规范文档**须包含：字段定义、判定边界、正反例，尤其是"语义正确 SQL"与"数值正确"的区分；
- **标注工具**：自建 Web（基于 Label Studio 或自研），支持快捷键与盲评（隐藏模型/人工来源）；
- **版本化**：评测集有版本号，Prompt/模型变更对应评测集版本，禁止用同一份数据既调优又评测（需保留 **20% 永不用于调优的 holdout**）。

### 7.4 回归评测流水线

```
git push / PR
   ▼
① 静态门禁：Prompt/工具 Schema 变更检查、格式校验、单元测试
   ▼
② 离线回归：跑全量评测集（离线，可并行）
   ├── 系统层：延迟/成本/工具成功率
   ├── 检索层：Recall@k / MRR / NDCG
   ├── 生成层：EX / 语义正确率 / 幻觉率 / 归因 Top1 / 用例评分
   ▼
③ 阈值判定（门禁规则，示例）
   L3 生成层: EX@500 ≥ 0.85 且 相比 main 不下降 > 2%
   L2 检索层: Recall@10 ≥ 0.97 且 NDCG@10 不下降 > 1%
   L1 系统层: P95 端到端 ≤ 25s，成本 ≤ ¥0.15
   L3 幻觉: 综合幻觉率 ≤ 5%
   任一不达标 → 阻断合并（Block PR）
   ▼
④ LLM-as-Judge 自动评分（人工分仅抽样核对）
   ▼
⑤ 生成对比报告：按问题分层（哪类问题退化最多）+ 回归样例
   ▼
⑥ 灰度：canary 5% → 25% → 100%（每档 ≥ 24h，指标达标才升级）
```

**统计显著性**：当两个版本评测集差异小于 1 个百分点时，用 **bootstrap 置信区间 + 配对检验（paired bootstrap）** 判断是否真实提升，避免噪声决策。

### 7.5 LLM-as-Judge 的使用边界

| 可自动评估 | 必须人工评估 |
|---|---|
| 格式合规、字段完整、限值可溯源 | 专家盲评 1-5 分（整体质量） |
| 数值是否存在于数据中（回溯校验） | 根因是否"专业上合理" |
| 引用是否存在且内容匹配 | 归因与现场实际是否一致 |
| 相似度、覆盖率等客观统计 | 步骤在机台上的**可执行性** |
| SQL 是否可执行/语义等价 | 是否遗漏了关键失效模式 |
| 拒答/澄清行为是否符合规则 | 报告表述是否适合发给客户 |

**Judge 模型选型与校准**：

- 选型：能力强、成本可控的中文模型（32B~70B 级）；关键判定用**多 Judge 投票**（3 模型，取多数），成本高的仅在发布前跑；
- **校准方法**：
  1. 用 100~200 条人工标注样本建立"人—Judge"对照，算 Judge 与人工的 Kappa；
  2. Kappa < 0.6 → 调整 rubric（给出更明确的评分锚点与正反例），或更换模型；
  3. 采用**成对比较**而非绝对打分（更稳定，减少尺度漂移）；
  4. 定期（每季度）重测 Judge 与人工的一致性，防模型迭代导致的漂移；
  5. Judge 打分前**不透露被评对象来源**（防位置/来源偏好）；随机交换 A/B 顺序。

### 7.6 A/B 与灰度上线策略

| 阶段 | 流量 | 时长 | 推进条件 |
|---|---|---|---|
| Shadow（影子） | 0%（并行跑，不回传用户） | 1 周 | 无 P0 故障，EX 与人工基线差 ≤ 5pt |
| Canary | 5% | 24h | 无崩溃、工具成功率 ≥ 95%、P95 延迟达标 |
| 灰度 A | 25% | 48h | 满意度 ≥ 4.0、幻觉率 ≤ 5% |
| 灰度 B | 50% | 72h | 采纳率与对照组持平或更优 |
| 全量 | 100% | — | 无回退需求；保留一键回滚开关 |

**回滚条件（任一触发即回滚）**：错误归因导致的误告警 > 3 条/天；P95 延迟恶化 > 30%；成本超预算 20%；出现数据越权访问。

---

## 8. 端到端 Demo 脚本（真实问题全链路）

**问题**：「上周 3 号线 A2 槽位那批 wafer 的 bin1 良率只有 91%，比前一周掉了 4 个点，帮我看看是什么问题」

---

### Step 0 — 用户输入（Web/IM）

```
上周 3 号线 A2 槽位那批 wafer 的 bin1 良率只有 91%，比前一周掉了 4 个点，帮我看看是什么问题
```

### Step 1 — 主控 Agent 意图解析

```json
{
  "intent": "investigate_yield_drop",
  "entities": {
    "time_range": {"raw": "上周", "resolved": "2026-11-09 ~ 2026-11-15", "timezone": "Asia/Shanghai"},
    "compare_range": {"raw": "前一周", "resolved": "2026-11-02 ~ 2026-11-08"},
    "dims": [{"dim": "line_no", "value": "3号线"}, {"dim": "wafer_slot", "value": 2}],
    "metric": "yield_bin1",
    "symptom": {"current_value": 91, "delta": -4, "delta_unit": "percentage_point"}
  },
  "plan": [
    {"step": 1, "tool": "metric_get", "args": {"metric_code": "yield_bin1"}},
    {"step": 2, "tool": "dim_value_lookup", "args": {"column": "dim_wafer.wafer_slot", "keyword": "A2"}},
    {"step": 3, "tool": "sql_query", "args": {"purpose": "计算上周与前一周 bin1 良率", "...": "..."}},
    {"step": 4, "tool": "fail_cluster", "args": {"scope": {"line_no": "3号线", "wafer_slot": 2, "date_range": "..."}}},
    {"step": 5, "tool": "rca_analyze", "args": {"cluster_sig": "<sig>"}},
    {"step": 6, "tool": "trend_detect", "args": {"metric_code": "yield_bin1", "dims": {...}}},
    {"step": 7, "tool": "report_render", "args": {"template": "daily"}}
  ]
}
```

### Step 2 — 口径确认（`metric_get`）

```json
{"metric_code":"yield_bin1","business_def":"首次测试（不含重测）判为 bin1 的 die 数 / 总测试 die 数",
 "sql_template":"SELECT 100.0*SUM(CASE WHEN p.final_bin=1 THEN 1 ELSE 0 END)/COUNT(*) ... WHERE p.is_retest=false AND {{filters}}"}
```

### Step 3 — 维度值解析（`dim_value_lookup`）

```json
{"column":"dim_wafer.wafer_slot","keyword":"A2",
 "values":[{"value":2,"display":"槽位 A2","count":184,"note":"产品 PXA-2201 常用槽位"}]}
```
→ Agent 归一化：`wafer_slot = 2`，并记录"用户说的 A2 对应槽位 2"。

### Step 4 — 生成并执行 SQL（`sql_query`）

```sql
WITH cur AS (
  SELECT 100.0*SUM(CASE WHEN p.final_bin=1 THEN 1 ELSE 0 END)/COUNT(*) AS y, COUNT(*) AS n
  FROM fact_part p JOIN dim_wafer w ON w.wafer_id=p.wafer_id
  WHERE p.is_retest=false AND w.line_no='3号线' AND w.wafer_slot=2
    AND p.ts>='2026-11-09' AND p.ts<'2026-11-16'
), prev AS (
  SELECT 100.0*SUM(CASE WHEN p.final_bin=1 THEN 1 ELSE 0 END)/COUNT(*) AS y, COUNT(*) AS n
  FROM fact_part p JOIN dim_wafer w ON w.wafer_id=p.wafer_id
  WHERE p.is_retest=false AND w.line_no='3号线' AND w.wafer_slot=2
    AND p.ts>='2026-11-02' AND p.ts<'2026-11-09'
)
SELECT cur.y AS cur_yield, cur.n AS cur_n, prev.y AS prev_yield, prev.n AS prev_n,
       cur.y-prev.y AS delta_pp, prev.y-cur.y AS drop_pp
FROM cur, prev;
```

返回：

```json
{"columns":["cur_yield","cur_n","prev_yield","prev_n","delta_pp","drop_pp"],
 "rows":[[91.24,12680,95.31,12104,-4.07,4.07]],"row_count":1,"elapsed_ms":842,
 "source_tables":["fact_part","dim_wafer"],"metric_codes":["yield_bin1"]}
```

> 关键：**4.07 个百分点**（用户说 4 个点，口径一致）。若 SQL 用了重测数据，会得到 93.8%——这正是 `is_retest=false` 口径必须由 `metric_get` 固定的原因。

### Step 5 — 失败聚类（`fail_cluster`）

```json
{"clusters":[
  {"sig":"SIG-3f9a21","count":978,"share":0.0771,"top_items":["T205_TSHIFT_FUNC","T101_VDD_FUNCT"],
   "sample_part_ids":["W3-A2-07#P00312","W3-A2-07#P00455"],"site_concentration":{"site2":0.71,"site5":0.06}},
  {"sig":"SIG-77bd10","count":312,"share":0.0246,"top_items":["T410_OSC_FREQ"],"site_concentration":{}},
  {"sig":"SIG-0c4e5b","count":141,"share":0.0111,"top_items":["T302_LEAKAGE_P"],"site_concentration":{}}]}
```

### Step 6 — 根因归因（`rca_analyze`）

系统内部计算：

```text
簇 SIG-3f9a21:
  site 集中度 = 0.71 (>0.6 判为 site 相关)              → 站点相关
  跨机台可复现？ 3 台机台中仅 TS-07 出现              → 机台/硬件相关
  同 wafer 重测 pass 率 = 0.61                          → 强接触不良信号
  wafer map 空间相关性 = 0.11 (无空间结构)              → 排除工艺空间性
  TP 版本一致性：全部同一 TP 版本，限值正常             → 排除 TP 错误
  历史故障库相似条目：FMEA-118 "site2 针尖磨损导致 VDD/TSHIFT 失效" (sim=0.83)
→ root_cause_type = probe_fail, confidence = 0.81
```

```json
{"root_cause_type":"probe_fail",
 "hypotheses":[
   {"cause":"探针卡 PC-07 site2 针尖磨损/接触电阻升高","confidence":0.81,
    "evidence":["71% 失败集中于 site2","同 wafer 重测通过率 61%","3 台机台中仅 TS-07 复现","wafer map 无空间相关性（0.11）"],
    "fault_ref":"FMEA-118"},
   {"cause":"site2 通道板卡故障","confidence":0.13,"evidence":["仅 site2 异常"]},
   {"cause":"VDD 测试条件（force/时间）设置不当","confidence":0.06,"evidence":["TSHIFT 与 VDD 功能项同时失败"]}],
 "recommended_actions":[
   {"action":"用备用探针卡 PC-09 复测 wafer W3-A2-07 前 5 片","priority":"P1","need_hitl":true},
   {"action":"调取 TS-07 site2 探针卡接触电阻测试记录","priority":"P1","need_hitl":false},
   {"action":"核查 TS-07 近期换卡/扎针记录","priority":"P2","need_hitl":false}]}
```

### Step 7 — 趋势与关联事件（`trend_detect` + `event_log_search`）

```json
{"series":[{"ts":"2026-11-09","yield":93.1},{"ts":"2026-11-10","yield":94.0},{"ts":"2026-11-11","yield":88.4}],
 "anomalies":[{"ts":"2026-11-11","value":88.4,"score":4.2,"method":"ewma"}],
 "changepoints":[{"ts":"2026-11-11T03:20:00+08:00","note":"良率水平下移"}]}
```

```json
{"events":[{"ts":"2026-11-11T02:50:00+08:00","type":"probe_card_change",
  "detail":"TS-07 更换探针卡 PC-07（原卡 PC-05 针尖磨损超限）","operator":"张工"}]}
```

> 事件与异常起点 **相隔 30 分钟**，时间相关性极强 —— 这是把"外部事件"引入归因体系后最有价值的一类证据。

### Step 8 — Wafer Map 交叉验证（`wafer_map_get` + `wafer_map_detect`）

```json
{"wafer_id":10233,"rows":8,"cols":12,"dominant_pattern":"Random",
 "pattern_score":0.88,
 "cluster_features":[{"area":37,"centroid":[7,4],"eccentricity":0.42,"edge_distance":2}],
 "note":"失效点离散、无环状/放射状结构 → 不支持工艺型空间缺陷"}
```

### Step 9 — 报告生成（`report_render`，HITL）

报告正文（数字为 SQL 注入，文字由 LLM 组织）：

```markdown
# 3 号线 A2 槽位 良率异常分析（W3-A2 系列，2026-11-09 ~ 11-15）

## 1. 结论
3 号线 A2 槽位上周 bin1 良率 **91.24%**（12680 die），较前一周 **95.31%** 下降 **4.07 个百分点**。
主因判定为 **TS-07 机台 site2 探针接触不良**，贡献约 **7.7%** 的 die 失效（978 颗）。
该异常起点 2026-11-11，与该机台当日 02:50 更换探针卡 PC-07 的记录相隔 30 分钟，事件相关性高。

## 2. 数据范围
- 范围：3 号线 / 槽位 A2 / 2026-11-09 ~ 11-15，11 个 wafer，12680 die（已剔除重测）
- TP：TP_CMOS_32G v2.4（2026-10-28 发布）
- 数据完整性：HDR 完整率 100%，PCRR/MRR 计数一致

## 3. 失效分解
| 失败簇 | die 数 | 占比 | 主导测试项 | site 集中 | 归因 |
|---|---|---|---|---|---|
| SIG-3f9a21 | 978 | 7.71% | T205_TSHIFT_FUNC | site2 71% | 探针接触不良 |
| SIG-77bd10 | 312 | 2.46% | T410_OSC_FREQ | — | 频率漂移，待观察 |
| SIG-0c4e5b | 141 | 1.11% | T302_LEAKAGE_P | — | 漏电流超标 |

## 4. 证据
- 重测通过率 61%（site2 失效 die 中）→ 典型接触不良特征
- 3 台机台中仅 TS-07 复现 → 排除工艺/料件
- wafer map 呈 Random 分布（空间相关性 0.11）→ 排除边缘/中心工艺缺陷

## 5. 建议行动
| # | 动作 | 优先级 | 责任 | 期限 |
|---|---|---|---|---|
| 1 | 用备用探针卡 PC-09 复测 W3-A2-07 前 5 片验证 | P1 | 测试组 | 11-18 |
| 2 | 调取 TS-07 site2 针尖接触电阻测试记录 | P1 | 设备组 | 11-18 |
| 3 | 若复测通过，确认 PC-07 针尖磨损并安排更换 | P2 | 设备组 | 11-20 |

## 6. 原始记录索引
exec_id 88231 / 88302 / 88415 …；wafer_map 10233~10243；FMEA-118；设备事件 2026-11-11T02:50
```

### Step 10 — HITL 确认卡片

```
⚠️ 即将执行 2 个外部动作
1. 发送告警 → 企业微信群「3号线-测试」  接收人：产线组(3)、设备组(2)
   摘要：「TS-07 探针接触不良导致 A2 槽位良率下降 4.07pp，建议复测验证」
2. 创建工单 → 设备组  标题：「TS-07 site2 探针卡 PC-07 接触不良疑似」
   链接：exec 88231 / FMEA-118
[确认执行] [确认一次并允许本会话同类操作] [取消]
```

### Step 11 — 用户追问（多轮）

```
用户：把复测的那 5 片重新算一下良率
Agent：调用 sql_query（is_retest=true, wafer_id in (10233..10237)）→ 95.6%（19/19? 具体由 SQL 返回）
       并附注："当前仅有 W3-A2-07 的重测数据，其余 4 片尚未复测"
```

> **关键设计点**：Agent 主动说明"数据只覆盖 1 片"，避免把 1 片的结果说成 5 片。

---

## 9. 数据安全与合规

### 9.1 STDF 数据脱敏

| 字段 | 风险 | 脱敏策略 | 映射管理 |
|---|---|---|---|
| `lot_no`（批次号） | 可反推客户产线 | 确定性哈希 `lot_XXXX`（HMAC-SHA256，密钥托管） | 映射表存于独立 Vault，仅授权角色可查 |
| `wafer_id` / `wafer_slot` | 客户产线追溯 | 分段编码：`W{prod}-{lot_alias}-{slot}` | 同上 |
| `customer_code` / 产品型号 | 商业机密 | 别名表 `客户A / 产品X`，保留映射供内部关联 | KMS 加密 |
| Tester SN / 探针卡 ID | 设备指纹 | 保留（内部资产必需），对外报告用 `机台-01` | 对外报告自动脱敏 |
| 测试项名 `test_num` | 反映工艺/设计 | 内部保留；对外报告用语义化别名（如"电源完整性测试"） | 别名字典 |
| 参数实测值 | 工艺参数机密 | 对外仅给统计量（良率/分布），不给单点值 | 角色权限 |
| 规格书 | IP 核心 | 入库时按产品域隔离，跨产品检索默认禁用 | 行级安全 |

**脱敏必须在入库前完成**，原始区（MinIO）保持加密存储且访问需审批；向量库/索引中仅存脱敏文本。

### 9.2 私有化部署要求

| 项 | 要求 |
|---|---|
| 部署位置 | 中科飞测内网机房，不出内网；如需外部大模型，仅允许**脱敏后**的白名单流量 |
| 推理服务 | vLLM / SGLang，GPU 8×（A100 80G 或 L20/4090 级别），支持张量并行与 P-D 分离 |
| 模型权重 | 内网镜像仓库，不运行时从公网拉取 |
| 组件 | Milvus/pgvector、Elasticsearch、Redis、MinIO、PostgreSQL、Airflow（调度）、Prometheus+Grafana |
| 网络 | 服务间走内网服务网格；数据库仅应用层可访问，Agent 沙箱用只读账号 + 行级安全 |
| 出网 | 默认全禁；LLM Gateway 统一代理并记录外发内容审计 |
| 备份 | 原始 STDF 与向量库每日全量 + 增量；恢复演练每季度一次，RTO ≤ 4h，RPO ≤ 1h |
| 合规 | 满足客户保密协议（NDA）要求；模型输出不用于外部训练；提供"数据不出域"承诺书 |

### 9.3 审计日志

```json
{
  "audit_id": "AUD-20261115-009821",
  "ts": "2026-11-15T14:22:31+08:00",
  "user_id": "eng_0132", "user_dept": "测试工程部",
  "session_id": "S-8842",
  "request_id": "R-77213",
  "intent": "investigate_yield_drop",
  "model": {"name": "qwen3-32b", "version": "2026-10-30", "temperature": 0.2},
  "prompt_hash": "sha256:9f2a...", "prompt_version": "p_v3.4",
  "tool_calls": [{"tool":"sql_query","args_hash":"sha256:...","duration_ms":842,"status":"ok"}],
  "retrieval": {"chunks_used": 6, "chunk_ids": ["C-10231","C-10240","..."], "top_score": 0.91},
  "output_hash": "sha256:...", "hitl": {"required": true, "decision": "approved", "actor": "eng_0132"},
  "pii_masked": true, "data_scope": ["dim_wafer.line_no='3号线'"],
  "cost": {"input_tokens": 4120, "output_tokens": 780, "gpu_ms": 3200}
}
```

审计要求：**不可删除、保留 ≥ 2 年、按季度归档到冷存储、支持按用户/会话/时间/工具检索**；生产库与审计库分离，审计表仅追加（append-only）。

---

## 10. 风险清单

| # | 风险 | 影响 | 概率 | 对策 |
|---|---|---|---|---|
| R1 | STDF 导出质量差（HDMR 缺失、字段不规范） | 解析后维度缺失，Agent 结论不可用 | 高 | 与测试机台团队联合制定《STDF 输出规范》；入库前做 Schema 校验，不合规文件阻断并告警；建立质量看板（完整率、PCRR/MRR 一致率） |
| R2 | STDF 记录语义各厂商不一致 | 同一指标跨机台口径不同，指标失真 | 中 | 建立**跨机台语义映射层**；指标定义绑定"口径版本"；早期只承诺 1~2 款主力机台 |
| R3 | 缺少 MES/设备事件数据（换卡、换料） | 归因无法定位外部诱因，Top1 命中率天花板约 60% | 高 | 里程碑 M2 前完成 MES 只读对接；未接通前，归因输出显式标注"缺少设备事件数据" |
| R4 | 历史故障知识库为空或过期 | RAG 无有效先验，检索召回低 | 高 | 优先做 FA 案例结构化（每案例一结构化记录）；设知识新鲜度 SLA；指定 FA 工程师为知识 owner |
| R5 | TP 与规格书文档不完整/非结构化 | 用例生成限值不可溯源，幻觉率高 | 中 | 强制 R01 规则；无来源标"待确认"并阻断；推动文档规范化（表格化） |
| R6 | 数值幻觉导致错误生产决策 | 严重（错判良率/错发客户报告） | 中 | 数值一律由 SQL 结果模板注入，LLM 禁止改写数字；后置回溯校验器；外发报告强制 HITL + 双人确认 |
| R7 | 权限越权导致客户数据泄露 | 严重（合规事故） | 低 | 行级安全 + 检索层前置权限过滤 + 只读账号 + 定期权限模拟测试（纳入回归） |
| R8 | 用户不信任、不使用 | 项目失败（采纳率低） | 中 | 从"高频痛点"入手（日报良率、Top 失败簇）；首屏给结论 + 可点击证据；做用户共创小组；每月满意度跟踪 |
| R9 | 推理成本超预算 | 规模化受阻 | 中 | LLM Gateway 统一计量；模型分级路由（简单任务小模型）；语义缓存；上下文裁剪；每场景成本上限告警 |
| R10 | 模型版本漂移导致线上效果下滑 | 指标退化 | 中 | 锁定模型版本 + 影子回归；升级前跑全量评测 + 灰度 |
| R11 | 评测集规模不足/分布偏斜 | 评测结论不可信，过拟合 | 中 | 200→500→2000 分阶段；按机型/产线/失效模式分层抽样；保留 20% holdout |
| R12 | LLM-as-Judge 与人工判定偏离 | 门禁误判，发布错误版本 | 中 | Judge-人工一致性每季度校准，Kappa<0.6 停用该 Judge；关键指标双人评审 |
| R13 | 单点依赖（某模型/某库） | 服务不可用 | 低 | 模型多候选 + 自动降级；向量库可切 pgvector/Milvus；关键路径缓存兜底 |
| R14 | 用例生成结果被大面积手改 | 工时下降目标落空 | 中 | 强化"模板 + 历史用例复用"而非自由生成；把 Diff 视图做好；把"修改点"回流成 few-shot 示例 |
| R15 | 工程师习惯难改变（Excel 流程） | 采用率低 | 中 | 提供 Excel 插件/导出到既有用例模板；Agent 嵌入现有 IM 与 CI；减少切换成本 |

---

## 11. 90 天里程碑

### 阶段一：M1（第 1-30 天）—— 数据地基与基线

| 交付物 | 验收标准 | 依赖 |
|---|---|---|
| STDF 解析服务（semistd + Rust 通道） | 解析吞吐 ≥50MB/s；PCRR/MRR 一致率 100%；HDR 完整率 ≥99.5% | 测试机台团队提供样例文件与规范 |
| 数据模型与建库（维度表/事实表/指标表 DDL 落地） | DDL 评审通过；装载 1 个真实 lot 的完整数据 | 存储资源（GPU/CPU/存储） |
| 指标语义层 v1（≥20 个核心指标定义） | 每个指标有业务口径 + SQL 模板 + 负责人；与人工口径核对一致 | 工艺/质量团队参与口径评审 |
| Agent 底座骨架（LLM Gateway + Tool Registry + 审计） | 工具契约统一；审计日志可查；只读沙箱就绪 | 基础平台组 |
| 评测集 v1（200 条）+ 标注规范 | 双人标注 Kappa ≥0.75；分层符合设计配比 | 领域专家投入 ≥40 人时 |
| NL2SQL Demo（问数闭环） | 200 条中 SQL 可执行率 ≥85%，EX ≥65% | 上述全部 |

### 阶段二：M2（第 31-60 天）—— 场景 A 核心能力

| 交付物 | 验收标准 | 依赖 |
|---|---|---|
| 失败模式聚类与归因（v1） | 120 个 FA 案例上 Top1 ≥42%、Top3 ≥68%；错误归因率 ≤18% | FA 工程师标注 + 故障库首批 ≥50 条 |
| Wafer Map 缺陷识别 | 图案分类准确率 ≥80%（内部标注集）；EdgeRing/Random 召回 ≥90% | 需 ≥200 张标注 wafer map |
| 趋势异常检测 + 告警解释 | 告警有效率 ≥35%；漏报重大异常 ≤2/季度 | 需基线稳定运行 ≥4 周 |
| RAG 知识库 v1（TP + 手册 + SOP） | Recall@10 ≥0.93；引用可溯源率 ≥95% | 知识 owner 到位，文档可导出 |
| 报告模板（日报/周报） | 数字 100% 来自 SQL；专家评审"可直接发出"比例 ≥60% | 工艺/质量确认模板 |
| MES/设备事件只读对接 | 换卡/换料事件可按 wafer/lot 查询，覆盖 ≥80% 机台 | MES 团队接口支持 |

### 阶段三：M3（第 61-90 天）—— 场景 B 与体系化

| 交付物 | 验收标准 | 依赖 |
|---|---|---|
| 用例生成 Agent v1（规格解析 + FM 推导 + 生成） | 字段完整率 ≥82%；格式合规率 ≥88%；专家盲评 ≥3.2/5 | 规格书样本 ≥30 份（多产品） |
| 用例质量自动检查（lint R01-R10） | 限值可溯源率 ≥70%；重复率 ≤25% | 历史用例库 ≥300 条可检索 |
| 评审工作台（Diff + 采纳/拒绝） | 采纳率 ≥45%；拒绝原因标签完整率 100% | 前端资源 |
| 场景 B 迭代（模板库 + RAG + 自修复） | 自修复收敛率 ≥55%；FMEA 覆盖率 ≥72% | FMEA 库 ≥100 条 |
| 回归评测流水线 + 门禁 | PR 自动跑全量评测；低于阈值阻断合并；报告可对比分层 | CI 资源 |
| LLM-as-Judge 校准 | Judge 与人工 Kappa ≥0.6；成对比较一致性 ≥80% | 人工复核 ≥100 条 |
| A/B 与灰度能力 | 支持影子/灰度/一键回滚；开关可按场景/用户维度 | 网关支持 |
| 90 天复盘与下一阶段规划 | 指标达成表 + 未达标归因 + 资源申请 | — |

---

## 附录 A：STDF 记录类型速查表

| 记录 | 代号 | 类别 | 说明 | 出现时机 |
|---|---|---|---|---|
| File Header Record | FDR | 信息 | 文件头，含 STDF 版本、创建者、时间 | 文件起始 |
| Program Information Record | PIR | 信息 | 程序信息（文件名、配置） | 执行开始 |
| Program Description Record | PRD | 信息 | 程序描述（版本、执行时间、lot/wafer、site 数） | 执行开始 |
| Header Record | HDR | 信息 | 运行头（时间戳、客户、测试条件） | 一次执行开始 |
| Hardware Map Record | HMR | 信息 | site → 硬件配置 | HDR 后 |
| Hardware Bin Record | HBR | 信息 | 硬件 bin 定义 | HMR 后 |
| Pin Map Record | PMR | 信息 | 引脚组 → DUT 引脚映射 | HBR 后 |
| Pin List Record | PLR | 信息 | 引脚列表 | PMR 后 |
| Pin Group Record | PGR | 信息 | 引脚组定义 | PLR 后 |
| Pin Group Attribute Record | PGAR | 信息 | 引脚组属性 | PGR 后 |
| Pin List Attribute Record | PLAR | 信息 | 引脚列表属性 | PLR 后 |
| Retest Descriptor Record | RDR | 信息 | 重测描述与依据 | 可选 |
| Sensor Descriptor Record | SDR | 信息 | 传感器（温度/电源）描述 | 可选 |
| Sensor Datarecord Record | SDRR | 信息 | 传感器读数 | 可选 |
| Master Program Record | MPR | 信息 | 主程序块 | 可选 |
| Master Results Record | MRR | 信息 | 总体结果汇总 | 文件结束 |
| Part Count Results Record | PCRR | 结果 | Part/Bin 计数 | 执行结束 |
| Test Synopsis Record | TSR | 结果 | 测试项级通过/失败汇总 | PCRR 后 |
| Pin Function Record | PFR | 结果 | 引脚功能测试结果 | TSR 前 |
| Test Parametric Record | PTR | 结果 | 参数测试实测值与限值 | 逐测试项 |
| Pin State Record | PSR | 结果 | 引脚状态（高阻/驱动/比较） | 逐引脚 |
| Test Results Record | TRR | 结果 | 单测试项结果（期望/实测/结果号） | 逐测试项 |
| Test Information Record | TIR | 结果 | 测试信息 | 逐测试项 |
| Common Results Record | CRR | 结果 | 通用结果（部分厂商扩展） | 可选 |
| Fail Map Record | FMR | 结果 | 失效位图 | 可选 |

**STDF 读取顺序（一次执行）**：
`FDR → PIR → PRD → HDR → HMR → HBR → PMR → PLR → PGR → [RDR] → [SDR/SDRR] → [MPR] → 执行体(PIR→PFR/PTR/PSR/TRR…) → TSR → PCRR → MRR`

---

## 附录 B：术语表

| 术语 | 英文全称 | 释义 |
|---|---|---|
| STDF | Standard Test Data Format | SEMI 制定的半导体测试数据标准格式 |
| TP | Test Program | 测试程序，定义测试项、限值与测试流程 |
| ATE | Automated Test Equipment | 自动测试设备 |
| Bin | Bin Code | 良率分档号，1 通常为 good bin |
| Wafer Map | Wafer Map | 晶圆上 die 的测试结果分布图 |
| Site | Test Site | 测试机台上的独立测试通道/工位 |
| PCRR | Part Count Results Record | Part/Bin 计数记录 |
| MRR | Master Results Record | 总体结果汇总记录 |
| TRR | Test Results Record | 单测试项结果记录 |
| HDMR | Header Data Matrices Record | 文件头附加数据（客户、时间等） |
| DUT | Device Under Test | 被测器件 |
| Hit / Miss | — | 试测中一次通过/失败 |
| Rerun / Retest | 重测 | 失败件再测，会影响真实良率口径 |
| Yields | 良率 | 良率 = Pass die / Total tested die |
| DFT | Design for Test | 可测试性设计 |
| Scan | Scan Test | 扫描测试，检查逻辑结构互连 |
| MBIST | Memory Built-In Self Test | 存储器内建自测试 |
| ATPG | Automatic Test Pattern Generation | 自动测试向量生成 |
| Boundary Scan | Boundary Scan / JTAG | 边界扫描，引脚级互连测试 |
| FMEA | Failure Mode and Effects Analysis | 失效模式与影响分析 |
| DPPM | Defective Parts Per Million | 每百万器件缺陷数 |
| NL2SQL | Natural Language to SQL | 自然语言转 SQL |
| Schema Linking | Schema Linking | 把问题中的实体链接到数据库表列 |
| RRF | Reciprocal Rank Fusion | 倒数排名融合（多路检索结果合并） |
| Reranker | Reranker | 检索结果重排模型 |
| RAG | Retrieval-Augmented Generation | 检索增强生成 |
| HitL | Human-in-the-Loop | 人在回路（人工确认环节） |
| LLM-as-Judge | — | 用大模型对输出做自动化评分 |
| SLO | Service Level Objective | 服务等级目标 |
| TTFT | Time To First Token | 首 token 延迟 |
| PPM / Kappa | Cohen's Kappa | 标注一致性系数，≥0.75 为高一致 |
| NP | Notary Protocol（此处指）Number Needed… | 本文档中指 NPS 等业务指标 |
| NPS | Net Promoter Score | 净推荐值 |
| holdout | — | 评测集中永不用于调优的保留集 |

---

*文档结束 ｜ 建议每两周更新一次指标达成情况与风险清单状态。*
