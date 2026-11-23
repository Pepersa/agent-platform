-- =============================================================================
-- doc_chunk 向量库 DDL（pgvector 参考实现）
-- -----------------------------------------------------------------------------
-- 对应设计文档：
--   docs/01_架构方案_RAG_Agent.md
--     §2.7 知识层 —— 「向量库」「Metadata 与 ACL」「混合检索融合」「Reranker」
--     §3.1 RAG 检索增强流程 —— 第 2 步（检索前权限/密级过滤）、第 4 步（RRF k=60）
--   docs/02_场景方案_STDF分析与测试用例生成.md
--     §3.5 RAG 设计 —— 知识源切分策略与必填元数据
--
-- 说明：
--   本文件是 **参考实现（reference implementation）**。即使本地运行在无 PG 的
--   Windows 环境下（SQLite + numpy 降级路径），这份 SQL 也必须保留：
--     1) 生产环境切 Milvus/pgvector 时可直接复用 DDL；
--     2) ACL 字段设计在 SQLite 降级路径中必须与本文件保持语义一致。
--
--   ACL 字段放置方案（**已选择：放入 metadata JSONB + 冗余出 3 个独立列**）：
--     - metadata JSONB：承载全部结构化元数据（source/page/section/title_path/
--       doc_version/product_line/roles/...），GIN 索引支持任意键查询；
--     - 独立列 security_level / product_line / visible_roles：
--       单独建 BTREE + GIN 部分索引，使「检索前权限过滤」能下推到 WHERE 子句，
--       避免 JSONB 表达式索引在过滤时无法走索引 plans。
--     详见下方 COMMENT ON COLUMN 段落。
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- 型号/管脚名模糊匹配（可选）
CREATE EXTENSION IF NOT EXISTS btree_gin;   -- JSONB + 标量混合 GIN

-- -----------------------------------------------------------------------------
-- 1. 主表：doc_chunk
-- -----------------------------------------------------------------------------
-- 父子分块语义（docs/01 §3.1 第 10 步 + §2.7 切分策略）：
--   * chunk_type = 'child'  → 子块，约 300 token 量级，用于**检索**
--   * chunk_type = 'parent' → 父块，约 1200 token 量级（章节级），用于**上下文回填**
--   父块通过 parent_id 关联子块；parent 自身的 parent_id 指向其所属文档根节点。
CREATE TABLE IF NOT EXISTS doc_chunk (
    chunk_id     TEXT        PRIMARY KEY,          -- 稳定哈希 ID（非向量序），见 chunker.stable_chunk_id
    doc_id       TEXT        NOT NULL,             -- 文档唯一标识
    doc_title    TEXT,                            -- 文档标题
    doc_version  TEXT,                            -- 文档版本号，如 "v1.2"
    chunk_type   TEXT        NOT NULL DEFAULT 'child'
                            CHECK (chunk_type IN ('parent', 'child')),
    parent_id    TEXT,                            -- 子块 → 父块；父块 → 文档根 chunk_id
    text         TEXT        NOT NULL,             -- 块正文
    token_count  INT         NOT NULL DEFAULT 0,  -- 估算 token 数（预算裁剪用）
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- ---------- ACL（冗余独立列，过滤条件下推到检索层）----------
    security_level TEXT       NOT NULL DEFAULT 'internal'
                            CHECK (security_level IN ('public', 'internal', 'confidential', 'secret')),
    product_line   TEXT,                           -- 所属产品线，如 "CIS32G" / "PowerIC"
    visible_roles  TEXT[]     NOT NULL DEFAULT ARRAY['*']::TEXT[],  -- 可见角色；'*' 表示全员

    embedding     vector(1024),                   -- bge-m3 输出维度 1024
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 2. 索引
-- -----------------------------------------------------------------------------
-- 2.1 向量 HNSW（余弦距离）—— docs/01 §2.7「向量库」推荐的 ANN 索引
CREATE INDEX IF NOT EXISTS idx_doc_chunk_embedding_hnsw
    ON doc_chunk USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 2.2 metadata GIN —— 支持 metadata->>'key' 等任意键过滤
CREATE INDEX IF NOT EXISTS idx_doc_chunk_metadata_gin
    ON doc_chunk USING gin (metadata jsonb_path_ops);

-- 2.3 (doc_id, parent_id) BTREE —— 父子块回填与按文档取全量块
CREATE INDEX IF NOT EXISTS idx_doc_chunk_doc_parent
    ON doc_chunk (doc_id, parent_id);

-- 2.4 ACL 过滤专用索引（配合检索前过滤下推）
CREATE INDEX IF NOT EXISTS idx_doc_chunk_acl
    ON doc_chunk (security_level, product_line);
CREATE INDEX IF NOT EXISTS idx_doc_chunk_roles_gin
    ON doc_chunk USING gin (visible_roles);

-- 2.5 按 chunk_type / doc_version 过滤
CREATE INDEX IF NOT EXISTS idx_doc_chunk_type_ver
    ON doc_chunk (chunk_type, doc_version);

-- 2.6 文档级去重（chunk_id + 内容哈希去重辅助）
CREATE INDEX IF NOT EXISTS idx_doc_chunk_created
    ON doc_chunk (created_at);

-- -----------------------------------------------------------------------------
-- 3. 字段说明
-- -----------------------------------------------------------------------------
COMMENT ON TABLE  doc_chunk IS
  'RAG 检索块表。docs/01 §2.7 知识层 / §3.1 第 5 步：召回 top-50 后按 chunk_id + 内容哈希去重。';

COMMENT ON COLUMN doc_chunk.chunk_id IS
  '稳定 chunk 标识（sha1(doc_id|ordinal|text_hash) 前 16 位），跨重建不变，供引用 [doc_id:chunk_id] 使用。';

COMMENT ON COLUMN doc_chunk.chunk_type IS
  'child = 检索单元（约 300 token）；parent = 上下文回填单元（约 1200 token，章节级）。';

COMMENT ON COLUMN doc_chunk.parent_id IS
  '子块指向其父块 chunk_id；父块指向文档根 chunk_id。docs/01 §3.1 关键取舍说明：命中 300 token 子块，回填 1200 token 父块。';

COMMENT ON COLUMN doc_chunk.metadata IS
  '结构化元数据 JSONB。必填键：source(来源文件名)、page(页码/None)、section(章节)、title_path(标题层级路径数组)、doc_id、doc_version、acl{security_level,product_line,visible_roles}。docs/02 §3.5 表格「元数据（必填，用于过滤）」。';

COMMENT ON COLUMN doc_chunk.security_level IS
  'ACL：密级。public/internal/confidential/secret。过滤条件必须下推到检索层（docs/01 §3.1 第 2 步），检索后过滤存在信息泄漏风险。';

COMMENT ON COLUMN doc_chunk.product_line IS
  'ACL：所属产品线（如 CIS32G、PowerIC、DDR5）。多产品线隔离检索时按此过滤。';

COMMENT ON COLUMN doc_chunk.visible_roles IS
  'ACL：可见角色数组。默认 {"*"} 表示全员可见；越权过滤条件：visible_roles && ARRAY[当前用户角色...] OR visible_roles @> ARRAY["*"]。';

COMMENT ON COLUMN doc_chunk.embedding IS
  'bge-m3 dense 向量，1024 维，L2 归一化后使用余弦相似度。docs/01 §2.7「Embedding 服务」推荐 BGE-M3。';

-- -----------------------------------------------------------------------------
-- 4. 检索辅助视图：只暴露子块（检索入口）
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_doc_chunk_searchable AS
SELECT chunk_id, doc_id, doc_title, doc_version, chunk_type, parent_id,
       text, token_count, metadata, security_level, product_line,
       visible_roles, embedding, created_at
FROM doc_chunk
WHERE chunk_type = 'child';

-- -----------------------------------------------------------------------------
-- 5. 建库 + 插入示例
-- -----------------------------------------------------------------------------
-- 5.1 建库（生产）
-- CREATE DATABASE feagent_kb;
-- \c feagent_kb
-- 上方 DDL 在该库中执行。

-- 5.2 插入示例（父块 + 子块）
INSERT INTO doc_chunk (
    chunk_id, doc_id, doc_title, doc_version, chunk_type, parent_id,
    text, token_count, metadata, security_level, product_line, visible_roles, embedding
) VALUES (
    'C-YIELD-0001',
    'DOC-YIELD-001',
    '良率口径定义',
    'v1.2',
    'parent',
    'C-YIELD-0000',
    '# 良率口径定义 ...（章节级父块，约 1200 token）',
    1180,
    '{"source":"良率口径定义.md","page":null,"section":"1 良率口径",
      "title_path":["良率口径定义","1 良率口径"],
      "doc_id":"DOC-YIELD-001","doc_version":"v1.2",
      "acl":{"security_level":"internal","product_line":"CIS32G","visible_roles":["*"]},
      "doc_type":"metric_definition","owner":"quality"}'::jsonb,
    'internal', 'CIS32G', ARRAY['*'],
    NULL   -- 由应用层写入 embedding
);

INSERT INTO doc_chunk (
    chunk_id, doc_id, doc_title, doc_version, chunk_type, parent_id,
    text, token_count, metadata, security_level, product_line, visible_roles, embedding
) VALUES (
    'C-YIELD-0002',
    'DOC-YIELD-001',
    '良率口径定义',
    'v1.2',
    'child',
    'C-YIELD-0001',
    'Bin1 良率 = 首次测试（is_retest = false）判为 bin1 的 die 数 / 总测试 die 数。'
    '重测件不计入分子与分母。',
    86,
    '{"source":"良率口径定义.md","page":null,"section":"1.2 Bin1 良率",
      "title_path":["良率口径定义","1 良率口径","1.2 Bin1 良率"],
      "doc_id":"DOC-YIELD-001","doc_version":"v1.2",
      "acl":{"security_level":"internal","product_line":"CIS32G","visible_roles":["*"]},
      "doc_type":"metric_definition","ordinal":2,"owner":"quality"}'::jsonb,
    'internal', 'CIS32G', ARRAY['*'],
    NULL
);

-- -----------------------------------------------------------------------------
-- 6. 相似度检索示例
-- -----------------------------------------------------------------------------
-- 6.1 纯向量检索（余弦距离，pgvector 的 <#> 为负内积，<=> 为余弦距离）
--     distance = 1 - cosine_similarity
SELECT chunk_id, doc_id, doc_title, text,
       1 - (embedding <=> $1::vector) AS score
FROM v_doc_chunk_searchable
WHERE embedding IS NOT NULL
ORDER BY embedding <=> $1::vector
LIMIT 50;

-- 6.2 **混合检索：BM25 通道 + 向量通道 + 检索前 ACL 过滤下推**
--     说明：真正的 BM25 通道由 Elasticsearch / Milvus 全文提供（docs/01 §2.7「全文检索」）；
--     这里给出「元数据过滤下推到检索层」的 pgvector 侧写法（docs/01 §3.1 第 2 步）。
--     ACL 过滤条件必须出现在 WHERE 中（而非检索后在应用层过滤）。
WITH acl AS (
    SELECT
        $1::text[] AS user_roles,     -- 当前用户角色
        $2::text   AS max_level        -- 用户可见的最高密级
),
cand AS (
    SELECT c.chunk_id, c.doc_id, c.doc_title, c.text, c.metadata,
           c.security_level, c.product_line, c.visible_roles,
           1 - (c.embedding <=> $3::vector) AS vec_score,
           ROW_NUMBER() OVER (ORDER BY c.embedding <=> $3::vector) AS vec_rank
    FROM v_doc_chunk_searchable c, acl
    WHERE c.embedding IS NOT NULL
      -- ↓↓↓ 检索前过滤下推 ↓↓↓
      AND c.security_level <= acl.max_level
      AND (c.product_line IS NULL
           OR $4::text IS NULL
           OR c.product_line = $4::text)
      AND (c.visible_roles @> ARRAY['*']::text[]
           OR c.visible_roles && acl.user_roles)
      -- ↑↑↑ 检索前过滤下推 ↑↑↑
    ORDER BY c.embedding <=> $3::vector
    LIMIT 50
)
SELECT chunk_id, doc_id, doc_title, text, metadata, vec_score, vec_rank
FROM cand
ORDER BY vec_rank;

-- 6.3 RRF 融合（k = 60，docs/01 §3.1 第 4 步）
--     score = Σ 1/(k + rank_i)，其中 rank_i 为各通道内的排名（1-based）。
--     本示例假定 BM25 通道结果已由 ES 侧物化为临时表 bm25_hits(chunk_id, rank)。
WITH acl AS (
    SELECT $1::text[] AS user_roles, $2::text AS max_level
),
vec_hits AS (
    SELECT c.chunk_id,
           ROW_NUMBER() OVER (ORDER BY c.embedding <=> $3::vector) AS rank
    FROM v_doc_chunk_searchable c, acl
    WHERE c.embedding IS NOT NULL
      AND c.security_level <= acl.max_level
      AND (c.product_line IS NULL OR $4::text IS NULL OR c.product_line = $4::text)
      AND (c.visible_roles @> ARRAY['*']::text[] OR c.visible_roles && acl.user_roles)
    ORDER BY c.embedding <=> $3::vector
    LIMIT 50
),
bm25_hits AS (
    -- ES 侧 BM25 命中表（同样在写入时已完成 ACL 过滤下推）
    SELECT chunk_id, rank FROM bm25_hit_table
),
rrf AS (
    SELECT COALESCE(v.chunk_id, b.chunk_id) AS chunk_id,
           COALESCE(1.0 / (60 + v.rank), 0) + COALESCE(1.0 / (60 + b.rank), 0) AS rrf_score
    FROM vec_hits v FULL OUTER JOIN bm25_hits b ON v.chunk_id = b.chunk_id
)
SELECT r.chunk_id, r.rrf_score, c.text, c.metadata
FROM rrf r JOIN doc_chunk c USING (chunk_id)
WHERE c.chunk_type = 'child'
ORDER BY r.rrf_score DESC
LIMIT 50;

-- -----------------------------------------------------------------------------
-- 7. 增量更新与失效（docs/01 §2.7「知识库运营」）
-- -----------------------------------------------------------------------------
-- 7.1 文档重新入库时先按 doc_id + doc_version 清理旧块
-- DELETE FROM doc_chunk WHERE doc_id = $1 AND doc_version <> $2;
-- 7.2 过期文档下线
-- UPDATE doc_chunk SET metadata = jsonb_set(metadata, '{status}', '"archived"')::jsonb
-- WHERE doc_id = $1;
-- 7.3 知识库新鲜度（docs/02 §6.3 知识库新鲜度指标）
-- SELECT now() - MAX(created_at) FROM doc_chunk WHERE chunk_type = 'child';
