# wiki-context

**面向 AI Agent 的确定性上下文引擎（Deterministic Context Engine）** —— 离线编译式 LLM Wiki + 表格感知 SQLite FTS5 + 零幻觉 MCP 知识网关。

[English](README.md) | 简体中文

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](pyproject.toml)
[![Dependencies: Zero](https://img.shields.io/badge/第三方依赖-零-brightgreen.svg)](pyproject.toml)
[![Protocol: MCP](https://img.shields.io/badge/MCP-2025--03--26-purple.svg)](docs/gateway-and-acl.md)

---

## 本项目的核心价值：为什么做 `wiki-context`？

在人手一个强力通用 Agent（**Claude Code、Codex CLI、Cursor、DeepSeek Harness**）的今天，开发者和企业团队真正缺的，**不是**再搭一套需要 8GB Docker 全家桶（向量数据库 + Embedding 服务 + Reranker + Postgres + 封闭网页聊天框）的传统 RAG 一体机。

强推理 Agent 真正需要的是一位**毫秒级响应、零黑盒、字字可回溯一手原件的「确定性 AI 图书管理员」**：

1. **「写链路重理解」与「读链路零大模型极速供料」彻底解耦**：
   - **离线写链路**：将散乱异构的 PDF、Word（`.docx`）、PPT（`.pptx`）、Excel（`.xlsx`）、CSV 与 Markdown 零依赖提取为带页码/行号/表头坐标的结构化镜像，并通过人机共创或 `wctx compile` 离线编译沉淀跨文档综述页（`wiki/` 或 `知识库/`），同时用 SHA-256 指纹锁死原件新鲜度。
   - **在线读链路**：网关自身 **0 大模型 Token 消耗、0 外部 API 依赖、0 第三方 Python 包**。单次 `wiki_context` 调用在 **~2–35 ms** 内，确定性地拼装出「高层 Wiki 全局导读（24% 预算）+ 一跳溯源的一手原件段落/自动置顶表头（76% 预算）+ 零命中缺词防幻觉护栏」。
2. **清晰的架构职责边界（`Context` vs. `State & Action`）**：
   - `wiki-context` 专注承载**只读背景认知（Context）**：技术规范（RFC/ADR）、产品选型手册、规格参数大表、商务报价红线、管理制度与故障复盘（Postmortem）。
   - 与企业事务型 MCP（SQL / ERP / GitHub / Jira MCP）天然并联互补：Agent 先花 3 ms 调一次 `wiki-context` 摸清业务规则与红线，再调用业务系统查询实时状态或执行变更动作。
3. **极简部署却具备生产级安全治理**：
   - 单文件不可变索引快照（`gen-<id>/bm25.sqlite`）无锁并发热切换、私钥与 API Key 自动脱敏、单网关多知识库挂载（`/mcp/<vault>`），以及**防综述泄密的溯源继承式目录级权限（`PrefixACLPolicy`）**。

```text
一手原始资料 (`sources/` 或 `史料/` —— PDF, DOCX, PPTX, XLSX, CSV, MD；严格只读真源)
  │  1. wctx sync  (纯标准库原生提取 + 可插拔 CLI OCR + SHA-256 增量清单)
  ▼
派生结构化镜像 (`mirrors/` 或 `史料_md/`)       编译式知识页 (`wiki/` 或 `知识库/`)
  │  (保留精确页码 / 幻灯片 / Sheet / 行号)      │  (Agent/Obsidian 精修或 `wctx compile` 自动编译
  │                                              │   + YAML `source_hashes` 新鲜度指纹 + `wctx lint`)
  └──────────────────────┬───────────────────────┘
                         │  2. wctx index (表格边界切块 + 副本折叠 + 过期自动打标)
                         ▼
         不可变快照 `gen-<id>/bm25.sqlite` ◄── `current.json` 原子热切换 (`?mode=ro&immutable=1` 无锁并发)
                         │
                         │  3. 单次 `wiki_context` 调用 (~2–35 ms，0 运行时 LLM Token)
                         ▼
     Claude Code / Codex / Cursor / DSH / 自研 Agent (支持 Stdio MCP、Streamable HTTP `/mcp` 与 REST)
```

---

## 深度对比：`wiki-context` vs. 传统 RAG vs. 纯 LLM Wiki

当前给 AI Agent 接知识库主流有两条路线：**传统切片向量 RAG** 与 **纯 LLM Wiki**（如 Karpathy 倡导的「不建向量库，由 LLM 将资料编译为 Markdown Wiki，问答时让 Agent 直接浏览目录与文件」）。在真实工程与企业场景下，这两者恰好存在互补的结构性盲区：

### 一、传统切片向量 RAG（Vector / Chunk RAG）的四大硬伤
1. **精确代号「语义糊化」（Embedding Blur）**：稠密向量擅长模糊语义匹配，但对工程与业务最致命的精确符号——国标行标 `TB/T-2075`、协议 `RFC-042`、错误码 `HTTP-429`、物料编码、版本号——极易发生「语义相近但型号张冠李戴」的误召回。
2. **长表格「腰斩丢表头」（Headerless Table Slicing）**：固定字数（如 500～800 字）切块会把上百行的 Excel/CSV/Markdown 规格表拦腰截断。当检索命中第 80 行的某个档位时，第 1 行的列名表头早已丢失，大模型只能对着一排孤立数字 `[12000, 36000, 64]` 盲猜含义（内置评测集 Q1 直接复现了该典型故障）。
3. **局部切片「只见树木不见森林」+ 巨型手册稀释权重**：孤立的 Top-K 切片无法回答「架构经过哪三次演进、为什么废弃旧方案」等跨文档全局问题；同时单本 300 页大手册在普通分块库中会严重扭曲词频统计、挤占精炼规范的召回名额。
4. **不可答问题「强塞近邻」诱发幻觉 + 基础设施笨重**：当用户询问库内根本不存在的概念（如问 `Kafka` 留存期，但系统实际用的是 Raft WAL），向量检索仍会强行返回余弦相似度最高的 Top-K 切片诱导模型胡编，且必须维护向量库与 Embedding/Rerank 接口。

### 二、纯 LLM Wiki（Agent 直接翻阅 Markdown 知识库）的四大实战瓶颈
纯 LLM Wiki 彻底抛弃了向量库，用人类可读的 Markdown 综述页解决了跨文档脉络问题，但在生产落地上会撞上另外四堵墙：
1. **在线多轮翻找「又慢又烧钱」**：Agent 回答一个常规问题，往往要先 `read 目录.md` → 猜主题打开 2～3 篇 Wiki → 发现缺少底层参数再去 `grep` 原件，动辄 **3～6 轮工具调用、耗时 15～40 秒、消耗 30,000～40,000 上下文 Token**，严重挤占主任务的推理窗口与配额。
2. **「有损压缩」丢失长尾条款与海量表行**：Wiki 综述天然是对原件的有损提炼——它能把主干架构讲得很透彻，但绝无可能把 200 页标书里的每一条细则、500 行配额表里的每一档数值原封不动搬进 Wiki。只读 Wiki 会丢细节，只搜原件又丢全局。
3. **「静默过期」沦为假情报（Silent Staleness）**：一旦底层 Word 规范或 Excel 参数表更新了数值（例如限流阈值从 `5000` 改为 `12000`），人工或半自动维护的 Wiki 页极易漏改。没有密码学级别的哈希校验，过期的 Wiki 总结反而会以极高置信度误导 Agent。
4. **缺乏企业级权限隔离与防综述泄密**：直接把文件夹交给多用户 Agent 翻阅，无法精细管控涉密目录；更危险的是**综述降密漏洞**——某员工虽无权读取 `史料/财务底稿/`，却能通过阅读引用了该底稿的顶层 `知识库/年度复盘.md` 间接套取核心机密数据。

### 三、`wiki-context` 如何融合两者所长并根除各自缺陷？
`wiki-context` 用**「离线编译器 + 在线确定性链接器」**的思路将两条路线融为一体：
- **完整保留 LLM Wiki 的跨文档综合力，但补齐「密码学保鲜」与「机械体检」**：
  - 知识层依然是扁平纯文本 Markdown + `[[wikilinks]]`（随时可用 Obsidian 打开审阅编辑、Git 可审计）；
  - 每篇 Wiki 页在 YAML Frontmatter 中记录引用原件的 **SHA-256 `source_hashes`** —— 只要底层原件发生哪怕 1 字节变更，索引器自动将该 Wiki 页标为 `stale`（检索排序自动 `0.5x` 降权 + 在证据包中打上醒目的 `⚠️ stale` 警告），配合 `wctx compile` 一键增量重编译刷新。
- **超越传统 RAG 的字面与表格精度，且单次调用 ~3ms 一步到位**：
  - **保留技术标号的中英双语 FTS5 + 文档级 IDF（`df_sources`）**：无损保留 `RFC-042`、`TB/T-2075`、`350mm` 等工程代号，中文采用二元滑窗 + 句尾语气词剥离（无需安装分词库），并按「独立源文档数」而非「切片数」计算 IDF，防止单本长文档压扁词权重。
  - **受控一跳溯源（Controlled 1-Hop Provenance）**：命中的高置信度 Wiki 页不仅提供全局导读，还会顺着 `sources:` 一跳拉取底层一手原件段落——且**仅当原件段落自身也独立命中核心高区分度查询词时**才给予 `1.25x` 提权，杜绝总览页把无关原件连带拽入上下文。
  - **表头折扣置顶滑窗（Header-Discounted Table Windowing）**：无论目标数据藏在百行大表的第几行，滑窗截取时都会**自动将首行 `table_header` 钉在片段顶部**，并在窗口打分时扣除表头已覆盖的通用列名权重，确保精准停在目标数据行。
  - **零命中缺词防幻觉护栏 + 溯源继承式 ACL**：核心查询概念若全库（含同义词展开）零命中，强制输出 `⚠️ 缺词预警 / Missing-Concept Alert` 阻断脑补；`PrefixACLPolicy` 严格保证：**用户必须拥有某篇 Wiki 页所引用的全部底层原件权限，才能看到该 Wiki 页**，彻底封死综述泄密通道。

### 全维度能力对比矩阵

| 对比维度 | 传统切片向量 RAG | 纯 LLM Wiki（Agent 自主翻阅） | **`wiki-context`** |
| :--- | :--- | :--- | :--- |
| **核心范式** | 固定切块 + Embedding 向量检索 + Rerank | LLM 维护 Markdown Wiki + 在线多轮 `read`/`grep` | **离线编译式 Wiki + 在线双层表格感知 FTS5 确定性组装** |
| **精确编号与代码（`RFC-042`、`TB/T-2075`）** | 弱（稠密向量语义模糊，易张冠李戴） | 取决于 Agent 能否猜对关键词 `grep` | **极强**（标号无损保留分词 + 文档级 `df_sources` IDF 重排） |
| **百行长表格（Excel / CSV / MD）** | 切块腰斩丢失表头，孤立数字无法解读 | Wiki 摘要会省略海量明细行 | **任意深层窗口自动置顶 `table_header`** + 表头词折扣滑窗 |
| **跨文档演进脉络与全局综述** | 弱（Top-K 碎片切片缺乏全局视角） | **强**（天然具备主题综述页与双向链接） | **强**（24% 预算给 Wiki 全局导读 + 76% 预算给一跳一手原件） |
| **口语俗称对齐专业术语** | 依赖黑盒向量近似（不可控、难调试） | 依赖 Agent 临场猜同义词重试 | **`synonyms.json` 单向标准词收敛**（保存即热生效，零重建索引） |
| **原件变更后的「总结过期」治理** | 无综述层 | 靠人工自觉同步（极易静默过期误导） | **SHA-256 `source_hashes` 自动侦测**（自动降权 `0.5x` + `⚠️ stale` 告警） |
| **超纲/不可答问题防幻觉** | 高危（强行返回最近邻切片诱导编造） | 依赖 Agent 自觉停止翻找 | **显式触发 `⚠️ 缺词预警`** + 未镜像/纯视觉页可答性边界声明 |
| **在线问答延迟与 Token 开销** | 1～5 秒 + Embedding/Rerank 接口开销 | 15～40 秒（3～6 轮工具调用，消耗 ~3.5 万 Token） | **单次工具调用，~2–35 ms，仅 2.6k～4.8k Token，0 大模型调用** |
| **多团队权限隔离（RBAC）** | 切片元数据过滤（难防派生摘要泄密） | 仅靠操作系统目录权限 | **热重载 `PrefixACLPolicy` + Wiki 100% 继承底层原件权限** |
| **部署依赖与运维成本** | 重（Milvus/Qdrant + Docker + 各类 SDK） | 纯本地文件夹 | **零第三方依赖**（Python 3.11+ 标准库 + 单文件不可变 SQLite FTS5） |

---

## 单次 `wiki_context` 真实输出示例

### 场景一：深层 CSV 大表查数 + Wiki 一跳溯源（耗时 `~3 ms`）

提问：*`What are the Sustained_RPS, Burst_Bucket, and Max_Payload_MB limits for the Enterprise-Dedicated tier?`*

```markdown
<wiki_context vault="AuroraEdge Engineering KB" route="fact" generation="gen-20261122T133553Z-1486c582">
## 1. Compiled Wiki Synthesis (Verify exact figures & clauses against Primary Sources below)
### [W1] wiki/Gateway-Rate-Limiting-and-SLOs.md § Key Facts (L20-L24) [freshness:fresh]
- **Token Bucket Enforcement**: Edge nodes enforce per-tenant token buckets using local atomic counters synced every `250ms`...
- **Enterprise Dedicated Tier**: Exact per-tier RPS and burst ceilings are defined in `sources/specs/tier-quotas.csv`.

## 2. Primary Source Evidence (Authoritative Ground Truth)
### [S1] sources/specs/tier-quotas.csv | tier-quotas | L24-L47 (section L8-L48) [via Wiki provenance]
| Tier_Code | Sustained_RPS | Burst_Bucket | Max_Payload_MB | Support_SLA |
| --- | --- | --- | --- | --- |
...
| Scale-05 | 5000 | 12500 | 25 | 24x7-Priority |
| Enterprise-Dedicated | 12000 | 36000 | 64 | 24x7-15min-Page |

## 3. Gap Diagnostics & Grounding Guardrails (Mandatory)
- Grounding Rule: Every claim must be directly supported by [S1]~[Sn] above with explicit source citations...
</wiki_context>
```
可以看到：`[W1]` 先给出经过哈希校验（`[freshness:fresh]`）的全局规则导读，同时一跳溯源拉出 `tier-quotas.csv` 第 `L24–L47` 行，并**自动将第 1 行的表头钉在片段最上方**。

### 场景二：超纲问题零命中防幻觉护栏（耗时 `~0.2 ms`）

提问：*`What is the Kafka partition retention period for billing events?`*（库内实际采用 Raft WAL，根本没有 Kafka）

```markdown
<wiki_context vault="AuroraEdge Engineering KB" route="fact" generation="gen-20261122T133553Z-1486c582">
## 2. Primary Source Evidence (Authoritative Ground Truth)
*(No directly matching primary source chunks found)*

## 3. Gap Diagnostics & Grounding Guardrails (Mandatory)
- ⚠️ Missing-Concept Alert: Key query concept(s) 'Kafka', 'partition', 'retention', 'period', 'billing', 'events' had 0 hits across all retrieved chunks! This likely indicates a knowledge gap in the vault — do NOT hallucinate figures or clauses from pretraining.
</wiki_context>
```

---

## 核心工程特性一览

1. **运行期与构建期双零依赖**：纯 Python 3.11+ 标准库实现（自带零依赖 PEP 517/660 构建后端、DOCX/PPTX/XLSX/CSV/MD 原生结构化提取器、`pdftotext` PDF 提取及可插拔 `WCTX_OCR_CMD` 扫描件 OCR 钩子），离线内网环境亦可秒级 `pip install -e .`。
2. **原生中英双语目录布局自适应**：
   - 英文布局（`--layout en`）：`sources/`、`mirrors/`、`wiki/`、`index.md`、`log.md`
   - 中文 Obsidian 布局（`--layout zh`）：`史料/`、`史料_md/`、`知识库/`、`目录.md`、`日志.md`（存量中文 Obsidian LLM Wiki 仓库无需写配置文件即可自动识别挂载）。
3. **无锁高并发与原子热更新**：读线程通过 `threading.local()` + `?mode=ro&immutable=1` 打开分代快照 `gen-<id>/bm25.sqlite`；`wctx sync` 或 `wctx index` 发布新快照时原子替换 `current.json`，旧快照自动进入 30 秒优雅排空队列，服务全程零中断、零锁竞争。
4. **纵深安全防护**：出站文本自动脱敏（拦截 `PRIVATE KEY`、`sk-...`、`AKIA...`、`Bearer`、`Cookie`）、`O_NOFOLLOW` 防符号链接越界读取、敏感库强校验开关（`sensitive: true`）以及 Token/ACL 保存即热生效。

---

## 60 秒快速开始

### 1. 安装

```bash
# 推荐：一行全局安装（独立环境，避免 macOS/Linux 系统 Python 的 PEP 668 报错）：
pipx install git+https://github.com/Minokun/wiki-context.git
# 或使用 uv：
uv tool install git+https://github.com/Minokun/wiki-context.git

# 或从本地源码可编辑安装：
git clone https://github.com/Minokun/wiki-context.git
cd wiki-context
pip install -e .

# 首次安装后一键体检（检查 Python、PATH、poppler/PDF 提取工具与本机 AI 客户端）：
wctx doctor
```

> **让 AI 帮你自动下载并安装（推荐）**：如果你已经在使用 Claude Code、Cursor、Codex CLI 等任意编码 Agent，可以直接把下面这句话发给它：
> ```text
> 请从 https://github.com/Minokun/wiki-context.git 下载安装 wiki-context（用 pipx 或 Python >= 3.11 的独立环境安装），运行 wctx doctor 检查环境，并打开 wctx ui 帮我引导配置。
> ```

*（全程无需下载任何第三方 Python 依赖包；如需原生提取 PDF 文本与渲染 PDF 视觉页，可按需安装系统级 `poppler`：`brew install poppler` 或 `sudo apt install poppler-utils`。）*

### 2. 运行内置可复现基准评测

```bash
python3 examples/benchmark/run_benchmark.py
```

在 `examples/demo-vault` 上的实测输出（对比传统 600 字定长切片 BM25 vs. `wiki-context` 单次 `ContextPack`）：

```text
ID   Route    NaiveBM25@3   ContextPack   Latency(ms)  Question
----------------------------------------------------------------------------------------
Q1   fact     MISS          PASS          2.94         What are the Sustained_RPS, Burst_Bucket, and
Q2   fact     PASS          PASS          3.54         When requests get throttled at the edge, how o
Q3   fact     PASS          PASS          1.87         How fast is container warmup p95 latency and h
Q4   complex  PASS          PASS          2.13         Compare Gateway circuit-breaker trip threshold
Q5   fact     (halluc.risk) GUARD_OK      0.20         What is the Kafka partition retention period f [Unanswerable]
----------------------------------------------------------------------------------------
Strict Complete-Evidence Recall : Naive 600c BM25 = 3/4 (75.0%)  vs  ContextPack = 4/4 (100.0%)
Unanswerable Gap Guardrail      : 1/1 (100.0%)
ContextPack Median Latency      : 2.13 ms
```

### 3. 初始化你自己的知识库

```bash
# 初始化中文三层知识库（`史料/`、`史料_md/`、`知识库/`、`目录.md`）
wctx init ./我的知识库 --name "研发技术规范库" --layout zh

# 或初始化英文布局知识库（`sources/`, `mirrors/`, `wiki/`, `index.md`）
wctx init ./my-kb --name "Platform Engineering KB" --layout en

# 将 PDF / Word / Excel / PPT / CSV / Markdown 原件放入 史料/，增量提取并建索引：
cp ~/Documents/*.docx ./我的知识库/史料/
wctx sync --vault ./我的知识库

# 在终端直接测试单次证据包组装：
wctx context --vault ./我的知识库 -q "系统网关限流规则与重试要求是什么？"

# 或一键打开可视化 Web 控制中心（默认 http://127.0.0.1:8765）：
wctx ui
```

---

## 使用场景与部署指南（个人 / 局域网 / 服务器 / 纯 MCP 接入）

不同角色只需按自己的场景选一种方式即可，无需全部折腾：

| 使用场景 | 推荐方式 | 对方/本机需要装什么？ | 适用说明 |
| :--- | :--- | :--- | :--- |
| **1. 个人本机使用** | **桌面 App** 或 **`wctx ui`（Web 端）** | 本机装桌面 App（或 `wctx`） | 拖入本地文件夹即可建索引，自动扫描并一键连本机 19 款主流 AI 工具 |
| **2. 个人多电脑 / 局域网共享** | 桌面 App / `wctx ui` 开启局域网共享 | 两台电脑装桌面 App 或 `wctx` | 生成 5 分钟一次性配对码，免填 IP 自动互联 |
| **3. 团队服务器部署（管理员）** | **`wctx gateway`** + **`wctx service install`** | 服务器装 Python 3.11+、`wctx`（可选 `poppler`） | 多人共享、Bearer 密钥鉴权、按文件夹隔绝权限；按需 SSH 隧道开 `wctx ui` |
| **4. 纯 MCP 接入团队服务器（支持 HTTP MCP）** | **客户端直连 Streamable HTTP** | **零安装**（无需装 Python 或 `wctx`） | 向管理员拿 URL + 个人密钥 `wctx_...`，直接填进 Claude Code / Cursor / VS Code / WorkBuddy |
| **5. 纯 MCP 接入团队服务器（仅支持 stdio / 一键全配）** | **`wctx remote add`** 或 **`wctx bridge`** | 仅需全局装 `wctx` CLI（或桌面端） | 自动发现并同步本机全部 AI 工具，或为仅支持本地 stdio 的工具做轻量转发 |

---

### 场景一：个人本机使用（桌面 App 或 Web 端）

- **方式 A：桌面 App（推荐）**  
  打开 `wiki-context.app`，把本地资料文件夹拖进去（支持 Markdown、PDF、Word、Excel、PPT），在右上角「整理模型设置」填入大模型 API Key 即可后台自动整理摘要、监听文件变动并托盘常驻。进入 **「设备与客户端」** 页会自动扫描本机已安装的 **19 款主流 AI 工具**并保留原有配置，点一下即可完成连接（支持随时查看或单独断开）：
  - **IDE 编辑器**：**Cursor、VS Code (Copilot)、Windsurf、Trae、Kiro、Cline、Roo Code、Antigravity (AGY)、Zed**
  - **命令行 CLI**：**Claude Code、Codex CLI、Gemini CLI、Qwen Code、Kimi CLI、OpenCode**
  - **桌面客户端**：**Claude Desktop、WorkBuddy、LM Studio、DeepSeek Harness (DSH)**
- **方式 B：Web 控制中心（`wctx ui`）**  
  不装桌面端也可以直接跑命令启动内置 Web 管理界面（界面源码已打包在 Python 包内，**无需安装 Node.js 或单独部署前端**）：
  ```bash
  wctx ui                # 默认监听 http://127.0.0.1:8765 并自动打开浏览器
  wctx ui --port 8765 --no-browser
  ```
- **即使关掉桌面 App / Web 端**：本机 AI 工具如果是直连模式（`wctx serve`），依然能直接查询本地索引；只有需要**自动监听文件变动、AI 自动整理新文档**或**共享给其他电脑**时，才需要保持软件运行。

> **关于 `poppler` 依赖说明**：`wiki-context` 解析 Word、Excel、PPT、Markdown 均零依赖；仅当知识库里包含 **`.pdf`** 文件时，会调用系统级开源工具 **`poppler`**（提供 `pdftotext` 提取文本与表格、`pdftoppm` 渲染页面给视觉工具看图）。未安装 `poppler` 时服务可正常启动，遇到 PDF 会自动跳过并提示：
> - macOS：`brew install poppler`
> - Ubuntu / Debian：`sudo apt-get update && sudo apt-get install -y poppler-utils`

---

### 场景二：个人多电脑 / 局域网共享

想在笔记本或家里电脑上直接检索主力工作机里的知识库：
1. **主力电脑（提供端）**：打开桌面 App 或 `wctx ui` → 进入 **「设备与客户端」** → 点击 **「开启局域网共享」** → 选择共享的知识库与权限角色，点击 **「生成一次性配对码」**（5 分钟有效，用完即失效）。
2. **另一台电脑（连接端）**：打开桌面 App 或 `wctx ui` → **「设备与客户端」** → 粘贴配对码点击 **「立即连接」**，会自动探测局域网或 Tailscale 最快链路，并一键同步到这台电脑上的 AI 工具。

---

### 场景三：团队服务器集中部署（管理员指南）

适用于把公司/团队知识库部署到内网或云端 Linux / macOS 服务器，统一维护索引与权限，同事只需用密钥接入。

> [!IMPORTANT]
> **安全边界：`wctx ui`（Web 管理后台）vs `wctx gateway`（生产数据网关）**
> - **`wctx ui`（默认 `8765` 端口）**：是无登录密码的本地管理后台（能浏览服务器目录、配置模型 Key、签发密钥、修改权限），**默认只允许监听 `127.0.0.1`**，切勿在公网裸开 `--allow-remote-admin`。
> - **`wctx gateway`（默认 `8090` 端口）**：是专用于团队对外服务的无界面生产网关——**不暴露任何 `/api/*` 管理接口**，仅提供 `/healthz`、`/mcp`、`/v1/*`，并强制校验 SHA-256 Bearer 密钥与目录级 ACL 权限。

#### 1. 服务器安装与体检

```bash
# 安装系统 PDF 工具（可选，有 PDF 文档时建议安装）
sudo apt-get update && sudo apt-get install -y poppler-utils

# 安装 wiki-context（要求 Python >= 3.11，零第三方 Python 依赖）
pipx install git+https://github.com/Minokun/wiki-context.git
# 或从源码 / wheel 安装：python3 -m pip install .

wctx doctor
```

#### 2. 准备知识库与构建索引

```bash
wctx init /srv/vaults/company --name "公司知识库" --layout zh
# 将文档放入 /srv/vaults/company/史料/ 后提取并建立索引
wctx sync --vault /srv/vaults/company

# （可选）调用大模型生成整理摘要并刷新索引
export WCTX_BASE_URL="https://api.deepseek.com/v1"
export WCTX_MODEL="deepseek-chat"
export WCTX_API_KEY="sk-..."
wctx compile --vault /srv/vaults/company --max-items 50
```

#### 3. 配置访问密钥与目录权限（保存即热生效）

你有两种方式管理成员密钥与文件夹权限（写入 `/etc/wiki-context/tokens.json` 或 `~/.wiki-context/tokens.json`，修改保存后**无需重启网关**立即生效）：

- **方式 A（推荐，通过 SSH 隧道安全使用 Web 端管理）**：  
  在自己电脑执行端口转发，把服务器的 `127.0.0.1:8765` 映射到本地浏览器打开，外人无法访问管理后台：
  ```bash
  # 在本地电脑执行：
  ssh -L 8765:127.0.0.1:8765 user@your-server
  # 在服务器上执行：
  wctx ui --no-browser --port 8765
  ```
  浏览器访问 `http://127.0.0.1:8765` 添加知识库、在「共享权限」配置角色目录树并生成密钥（也可以用 Nginx 套 HTTPS + `auth_basic` 登录密码再长期映射 Web 端）。
- **方式 B（纯命令行生成 SHA-256 密钥文件）**：
  ```bash
  # 生成随机密钥及对应的 SHA-256（明文发给同事，哈希写入服务器配置）
  python3 -c 'import secrets, hashlib; t="wctx_"+secrets.token_urlsafe(24); print("明文发给同事:", t); print("写入配置的 sha256:", hashlib.sha256(t.encode()).hexdigest())'
  ```
  写入 `/etc/wiki-context/tokens.json`（`chmod 600 /etc/wiki-context/tokens.json`）：
  ```json
  {
    "tokens": [
      {
        "uid": "admin-wxk",
        "token_sha256": "<管理员密钥的 sha256>",
        "groups": ["admin"],
        "allowed_vaults": ["*"]
      },
      {
        "uid": "partner-01",
        "token_sha256": "<成员密钥的 sha256>",
        "groups": ["partner"],
        "allowed_vaults": ["company"]
      }
    ],
    "acl": {
      "partner": {
        "allow": ["史料/公开资料/**"],
        "deny": ["史料/财务/**", "史料/涉密/**"]
      }
    }
  }
  ```

#### 4. 启动常驻服务、定时更新与暴露访问

```bash
# 先在前台测试启动（如只走 Nginx 反代建议绑 127.0.0.1；内网/Tailscale 直连可绑 0.0.0.0）
wctx gateway \
  --vault company=/srv/vaults/company \
  --token-file /etc/wiki-context/tokens.json \
  --host 127.0.0.1 --port 8090

# 一键安装为系统后台服务（Linux 生成 systemd --user，macOS 生成 launchd）
wctx service install --target gateway \
  --vault company=/srv/vaults/company \
  --token-file /etc/wiki-context/tokens.json \
  --host 127.0.0.1 --port 8090
wctx service status --target gateway

# Linux 建议开启用户级常驻，防止退出 SSH 后服务停止：
sudo loginctl enable-linger $USER
```

- **对外暴露方式**：公网域名建议在前面挂 Nginx HTTPS 反代（设置 `proxy_buffering off;` 并透传 `Authorization`，详见 [网关与 Nginx 配置文档](docs/gateway-and-acl.md)）；公司内网推荐直接用 **Tailscale** 组网直连 `8090` 端口，免配证书。
- **验收**：`curl http://127.0.0.1:8090/healthz` 返回 `200`，再测 `curl -H "Authorization: Bearer wctx_..." http://127.0.0.1:8090/v1/vaults`。
- **定时增量更新索引**：索引采用不可变快照原子热切换，更新全程**不中断在线检索**，在 `crontab -e` 加一行即可：
  ```cron
  */15 * * * * wctx sync --vault /srv/vaults/company >> ~/.wiki-context/sync.log 2>&1
  ```

---

### 场景四：仅通过 MCP 接入团队服务器（普通成员，无需部署服务）

如果公司已经在服务器上部署好 `wctx gateway`（假设地址为 `https://kb.example.com` 或内网 `http://192.168.1.25:8090`），其他成员只管从自己电脑用 AI 工具调 MCP 查资料即可，有两种接入方式：

#### 方式 4A：零安装直连 Streamable HTTP（推荐）

适用支持远端 HTTP MCP 的客户端（**Claude Code、WorkBuddy、Cursor 及支持 Streamable HTTP 的自研 Agent**）——**成员电脑不用装 Python、`wiki-context` 或桌面 App**，向管理员获取「库地址」与「个人密钥 `wctx_...`」后直接配置即可（默认库用 `/mcp`，指定库用 `/mcp/<vault_id>`）：

- **Claude Code**（一行命令或写入 `~/.claude.json`）：
  ```bash
  claude mcp add --transport http company-kb https://kb.example.com/mcp/company \
    --header "Authorization: Bearer wctx_你的个人密钥"
  ```
  对应的 `~/.claude.json`：
  ```json
  {
    "mcpServers": {
      "company-kb": {
        "type": "http",
        "url": "https://kb.example.com/mcp/company",
        "headers": {
          "Authorization": "Bearer wctx_你的个人密钥"
        }
      }
    }
  }
  ```
- **WorkBuddy**：
  ```json
  {
    "mcpServers": {
      "company-kb": {
        "type": "streamable-http",
        "url": "https://kb.example.com/mcp/company",
        "headers": {
          "Authorization": "Bearer wctx_你的个人密钥"
        }
      }
    }
  }
  ```
- **Cursor / 其他标准 Streamable HTTP MCP 客户端**（在 `~/.cursor/mcp.json` 或客户端 MCP 设置中填入 `url` 与 `Authorization` 请求头）：
  ```json
  {
    "mcpServers": {
      "company-kb": {
        "url": "https://kb.example.com/mcp/company",
        "headers": {
          "Authorization": "Bearer wctx_你的个人密钥"
        }
      }
    }
  }
  ```
- **也可以用 REST JSON 接口直接调**（适合脚本、CI 或自研平台）：
  ```bash
  curl -X POST https://kb.example.com/v1/context \
    -H "Authorization: Bearer wctx_你的个人密钥" \
    -H "Content-Type: application/json" \
    -d '{"vault": "company", "question": "网关限流阈值是多少？"}'
  ```

#### 方式 4B：本地客户端仅支持 stdio / 想一键同步本机所有 AI 工具

如果使用的客户端仅支持本地 `stdio` 进程（如部分版本的 **Codex CLI**、**DeepSeek Harness**、老版本工具），或服务器挂了多个知识库懒得一个个手写配置：

- **方法 1：一键自动发现并写入本机已安装的 AI 工具**（支持上述 19 款主流客户端；安装 `wctx` CLI 后运行一行即可，或在桌面 App「设备与客户端 → 手动填写地址和密钥」）：
  ```bash
  wctx remote add --url https://kb.example.com --token wctx_你的个人密钥
  ```
  会自动拉取你密钥有权限访问的所有知识库，并无损写入本机已安装的客户端配置。
- **方法 2：手动配置 `wctx bridge`（stdio 转远端 HTTP 转发）**  
  推荐把密钥放在 `env.WCTX_TOKEN` 环境变量里，避免出现在进程命令行参数中：
  - **Codex CLI (`~/.codex/config.toml`)**：
    ```toml
    [mcp_servers.company-kb]
    command = "wctx"
    args = ["bridge", "--url", "https://kb.example.com/mcp/company"]
    env = { WCTX_TOKEN = "wctx_你的个人密钥", PYTHONUTF8 = "1" }
    ```
  - **通用 Stdio JSON（Cursor / Claude Code / 其他兼容工具）**：
    ```json
    {
      "mcpServers": {
        "company-kb": {
          "command": "wctx",
          "args": ["bridge", "--url", "https://kb.example.com/mcp/company"],
          "env": { "WCTX_TOKEN": "wctx_你的个人密钥", "PYTHONUTF8": "1" }
        }
      }
    }
    ```

---

## 知识库目录结构与创建组织指南

很多团队在落地知识库时最关心两个问题：**「文件夹到底长什么样？」**以及**「我日常该往哪放文件、哪些是自动生成的？」**。`wiki-context` 采用职责严格分离的**三层目录规范**，原生兼容中文 Obsidian 命名（`--layout zh`）与英文工程命名（`--layout en`）。

### 1. 标准知识库完整目录树

以中文布局（`wctx init ./研发知识库 --layout zh`）为例，英文布局仅目录/文件名不同（括号内为 `--layout en` 对应名称）：

```text
研发知识库/                              # 知识库根目录 (Vault Root)
├── vault.json                          # [配置] 库名、布局、单文件大小上限、排除规则、OCR 目录、本地 ACL
├── synonyms.json                       # [配置] 口语俗称 -> 标准术语单向映射表（保存即热重载生效）
├── AGENTS.md                           # [自动生成] 给 Claude Code / Codex / DSH 看的维护与入库作业规范
├── 目录.md          (index.md)          # [导航总表] 按主题分类登记 [[Wiki页面名]]，兼作 Obsidian 首页
├── 日志.md          (log.md)            # [变更流水] 只追加记录每次 init / sync / compile 操作审计
│
├── 史料/            (sources/)          # 【第 1 层：一手事实源 —— 你手动放原始文件的地方（严格只读）】
│   ├── 01-公共架构规范/                 #   ├─ 支持按部门、业务域、保密等级自由创建任意多级子目录
│   │   ├── rfc-042-网关限流规范.md      #   ├─ 文本/表格/富格式：.md / .txt / .csv / .html / .eml / .ipynb / .srt / .vtt
│   │   └── 系统总体设计v2.docx          #   └─ 办公与旧版格式：.docx / .pptx / .xlsx / .pdf / .doc / .xls / .ppt / .rtf
│   ├── 02-规格与配额表/
│   │   └── 企业版档位配额表.xlsx        #   (原生支持合并单元格展开、并排子表切分、标题横幅识别与流程图形状文本)
│   ├── 03-架构图与音视频/               #   (支持图片 VLM/OCR 按需识别、MCP 原生看图及 WCTX_ASR_CMD 音视频时间戳转写)
│   ├── 90-财务与商务底价/               #   (天然对应 PrefixACLPolicy 权限隔离目录前缀)
│   └── 99-历史废弃归档/                 #   (可在 vault.json 的 exclude_patterns 中一键屏蔽旧版干扰)
│
├── 史料_md/         (mirrors/)          # 【第 2 层：派生镜像、内嵌附图与索引 —— wctx 自动生成，请勿手改】
│   ├── .manifest.json                  #   ├─ 原件 SHA-256 与增量同步清单
│   ├── .assets/                        #   ├─ 自动抽取的文档内嵌架构图与按需渲染的 PDF 视觉页
│   ├── 01-公共架构规范/                 #   ├─ 与 史料/ 保持 1:1 相对路径镜像（.md + .json 页/表/附图坐标）
│   └── .index/                         #   └─ 分代不可变检索引擎目录（gen-<id>/bm25.sqlite，Git 建议忽略）
│
├── 知识库/          (wiki/)             # 【第 3 层：编译式知识页 —— 扁平一层 *.md 高层跨文档综述】
│   ├── 知识库总览.md (Overview.md)      #   ├─ 默认总入口页
│   └── 网关限流与SLO.md                 #   └─ 专题综述页（顶部 YAML 声明 sources 与 source_hashes）
│
└── 记忆/            (notes/)            # 【第 4 层：领域工作记忆与原件勘误收件箱 —— 隔离存储、继承原件 ACL】
    └── mem-20261122-xxxx.md            #   └─ wiki_remember 写入的实战经验或只读原件勘误钉子；wctx compile 可晋升进知识库
```

| 层级 / 文件 | 谁来写入？ | 核心职责与黄金守则 | 是否建议提交 Git？ |
| :--- | :---: | :--- | :---: |
| **`史料/` (`sources/`)** | **仅人工放入** | 唯一事实真源（Ground Truth）；引擎与 Agent **绝对只读、绝不篡改** | 中小文本规范可提交；大型二进制可用 Git LFS 或共享盘 |
| **`史料_md/` (`mirrors/`)** | **`wctx sync` 自动生成** | 结构化 Markdown 镜像、内嵌附图 `.assets/` 与 `bm25.sqlite`；100% 可重建 | **不提交 `.index/`**（甚至可忽略整个 `史料_md/`） |
| **`知识库/` (`wiki/`)** | **Agent / 人工 / `wctx compile`** | 跨多份原件提炼架构脉络、结论对比与避坑指南；**保持扁平一层 `*.md`** | **强烈建议提交**（纯文本，PR 可审计知识演进） |
| **`记忆/` (`notes/`)** | **`wiki_remember` / `wctx remember`** | 沉淀会话期领域踩坑经验与只读原件 `📌 勘误钉子`（自动继承关联原件 ACL，绝不污染一手史料；跨项目个人偏好仍交由 `magpie-memory` 管理） | **建议提交**（纯文本 Markdown，支持 `wctx compile` 巩固晋升进 Wiki） |
| **`目录.md` / `vault.json` / `synonyms.json`** | **Agent / 人工 / `wctx synonyms mine`** | 专题索引树、库级策略配置与团队口语同义词表 | **强烈建议提交** |

---

### 2. 从零搭建与日常维护的 5 个标准步骤

#### 第一步：创建知识库骨架（或零配置接入存量目录）
- **全新创建**：指定一个**尚不存在或为空**的目标路径执行 `wctx init`（为防止误覆盖已有资料，非空目录会安全报错）：
  ```bash
  # 中文 Obsidian 风格（推荐国内团队）
  wctx init ./研发知识库 --name "研发技术规范库" --layout zh

  # 或英文工程风格
  wctx init ./eng-kb --name "Engineering KB" --layout en
  ```
- **存量文件夹直接挂载**：如果你手里已经有一个包含 `史料/`（或 `sources/`）的文件夹，**甚至不需要运行 `init` 或手写 `vault.json`**——直接对其运行 `wctx sync --vault <目录路径>`，引擎会自动识别中/英文布局并补齐镜像与索引层。

#### 第二步：规划 `史料/`（`sources/`）子目录并放入原始文档
你可以按习惯在 `史料/` 下建立任意深度的文件夹，推荐遵循**「按业务域 + 保密边界 + 生命周期分目录」**原则：
1. **目录结构即权限边界**：将来启用多人网关时，`PrefixACLPolicy` 直接基于 `史料/` 下的子目录前缀做授权（如给合作方开放 `"allow": ["史料/01-公共架构规范/**"]`，同时锁死 `"deny": ["史料/90-财务与商务底价/**"]`），提前分好文件夹即可零成本开启细粒度权限。
2. **旧版归档免删隔离**：文档迭代产生旧版（如 `v1` 废弃方案）时，不必物理删除，直接移入 `史料/99-历史废弃归档/`，并在 `vault.json` 中加上 `"exclude_patterns": ["99-历史废弃归档/"]`，即可让旧文档退出检索且保留审计底稿。
3. **内置文件准入与防污染规则（无需人工挑拣杂物）**：
   - **自动提取入库**：`.md`、`.markdown`、`.txt`、`.rst`、`.csv`、`.tsv`、`.docx`、`.pptx`、`.xlsx`、`.pdf`（安装 `poppler` 后自动提取文本与页界）。
   - **自动安全跳过**：私钥与凭证文件（`.env`、`*.pem`、`*.key`、`id_rsa`、`credentials.json`）、机器日志与数据库（`.log`、`.json`、`.yaml`、`.sql`、`.sqlite`）、压缩包与音视频、隐藏目录（`.git`、`.obsidian`、`*.assets`）以及超限单文件（默认 `>2 MiB`，可在 `vault.json` 调大 `max_doc_bytes`）会被自动拦截，并在 `wiki_status` 中透明列出跳过原因；完全相同的重复文件（如 `规范 (1).docx`、`规范_副本.docx`）会自动折叠去重。
   - **架构图与扫描件按需 OCR**：默认跳过普通 `.png`/`.jpg` 配图防噪声；若某目录专门存放扫描件或架构大图，在 `vault.json` 配置 `"ocr_image_dirs": ["03-架构图与扫描件"]` 并指定 `WCTX_OCR_CMD` 即可定向提取。

#### 第三步：运行 `wctx sync`（此时知识库已立即可用！）
```bash
wctx sync --vault ./研发知识库
```
> **渐进式启用提示**：你**不需要**在建库第一天就写满 `知识库/*.md`！只要跑完 `wctx sync`，所有一手原件和 Excel/CSV 长表格就已经完成表格感知切块并生成了 `bm25.sqlite` 索引，此时接入 MCP 就已经能精准查表、溯源原件并拦截超纲幻觉了。

#### 第四步：沉淀 `知识库/`（`wiki/`）综述页与配置 `synonyms.json`
随着资料增多，当同一个业务主题分散在多份文档中时，在 `知识库/` 下维护**扁平一层**的 Markdown 专题页（不建子文件夹，分类层级统一写在根目录 `目录.md` 中）：

- **单篇 Wiki 页面标准格式（`知识库/网关限流与SLO.md`）**：
  ```markdown
  ---
  title: "网关限流与SLO"
  type: synthesis
  status: reviewed
  updated: 2026-11-22
  sources:
    - 史料/01-公共架构规范/rfc-042-网关限流规范.md
    - 史料/02-规格与配额表/企业版档位配额表.xlsx
  source_hashes:
    "史料/01-公共架构规范/rfc-042-网关限流规范.md": "aa2a7d5bd5ed..."
    "史料/02-规格与配额表/企业版档位配额表.xlsx": "3fb80b1f5dc2..."
  ---

  # 网关限流与SLO

  ## 核心结论
  - 边缘节点采用本地原子令牌桶，每 `250ms` 同步一次计数；超限统一返回 `HTTP-429` 及 `Retry-After` 响应头。
  - 各商业化档位的精确 RPS 与突发上限以 `史料/02-规格与配额表/企业版档位配额表.xlsx` 原表为准。

  ## 关联页面
  [[知识库总览]]; [[容器运行时与容灾切换]]
  ```
  *（注：`source_hashes` 无需手算，运行 `wctx compile` 或 `wctx freshness --vault ./研发知识库 snapshot` 会自动计算并写回。）*

- **配置团队黑话与口语同义词（`synonyms.json`，保存即热生效）**：
  ```json
  {
    "version": 1,
    "groups": [
      {
        "canonical": ["HTTP-429", "令牌桶限流"],
        "aliases": ["被卡流了", "接口报429", "请求太频繁", "throttled"]
      }
    ]
  }
  ```

- **`vault.json` 常用配置示例**：
  ```json
  {
    "name": "研发技术规范库",
    "layout": "zh",
    "max_doc_bytes": 5242880,
    "sensitive": false,
    "exclude_patterns": ["99-历史废弃归档/"],
    "ocr_image_dirs": ["03-架构图与扫描件"],
    "acl": {
      "partner": {
        "allow": ["史料/01-公共架构规范/**"],
        "deny": ["史料/90-财务与商务底价/**"]
      }
    }
  }
  ```

#### 第五步：原件变更时的日常更新闭环
当团队往 `史料/` 新增了文档，或直接覆盖更新了某份旧 Word/Excel 原件后：
1. 执行 `wctx sync --vault ./研发知识库`：增量提取变更原件；若某篇旧 Wiki 引用了被修改的原件，索引器会自动识别哈希失配并将其标为 `stale`（自动 `0.5x` 降权 + 附带过期警告），**绝不会用过期总结静默误导 Agent**。
2. 更新编译层（二选一）：
   - **全自动**：运行 `wctx compile --vault ./研发知识库`，自动编译新原件并刷新所有 `stale` 页面、更新哈希、跑 `lint` 并热切换索引；
   - **Agent 辅助精修**：在知识库目录让 Claude Code / Codex / DSH 更新对应 Wiki 页与 `目录.md`，随后执行：
     ```bash
     wctx freshness --vault ./研发知识库 snapshot
     wctx lint --vault ./研发知识库
     wctx index --vault ./研发知识库
     ```
     正在运行的 `wctx serve` 或 `wctx gateway` 会在下一次请求时毫秒级无感热切换到新索引，无需重启服务。

---

## 接入 AI Agent（MCP 工具与配置）

`wiki-context` 向宿主 Agent 暴露 **9 个职责分明、权限受控的 MCP 工具**（多库同时挂载时会自动在工具描述前注入 `[知识库名称]` 与领域词提示，防止 Agent 调错库）：

| MCP 工具 | 用途说明 |
| :--- | :--- |
| **`wiki_context`** | **首选主力工具（95% 问答只需调一次）**：自动完成同义词对齐、Wiki 导读 + 一手原件检索、表头置顶、附图提示、1-hop 原件勘误钉子与缺词防幻觉告警 |
| `wiki_inspect_visual` | **原生多模态看图**：将文档内嵌架构图、独立图片或按需渲染的 PDF 视觉页以标准 MCP `image`（base64）内容块返回给视觉 Agent 直接审阅 |
| `wiki_remember` | **领域工作记忆与原件勘误写入**：向第 4 层 `记忆/`（`notes/`）记录实战踩坑经验或只读原件 `errata` 勘误钉子（自动脱敏并继承关联原件 ACL） |
| `wiki_forget` | 归档撤回已过时或有误的第 4 层工作记忆笔记 |
| `wiki_search` | 细粒度 BM25/FTS5 检索，支持按 `section`（章节）、`page`（页/Sheet）、`wiki`（知识页）过滤 |
| `wiki_read` | 沙箱安全读取派生镜像、Wiki 页或 `目录.md` 的精确行区间（单次 `<=400` 行）或指定 `page_num` |
| `wiki_route` | 判断问题属于单点事实直答（`fact` 2.6k 预算）还是多跳综合分析（`complex` 4.8k 预算） |
| `wiki_list` | 列出知识库已索引文档清单与提取方式（支持路径子串过滤） |
| `wiki_status` | 查看当前索引代数、文档/切片规模、活跃记忆条数、跳过文件统计与未镜像资料边界 |

### 方式一：本机单用户 Stdio 接入（Claude Code / Cursor / Codex / DSH）

**Claude Code (`~/.claude.json`) / Cursor (`~/.cursor/mcp.json`) / DeepSeek Harness**：

```json
{
  "mcpServers": {
    "wiki": {
      "command": "wctx",
      "args": ["serve", "--vault", "/absolute/path/to/我的知识库"],
      "env": { "WCTX_USER": "dev" }
    }
  }
}
```

**Codex CLI (`~/.codex/config.toml`)**：

```toml
[mcp_servers.wiki]
command = "wctx"
args = ["serve", "--vault", "/absolute/path/to/我的知识库"]
env = { WCTX_USER = "dev" }
```

### 方式二：团队多人多库 HTTP 网关（`wctx gateway`）

一个轻量进程同时托管多个知识库，支持 SHA-256 Bearer Token 鉴权与保存即热生效的目录前缀权限（`PrefixACLPolicy`）：

```bash
WCTX_TOKENS="alice:secret_1,bob:secret_2" \
wctx gateway \
  --vault tech=./研发技术库 \
  --vault rules=./制度规范库 \
  --host 127.0.0.1 --port 8090
```

- **Streamable HTTP MCP（`2025-03-26`）**：默认库 `POST http://<host>:8090/mcp`，按库独立挂载 `POST http://<host>:8090/mcp/tech`、`/mcp/rules`；支持客户端零安装直连（`Authorization: Bearer <token>`）。
- **REST JSON 接口**：`POST /v1/context`、`POST /v1/read`、`GET /v1/vaults`、`GET /v1/whoami`、`GET /healthz`。
- **一键接入与 Stdio 桥接**：成员端可运行 `wctx remote add --url http://<host>:8090 --token <token>` 一键同步本机全部 AI 客户端，或通过 `wctx bridge --url http://<host>:8090/mcp/tech`（配合 `WCTX_TOKEN` 环境变量）桥接仅支持本地 stdio 的工具。详情参考上方「场景三 / 场景四」与 [网关与权限指南](docs/gateway-and-acl.md)。

---

## 离线 Wiki 编译整理（两种工作流自由组合）

1. **人机交互式精修（推荐高价值核心库）**：在知识库根目录打开 Claude Code / Codex / DSH，直接对 Agent 说「刚放了一批新规范，帮我整理进知识库」。Agent 会按照初始化生成的 `AGENTS.md` 规范，自动完成 `wctx sync → 提炼更新知识库页与目录 → freshness snapshot → lint 校验 → 重建索引`，你随时可在 Obsidian 中审阅双向链接图谱。
2. **命令行批量自动编译（`wctx compile`）**：内置零 SDK 编译引擎，直连任意 OpenAI 兼容接口（DeepSeek、OpenAI、本地 vLLM/Ollama 等）或 Anthropic `/v1/messages` 接口，自动识别尚未归档的新镜像与已标 `stale` 的过期 Wiki 页：

```bash
# 先预览待编译的新镜像与因原件修改而过期（stale）的 Wiki 页（不消耗 API）
wctx compile --vault ./我的知识库 --dry-run

# 调用大模型批量编译、自动记录 SHA-256 source_hashes、执行 lint 并热切换 FTS5 索引
WCTX_API_KEY="sk-..." \
WCTX_BASE_URL="https://api.deepseek.com/v1" \
WCTX_MODEL="deepseek-chat" \
wctx compile --vault ./我的知识库
```

---

## CLI 命令速查（`wctx`）

| 命令 | 功能说明 |
| :--- | :--- |
| `wctx ui [--port 8765]` | 启动本机可视化 Web 控制中心（仅绑定 `127.0.0.1`，管理文件夹、令牌、多端 MCP、记忆 + 开发网关） |
| `wctx doctor [--json]` | 一键环境体检（Python、全局 `wctx` 路径、`pdftotext`/`pdftoppm`、AI 客户端与后台守护状态） |
| `wctx service [status\|render\|install\|uninstall]` | 生成或安装 macOS `launchd` / Linux `systemd --user` 后台守护服务（推荐 `--target gateway`） |
| `wctx init <path> [--layout en\|zh]` | 初始化中/英文四层结构知识库与 `AGENTS.md` 维护规约 |
| `wctx sync --vault <V> [--list]` | 增量提取变更原件并重建不可变 FTS5 索引 |
| `wctx index --vault <V> [--check --deep]` | 构建或深度校验 `gen-<id>/bm25.sqlite` 分代索引快照 |
| `wctx context --vault <V> -q "<Q>"` | 终端单次组装带精确引用的 `<wiki_context>` 证据包 |
| `wctx search --vault <V> -q "<Q>"` | 执行底层双层 BM25/FTS5 检索或 `--route` 复杂度判定 |
| `wctx compile --vault <V> [--dry-run]` | 批量编译未覆盖镜像、刷新 `stale` 页并将第 4 层记忆巩固进 Wiki |
| `wctx remember --vault <V> -t "<T>"` | 向第 4 层 `记忆/` 写入实战经验或 `--kind errata` 原件勘误钉子 |
| `wctx notes [list\|forget\|import] --vault <V>` | 查看、归档撤回或从外部 JSON（如 `magpie-memory` 导出）导入领域记忆 |
| `wctx synonyms mine --vault <V> [--apply]` | 自动从库内术语对照表与双语括号定义中挖掘同义词组写入 `synonyms.json` |
| `wctx eval --vault <V> [--questions Q.json]` | 自助运行严格证据召回率、MRR、延迟与防幻觉护栏量化基准评测 |
| `wctx freshness --vault <V> [check\|snapshot]` | 基于原件 SHA-256 检测过期 Wiki 页或更新哈希快照 |
| `wctx lint --vault <V>` | 机械检查死链 `[[wikilinks]]`、缺失引用原件与目录漂移 |
| `wctx serve --vault <V> [--test]` | 启动本地 Stdio MCP 服务（9 个工具，默认开启凭证脱敏） |
| `wctx gateway --vault id=path [--test]` | 启动多人多库 Streamable HTTP MCP + REST 知识网关 |
| `wctx remote [add\|list\|probe\|remove]` | 填入远程电脑网关地址与令牌（`--url` + `--token`），自动发现授权库并一键挂载至本机 AI 客户端 |
| `wctx bridge --url <URL> --token <T>` | 将本地 Stdio MCP 客户端桥接至远端 HTTP 网关 |

---

## 详细技术文档

- [核心架构与五步确定性检索供料原理](docs/architecture.md)
- [知识库目录规范、单向同义词与 SHA-256 新鲜度机制](docs/vault-spec.md)
- [多库 HTTP 网关、Token 鉴权与目录前缀权限（ACL）指南](docs/gateway-and-acl.md)

## 开源协议

[MIT License](LICENSE)
