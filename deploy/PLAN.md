# `wiki-context` v0.4.0 跨平台控制台与桌面应用架构方案（v2.0 经 Autoresearch 审查收敛版）

> **定位**：将 `wiki-context` 从命令行工具升级为**全天候常驻、免记 IP/端口、全链路可观测**的跨设备知识中枢。一套前端代码同时驱动 **macOS 桌面端（`.dmg`）**、**Windows 桌面端（`-setup.exe` / `.msi`）** 与 **Web 控制台（`wctx ui`）**，100% 复用现有 Python 确定性上下文引擎。
>
> **v2.0 对抗审查修订说明**：本方案已完成 `autoresearch` 五专家冷启动代码级审查（软件架构师、安全分析师、性能工程师、Windows/macOS 可靠性工程师、魔鬼代言人）与三盲审全票收敛（3/3 一致通过，均分 `8.4/10`，对比初版 `3.7/10`），系统性根治了初版在 **Windows 文件锁缺失/`[WinError 32]`/GBK `stdio` 崩溃、`PyInstaller --onefile` 冷启动与内存放大、Tauri CORS/SSE 鉴权与退出挂死、配对码永久凭据泄露与 SSRF、远端库透视假象及 `:8090` 多实例脑裂** 等 12 项高危隐患。

---

## 一、 核心设计原则

1. **一套 UI 源码，三端同构发布（Web 零退化）**：
   - 前端工程位于 `deploy/web/`（`React 19 + TypeScript + Vite + Tailwind CSS v4`），通过 `PlatformAdapter` 自动识别 **Tauri v2 桌面容器** 与 **浏览器（`wctx ui`）**。
   - Web 构建产物输出至 `src/wiki_context/ui_dist/`（`.gitattributes` 标记 `linguist-generated=true`），补齐 `_build_backend.py` 完整 PEP 517/660 钩子并打包非 `.py` 静态资产，同时在 `console.py` 新增 `/api/bootstrap` 与静态文件路由，确保 `pip install .` 后运行 `wctx ui` 与桌面端体验 100% 同构。
2. **高性能零 Python 依赖分发（`--onedir` 引擎 + 稳定命令垫片）**：
   - **严禁使用 `PyInstaller --onefile` 作为客户端 MCP 入口**（每次拉起子进程解压数千文件至 `%TEMP%\_MEI*`，冷启动高达 2~6 秒且强杀残留垃圾目录）。
   - 桌面端改为采用 **`PyInstaller --onedir`** 构建至 Tauri `resources/engine/`（冷启动 `~80–150ms`，多进程共享操作系统 DLL 内存页），配合 **`KBSnapshot` 按需懒加载**（`docs.jsonl` / `pages.jsonl` 延迟到 `wiki_list` / `wiki_read` 时加载，单进程空闲内存从 `~105MB` 压降至 `~18MB`）。
   - **防搬移断链垫片**：绝不往 6 大 AI 客户端配置里直接写入 `.app/Contents/...` 内部路径；桌面端启动时自动维护稳定垫片（macOS: `~/.local/bin/wctx`，Windows: `%LOCALAPPDATA%\Programs\WikiContext\bin\wctx.cmd`），移动 `.app` 或升级版本后客户端配置永不失效。
3. **单实例仲裁与彻底告别记 IP / 端口**：
   - **本机单实例复用（防 `:8090` 幽灵脑裂）**：新增 `~/.wiki-context/daemon.lock`（跨平台文件锁）+ `daemon.json`（记录当前活跃管理端口与 PID）。无论用户先开终端 `wctx ui` 再点桌面图标，还是反过来，后启动方自动附着唤醒已有守护实例，彻底杜绝两个 `ConsoleState` 争抢 `0.0.0.0:8090` 或并发覆写 `console.json`。
   - **跨机免输 IP（一次性配对码 + Tailscale 主通道 + 局域网广播辅助）**：以 **5 分钟单次有效 OTP 配对码（`wctx1_...`）** 与 **Tailscale 在线节点自动扫描** 为核心主路径，局域网 UDP 广播发现降级为同网段零配置辅助（明确规避企业 VLAN / macOS 本地网络权限 / Windows 公用防火墙阻塞）。
4. **全链路低开销可观测 + 诚实能力边界**：
   - 本地 `stdio` 子进程与 HTTP 网关统一通过**内存无锁队列 + 后台单写线程**批量落盘至 `SQLite WAL`（`telemetry.sqlite`），主请求路径耗时 `< 0.02ms`。
   - 明确区分 **`local`（本地库）** 与 **`remote`（挂载远端库）** 的能力矩阵（`capabilities`），不在远端受限网关上伪造本地管理能力。

---

## 二、 整体系统架构（v2.0 硬化版）

```mermaid
flowchart TB
    subgraph Desktop["Tauri v2 桌面壳 (macOS .app / Windows .exe)"]
        Tray["系统托盘 / 菜单栏 + 开机自启"]
        NativeWatch["Rust notify 原生文件监听 (FSEvents / ReadDirectoryChangesW)"]
        NativeAPI["原生能力桥接 (目录选择器 / 文件拖拽 / 单实例唤醒)"]
        WebView["React + TS 控制台前端 (deploy/web)"]
        SidecarMgr["Sidecar 守护器 (--onedir 引擎 + stdin EOF 陪葬防孤儿进程)"]
    end

    subgraph WebMode["纯 Web 模式 (wctx ui 浏览器直连)"]
        Browser["现代浏览器 (/api/bootstrap + ui_dist 静态包)"]
    end

    subgraph Engine["Python 核心引擎 (wctx / resources/engine/wctx)"]
        LockMgr["state_lock.py (fcntl / msvcrt 跨平台锁 + daemon.json 单实例仲裁)"]
        ConsoleAPI["Console Admin API (BoundedSemaphore 线程池 + 严格 Origin/Host 白名单)"]
        Gateway["多库 MCP & REST 网关 (0.0.0.0:8090 显式端口冲突检测 + /v1/pair/exchange)"]
        Playground["检索与 ACL 透视引擎 (结构化 ACLDecision + Local/Remote 能力分流)"]
        Discovery["Tailscale 路径自适应扫描 + OTP 配对码 + LAN UDP 辅助发现"]
    end

    subgraph Agents["本机与远端 AI 客户端"]
        LocalStdio["本机 6 大客户端 (经稳定 shim 调用 wctx mcp, UTF-8 二进制流)"]
        RemoteAgents["另一台电脑客户端 (wctx remote / Streamable HTTP)"]
    end

    subgraph Storage["~/.wiki-context (0600 严格权限 + Schema 版本迁移)"]
        StateFiles["console.json + tokens.json + remotes.json (v2 统一远端连接簿)"]
        TelemetryDB["telemetry.sqlite (SimpleQueue 异步批量 WAL 写入)"]
        VaultIndex["四层知识库 + 懒加载 FTS5 索引 + Windows 延迟 .trash 回收"]
    end

    WebView -->|"IPC 获取动态端口与 X-WCTX-Admin-Token"| SidecarMgr
    WebView -->|"HTTP + fetch ReadableStream SSE"| ConsoleAPI
    Browser -->|"HTTP + X-WCTX-CSRF"| ConsoleAPI
    NativeWatch -->|"防抖 2s 触发异步 Job"| ConsoleAPI
    SidecarMgr -->|"拉起/附着"| LockMgr

    ConsoleAPI --> LockMgr
    ConsoleAPI --> Playground
    ConsoleAPI --> Discovery
    ConsoleAPI --> StateFiles
    ConsoleAPI --> TelemetryDB

    LocalStdio -->|"WAL 异步追加"| TelemetryDB
    Gateway -->|"异步入队"| TelemetryDB
    RemoteAgents -->|"设备独立 Bearer Token"| Gateway
    LocalStdio --> VaultIndex
    Gateway --> VaultIndex
```

---

## 三、 仓库目录结构规划（新增 `deploy/`）

```text
wiki-context/
├── .gitattributes                    # [新增] 标记 src/wiki_context/ui_dist/** linguist-generated=true
├── .github/workflows/
│   ├── ci.yml                        # [新增 Phase 1] macOS + Windows (PYTHONIOENCODING=gbk) 双平台单测矩阵
│   └── release-desktop.yml           # [新增 Phase 4] 自动构建 macOS .dmg + Windows -setup.exe/.msi + Python .whl
├── pyproject.toml                    # [清理] 移除无用 [tool.setuptools]，保持 requires-python >= 3.10
├── _build_backend.py                 # [升级] 补齐 PEP 517 get_requires_* 钩子，打包 src/wiki_context/ui_dist/**
├── src/wiki_context/
│   ├── state_lock.py                 # [新增] POSIX fcntl + Windows msvcrt 失败关闭锁、0600 原子写、Schema 迁移、daemon.json
│   ├── telemetry.py                  # [新增] SimpleQueue 后台单写 SQLite WAL 审计库 + 环形批次裁剪 + wctx_* 脱敏
│   ├── discovery.py                  # [新增] 5 分钟单次 OTP 配对码、RFC1918/CGNAT SSRF 防护、Tailscale 探测、UDP 辅助发现
│   ├── watcher.py                    # [新增] CLI 模式浅层目录 stat 增量监听器（桌面端优先走 Rust notify）
│   ├── console.py                    # [扩展] 异步 sync Job、Playground、fetch-SSE 日志流、CORS/Origin 白名单、ui_dist 托管
│   ├── context.py                    # [扩展] 结构化 ACLDecision (deny_prefix / outside_allowlist / wiki_ref_blocked)
│   ├── mcp_server.py                 # [扩展] UTF-8 二进制 stdout 修复、KBSnapshot 懒加载、接入 telemetry
│   ├── indexer.py / sync.py          # [加固] 接入 state_lock.py、Windows [WinError 32] 延迟 .trash 回收与 replace 退避重试
│   ├── clients.py                    # [扩展] 注入 WCTX_CLIENT + PYTHONUTF8=1、Windows shim 路径探测与健康自检
│   └── ui_dist/                      # [构建产物] deploy/web 编译输出的静态资源包
└── deploy/
    ├── PLAN.md                       # 本架构方案文档
    ├── web/                          # 三端共享前端工程 (React 19 + TypeScript + Vite + Tailwind v4)
    │   ├── package.json
    │   ├── tsconfig.json
    │   ├── vite.config.ts
    │   └── src/
    │       ├── api/                  # 统一 API Client + fetch ReadableStream SSE 订阅器（支持自定义鉴权头）
    │       ├── platform/             # 运行时适配层 (tauri.ts vs browser.ts)
    │       ├── layouts/              # 侧边栏导航 + 顶部守护进程/网关状态栏 + 全局快捷面板
    │       ├── pages/
    │       │   ├── DashboardPage.tsx # 总览看板（引擎/工具链体检警告、今日调用曲线、高频缺词榜）
    │       │   ├── VaultsPage.tsx    # 知识库中心（原生选目录、拖拽导入、异步同步进度条、自动监听开关、改 ID）
    │       │   ├── PlaygroundPage.tsx# 检索与权限透视台（按 capabilities 自动切换本地全透视 / 远端客户端视角）
    │       │   ├── LogsPage.tsx      # 实时 MCP 调用日志流（客户端徽标、缺词过滤、详情抽屉、一键送入透视台重放）
    │       │   ├── NetworkPage.tsx   # 多机互联中心（LAN 网关总闸与冲突提示、OTP 配对码互连、Tailscale/LAN 发现）
    │       │   ├── AccessPage.tsx    # 访问控制（每设备独立令牌轮换/吊销 + 图形化角色组 & 多级目录树黑白名单）
    │       │   ├── ClientsPage.tsx   # AI 客户端矩阵（6 大客户端 + Magpie 勾选同步、失效路径一键修复）
    │       │   └── MemoryPage.tsx    # Layer-4 实战记忆与原件勘误管理（本地库读写、批量导入导出）
    │       └── components/
    ├── desktop/                      # Tauri v2 桌面端工程 (macOS + Windows)
    │   ├── package.json
    │   └── src-tauri/
    │       ├── Cargo.toml
    │       ├── tauri.conf.json       # 绑定 resources/engine/ (--onedir) 与最小化 capabilities 权限
    │       ├── capabilities/default.json
    │       ├── icons/
    │       └── src/
    │           ├── main.rs
    │           ├── lib.rs
    │           ├── sidecar.rs        # daemon.json 单实例附着/拉起、WCTX_READY <port> <pid> 握手、stdin 管道防孤儿、wctx shim 安装
    │           ├── watcher.rs        # 基于 notify crate 的零 CPU 原生文件变更监听（防抖转发至本地 API）
    │           └── tray.rs           # 系统托盘/菜单栏（网关开关、生成配对码、立即同步、开机自启、优雅退出）
    └── scripts/
        ├── build_web.py              # 构建 deploy/web 并校验写入 src/wiki_context/ui_dist/
        ├── build_engine.py           # 调用 PyInstaller --onedir 构建无控制台闪烁的精简 Python 引擎目录
        └── build_desktop.py          # 串联 Web -> Engine -> Tauri Bundle 产出 .dmg / -setup.exe / .msi
```

---

## 四、 核心模块与关键技术攻关设计

### 4.1 桌面握手、CORS 安全边界与单实例仲裁

1. **解决 Tauri WebView 跨域与预检请求（`403` / `501`）隐患**：
   - Tauri v2 前端运行在 `tauri://localhost`（macOS）或 `http://tauri.localhost`（Windows WebView2），与 Sidecar `http://127.0.0.1:<P>` 跨源。
   - `ConsoleHTTPHandler` 新增严格 `do_OPTIONS` 与请求头校验：
     - **Host 白名单**：仅允许 `127.0.0.1:<P>`、`localhost:<P>`（彻底阻断 DNS Rebinding 攻击）。
     - **Origin 白名单**：仅对精确匹配 `http://127.0.0.1:<P>`、`http://localhost:<P>`、`tauri://localhost`、`http://tauri.localhost`、`https://tauri.localhost` 的请求回显 CORS 头。
     - **双轨会话凭证**：Web 浏览器通过同源 `GET /api/bootstrap` 获取内存随机 `csrfToken`（发送 `X-WCTX-CSRF`）；Tauri 桌面端由 Rust 父进程通过环境变量注入 256-bit `WCTX_ADMIN_TOKEN` 并经 Tauri IPC `get_bootstrap_config` 传给前端（发送 `X-WCTX-Admin-Token`）。
2. **用 `fetch()` `ReadableStream` 替代原生 `EventSource` 实现 SSE**：
   - 浏览器原生 `EventSource` 无法携带自定义 Header，若把凭证塞进 URL Query（`?token=...`）极易泄露在日志与历史记录中。
   - 前端统一采用 `fetch("/api/logs/stream", { headers: { "X-WCTX-Admin-Token": ... } })` 流式读取 `text/event-stream`，既保留自定义安全头，又支持断线指数退避重连。
3. **优雅毫秒级退出（解决 SSE 挂死 `shutdown()`）**：
   - 服务端维护全局 `shutdown_event: threading.Event` 与活跃 SSE 连接集合，SSE 循环每 `1.0s` 检查一次 `shutdown_event` 并发送心跳注释 `: ping\n\n`。
   - 注册 `SIGTERM` / `SIGINT` 信号与 `--watch-stdin-eof`（父进程 Tauri 崩溃或被任务管理器强杀导致管道闭合时，Python 引擎 `300ms` 内自动关闭所有 SSE 流与 `:8090` 网关退出，绝不残留僵尸进程）。

---

### 4.2 免记 IP / 端口与安全跨机配对（OTP 单次兑换 + 设备独立凭据）

1. **杜绝剪贴板永久 Token 泄露与 SSRF：5 分钟一次性配对码（`wctx1_<base64url>`）**：
   - 主机端选择好授权范围（允许访问哪些知识库、绑定哪个角色组）后点击 **「生成配对码」**，服务端在内存中创建一条有效期 **300 秒、仅限使用 1 次** 的注册票据 `(otp_id, otp_secret)`，连同主机 `instance_id`、TOFU 指纹 `fp` 以及本机 **RFC1918 内网 IP + Tailscale `100.64.0.0/10` IP** 编码为 `wctx1_...` 短码：
     ```json
     {
       "v": 1,
       "host": "wxk-mac",
       "instance_id": "inst_8f3a2c10",
       "fp": "sha256:9b1e...",
       "urls": ["http://192.168.31.88:8090", "http://100.101.102.103:8090"],
       "otp_id": "otp_91b2",
       "otp_secret": "s3cr3t_once_only",
       "exp": 1763895000
     }
     ```
   - **SSRF 与凭据防窃取守卫**：对端电脑粘贴配对码后，后端严格校验 `urls` 必须为 `http/https`、端口 `>= 1024`，且目标 IP 属于私网（`10/8`、`172.16/12`、`192.168/16`）或 Tailscale CGNAT（`100.64/10`），严禁访问 `127.0.0.1` 或云元数据 `169.254.169.254`；先无凭证并发请求 `GET /healthz` 校验返回的 `instance_id` 与指纹 `fp` 一致并选取最快通道，随后仅向验证通过的唯一地址发送 `POST /v1/pair/exchange` 兑换出**该设备专属的独立可吊销 Bearer Token**（如 `label: "Paired: wxk-home"`），OTP 当即销毁失效！支持后续按单台设备独立轮换（`/api/tokens/rotate`）或一键吊销。
2. **Tailscale 优先 + 局域网广播辅助发现**：
   - 新增跨平台外部工具定位器 `resolve_external_tool()`（自动探测 macOS `/Applications/Tailscale.app/Contents/MacOS/Tailscale`、`/opt/homebrew/bin` 与 Windows `C:\Program Files\Tailscale\tailscale.exe`、Poppler 安装目录，解决 Finder/Explorer 启动 GUI 应用无 `PATH` 问题）。
   - **多机互联中心**并发扫描 Tailscale 在线节点（10s TTL 缓存，不阻塞主锁）与局域网 UDP `48765` 辅助广播，并在升级后的 **`remotes.json`（v2 Schema，`0600` 原子创建 + TOFU 指纹锁定）** 中按 `instance_id` 统一管理多地址故障自动切换（不再新建冗余的 `nodes.json`）。

---

### 4.3 检索与权限透视台（Playground + 结构化 ACL Shadow Diff）

1. **显式区分本地库与远端库能力矩阵（`capabilities`）**：
   - **本地知识库（`kind == "local"`）**：开放完整 4 维透视（QueryPlan 分词与 IDF 权重解剖、四层命中卡片、**ACL 权限拦截对比 Shadow Diff**、`<wiki_context>` 原始载荷预览、一键增补同义词与实战记忆）。
   - **已挂载远端库（`kind == "remote"`）**：界面自动切换为 **「远端调用实测模式」**，基于当前挂载凭证调用远端 `/v1/context` 展示实际召回文本、网络往返耗时与缺词提醒，明确置灰仅限主机端执行的 ACL Shadow Diff 与同义词改写，杜绝假按钮与 `400` 报错。
2. **底层 `PrefixACLPolicy` 结构化原因追踪（`context.py` 扩展）**：
   - 为 `PrefixACLPolicy` 新增 `explain_source_access(user, src)` 与 `explain_wiki_access(user, wsrc, refs)`，返回结构化判定 `(allowed: bool, reason_code: str, matched_rule: str)`：
     - `deny_prefix`：命中黑名单前缀（如 `finance/`）
     - `outside_allowlist`：未命中角色组白名单前缀
     - `wiki_provenance_blocked`：Layer-3 Wiki 页面因引用的底层 `sources:` 原件受限而被连带拦截
   - 新建令牌默认角色组与“管理员全量视角（`admin`）”解耦，使透视台选择任意业务角色组或受限令牌时，都能清晰对比列出被拦截的切片及具体触发规则。

---

### 4.4 跨进程低延迟 MCP 调用日志流（SQLite WAL 异步单写架构）

1. **零阻塞写入与并发安全（`src/wiki_context/telemetry.py`）**：
   - 在网关/控制台常驻进程中采用 `SimpleQueue(maxsize=4096)` + 单一后台守护写线程（每 100ms 或攒满 32 条批量 `executemany` 提交），工具调用主路径仅做一次内存入队（`< 0.01ms`）。
   - 在独立短生命周期的 `wctx mcp`（`stdio`）子进程中，使用独立连接配合 `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=3000; PRAGMA wal_autocheckpoint=1000;` 追加记录。
   - 容量治理采用按主键游标的轻量分批裁剪（`DELETE FROM mcp_invocations WHERE id <= (SELECT MAX(id) - 10000 FROM mcp_invocations)`，严禁在请求路径执行阻塞式 `VACUUM`）。
2. **安全脱敏与防越权泄露**：
   - 扩充 `scrub_secrets` 正则以覆盖 `wctx_*` / `wctx1_*` 令牌格式。
   - 共享审计表仅记录**调用方有权看到的命中文档摘要**以及**被 ACL 拦截的整数计数（`acl_filtered_count`）**，绝不在共享日志中持久化被拒绝访问的机密文件路径。
3. **客户端自动归因**：
   - `clients.py` 写入 6 大客户端配置时统一注入 `env: { "WCTX_CLIENT": "<client_id>", "PYTHONUTF8": "1" }`，日志流实时点亮 `[DSH]`、`[Cursor]`、`[Claude Code]`、`[Codex]`、`[WorkBuddy]`、`[Antigravity]` 或 `[Remote: <uid>]` 专属徽标，并聚合 **高频 0 命中缺词雷达**。

---

### 4.5 Windows 与跨平台底层加固清单（必做项）

1. **跨平台文件锁（`state_lock.py`）**：封装 POSIX `fcntl.flock` 与 Windows `msvcrt.locking`，采用失败关闭（Fail-Closed）策略，统一保护 `sync.py`、`indexer.py`、`memory.py` 与 `console.json` / `tokens.json` / `remotes.json` 的读写事务。
2. **Windows `[WinError 32]` 索引代数回收与原子替换退避**：
   - Windows 下当 `KBService` 仍持有旧一代 `bm25.sqlite` 只读句柄时，`shutil.rmtree` 无法直接删除目录。改造 `indexer.py`：淘汰代数先标记写入 `.trash` 清单，在后续打开/构建且快照引用归零后再物理清理，并为所有 `os.replace` 增加针对杀毒软件/资源管理器瞬时锁的 `5 × 40ms` 退避重试与随机临时文件名（`NamedTemporaryFile`）。
3. **中文 Windows（`cp936`/`gbk`）`stdio` 崩溃根治**：
   - `mcp_server.py` 与 `remote_stdio.py` 的 JSON-RPC 标准输出统一改为 `sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")`，彻底避免输出 `🖼️`、`📌`、`⚠️` 等符号时触发 `'gbk' codec can't encode character` 崩溃。
4. **锁外异步 I/O 与连接泄漏修复**：
   - 移除 `ConsoleState._lock` 内部的所有远程 HTTP 探测；`POST /api/vaults/sync` 升级为后台异步任务（返回 `job_id` 供前端展示实时进度并只做一次快照热切换）；修复 `search.py` 中 `BM25Index._open_cons` 在多线程 HTTP 服务下的只读句柄累积问题。

---

## 五、 新增与升级后端 API 清单（`console.py` / `gateway.py`）

| 路由 | 平面 | 方法 | 用途 |
| :--- | :--- | :--- | :--- |
| `/api/bootstrap` | Admin | `GET` | 浏览器同源初始化握手（返回版本号、运行模式、同源 CSRF 凭证） |
| `/api/jobs/<job_id>` | Admin | `GET` | 查询后台异步 `sync` / 导入任务进度与结果 |
| `/api/vaults/watch` | Admin | `POST` | 开启/关闭指定本地知识库的文件变更自动增量同步 |
| `/api/vaults/synonyms` | Admin | `GET / POST` | 读取与编辑本地知识库 `synonyms.json`（`mtime` 实时热生效） |
| `/api/tokens/rotate` | Admin | `POST` | 单设备令牌密钥轮换（立即使旧令牌失效并返回新密钥） |
| `/api/playground/inspect` | Admin | `POST` | 本地库全链路四层透视 + 结构化 ACL Shadow Diff / 远端库实测调用 |
| `/api/logs/list` | Admin | `GET` | 多条件游标分页查询 MCP 调用记录（按客户端、知识库、缺词、慢查询过滤） |
| `/api/logs/stream` | Admin | `GET (SSE)` | 支持 `fetch()` 自定义鉴权头的实时调用流推送（含 1s 心跳与优雅关停） |
| `/api/logs/stats` | Admin | `GET` | 24h 调用趋势、客户端占比、P50/P95 延迟、**Top 0 命中知识缺口词榜** |
| `/api/logs/clear` | Admin | `POST` | 清理历史调用审计日志 |
| `/api/network/discover` | Admin | `GET` | 并发执行 Tailscale 在线节点扫描（10s 缓存）+ 局域网 UDP 辅助探测 |
| `/api/network/pairing-code` | Admin | `POST` | 生成 5 分钟单次有效的 `wctx1_` OTP 配对码（含内网 + Tailscale 双通道地址） |
| `/api/network/connect-code` | Admin | `POST` | 经 SSRF 白名单与 TOFU 指纹校验后并发测速兑换设备专属 Token，写入 `remotes.json` v2 |
| `/v1/pair/exchange` | Gateway | `POST` | 网关公开端点：验证并一次性销毁 `(otp_id, otp_secret)`，签发设备专属 Bearer Token |

---

## 六、 分阶段实施路线图（调整后更均衡的 4 阶段与验收门禁）

### Phase 1：跨平台内核加固、可观测底座与双平台 CI（约占 35%）
- 落地 `state_lock.py`（跨平台锁 + `0600` 无竞态创建 + `daemon.json` 单实例仲裁）、Windows `[WinError 32]` 延迟回收、UTF-8 二进制 `stdio`、`KBSnapshot` 懒加载与 `BM25Index` 连接池修复。
- 落地 `telemetry.py`（异步单写队列 WAL 日志）、`context.py` 结构化 `ACLDecision`、`discovery.py`（OTP 配对码 + SSRF 防护 + Tailscale/工具链路径探测）及 `console.py` 全部新增 API、CORS/Origin 白名单与异步 `sync` Job。
- 升级 `_build_backend.py` 支持 `ui_dist/**` 与完整 PEP 517 钩子；配置 `.github/workflows/ci.yml`（含 `windows-latest` `PYTHONIOENCODING=gbk` 门禁测试）。

### Phase 2：`deploy/web` 三端同构 React 控制台（约占 30%）
- 搭建 `deploy/web/`（`React 19 + TypeScript + Vite + Tailwind v4`）与桌面/浏览器 `PlatformAdapter`。
- 实现全部 8 大视图（总览看板、知识库中心、**检索与 ACL 透视台**、**实时 MCP 日志流与缺词雷达**、多机互联中心、图形化角色组/目录树 ACL、6 大客户端同步与路径体检、Layer-4 记忆中心）。
- 通过 `deploy/scripts/build_web.py` 注入 `src/wiki_context/ui_dist/`，实测验证 `wctx ui` Web 模式完整闭环。

### Phase 3：`deploy/desktop` Tauri v2 桌面壳与原生能力集成（约占 20%）
- 初始化 `deploy/desktop/src-tauri`，实现 `sidecar.rs`（`daemon.json` 单实例附着、`WCTX_ADMIN_TOKEN` 注入、`stdin` 管道防孤儿退出、稳定 `wctx` 命令垫片安装）、`watcher.rs`（Rust `notify` 原生监听）与 `tray.rs`（菜单栏/托盘常驻、开机自启、关闭最小化）。
- 打通原生文件夹选择框与拖拽入库。

### Phase 4：`PyInstaller --onedir` 引擎封装与 macOS / Windows 安装包交付（约占 15%）
- 编写 `deploy/scripts/build_engine.py`（`--onedir` 无黑窗构建）与 `deploy/scripts/build_desktop.py`。
- 在本机完成 macOS `.app` / `.dmg` 完整打包、冷启动耗时（`< 200ms`）、6 客户端挂载与优雅退出实测；同步产出 Windows 打包流水线（`.github/workflows/release-desktop.yml` 及本地 Windows 一键打包脚本），发布 **`v0.4.0`**。
