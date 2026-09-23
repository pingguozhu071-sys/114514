# PENDING · 未完成 / 未验证 登记

> **纪律**：不假装完成。每条写清「是什么 / 为什么还没做 / 什么时候做 / 怎么验证」。
> 交付说明里必须包含本文件中的"未验证"项。

## 一、S1 必做（安全·策略·凭据地基）—— **已完成（2026-09-23）**

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| P1 | 脱敏策略对象 + 每类型开关 | **已完成** | `obs/policy.py`（7 类出口 + `summary/describe/log_state`）；门禁 C1–C5 |
| P2 | 唯一出网闸接线：全仓除闸外不得有裸请求 | **已完成** | `net/fetch.py`（Fetcher）；门禁 E1 扫描全仓 → 0 命中 |
| P3 | 礼貌预算收敛为单一咽喉（下载/HLS 都过） | **已完成** | 门禁 G1/G2 集成证明：名额与 UA 在咽喉内生效 |
| P4 | robots 抓取与按域缓存（5xx/不可达=全禁，≤24h） | **已完成** | `net/robots.py`；门禁 B1–B4 |
| P5 | Cookie 导入（Netscape/JSON → 会话 jar，DPAPI 落盘） | **已完成** | `privacy/cookies.py`；门禁 D1–D5（密文无明文、失效明确报错） |

**新增（S1 顺带发现，已修）**：`sanitize_url` 的 `[^&#]+` 会吃掉 URL 之后的整段文字 → 改为
`[^\s&#"'<>]+`（见 DEVLOG；这是自建门禁抓到的真 bug）。

## 二、S2 必做（核心骨架）—— **已完成（2026-09-23）**

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| P6 | DB 连接工厂（WAL / busy_timeout，**读连接也设**） | **已完成** | `store/db.py`；门禁 C1 断言 `journal_mode=wal`、`busy_timeout=30000` |
| P7 | `user_version` 迁移器（幂等 + 版本号由脚本自身写） | **已完成** | `frontier/migrations.py`；门禁 C1（重复迁移零变更） |
| P8 | 有界队列（前沿 ≤200k，满则拒绝=背压） | **已完成（前沿部分）** | `frontier.enqueue`；门禁 D2。download→parse / parse→store 两条队列在 S4 随执行面落地 |
| P9 | 单写线程 + 批提交 + 失败批次死信 | **已完成** | `store/writer.py` + `store/deadletter.py`；门禁 C2/C3（含"坏数据只回滚自己"） |
| P10 | 租约 + CAS（写在产物前）+ 心跳续到期 + 看门狗 | **已完成（任务级）** | `frontier/frontier.py`；门禁 D3–D8。**任务级看门狗**（`future.result(timeout=)` 超时标记作废、不杀线程）随 S4 的执行面落地 |

**本轮新增纪律（写进代码注释，别改回去）**：
* **SQL 必须是字符串字面量**（本机安全策略）：`execute()/executescript()` 的第一参数必须是字面量，
  连模块级 SQL 常量也算动态 → 所以 `frontier.py` 的 SQL 全部内联；迁移 DDL 在 `frontier/migrations.py` 里内联。
* **死信的表写入必须传写线程的连接**（`DeadLetter.write(..., conn=conn)`）：另开连接会撞锁卡住
  `busy_timeout`（30 秒），把整条写路径拖停（门禁 C2 抓到过）。
* 迁移目录 `frontier/migrations/`（.sql）已删除，`frontier/schema.sql` 降级为《开工包》参考对照。

## 三、S3–S5 必做

| # | 事项 | 状态 | 证据 / 说明 |
|---|---|---|---|
| P11 | 原始层（内容寻址 + 元数据 + 血缘）+ 离线 `reparse` 重放 | **已完成（2026-09-23）** | `capture/rawstore.py` + `capture/replay.py`；门禁 A1–A5、D1–D4（含"重放不联网"的模块级扫描） |
| P12 | 域冷却与 DB `cooldowns` 表双向同步 | **已完成（2026-09-23）** | `net/cooldown.py`；门禁 F1（落库 → 重启恢复 → 过期清理） |
| P13 | HLS 分片并发下载（解密与拼接必须按序） | **已完成（2026-09-23）** | `adapters/hls.py` 的 `concurrency`；门禁 C4/C5（6 片并发按序号落盘；AES-128 逐片解密顺序正确） |
| P14 | 文档解析（PDF/Office/图片/音视频元数据，进程隔离 + 资源上限） | **部分完成** | 新增 `understand/parsers/mediainfo.py`：图片尺寸（PIL）、容器条目（zipfile）、媒体元数据（ffprobe）、PDF 结构粗计；**进程隔离**已就绪（缺省即拒绝 + 超时终止）。**仍缺**：PDF/Office 的**正文文本抽取**（需额外库，属后续专题） |
| P15 | 产物契约统一化（大小 + mime + 可读/可播 + 内容哈希） | **已完成（2026-09-23）** | `capture/artifacts.py`（三个档位 + 带事实的判定）；门禁 A1–A5 |
| P23 | **端到端闭环**（`Task` → 环境 → 证据 → `Router.on_evidence` → `Frontier.commit_done/mark_failed`） | **已完成（2026-09-23，S6）** | `core/runner.py`（TaskRunner）+ `tests/gates/s6_gate.py` 11/11；覆盖直连/制品跨环境、robots 拒绝、连续限流、404、有界升级、CAS 失守、发现子任务、证据可解释 |
| P24 | 浏览器二进制就绪探测 | **已完成（2026-09-23，S8）** | `env/browser.py` 的 `probe_browser()`：只读 `browsers.json` + 二进制路径（20 次 2.3ms、零进程）。**实测：chromium 已装可用**（`ms-playwright\chromium-1234\chrome-win64\chrome.exe`）；firefox/webkit 未装（用不上），缺件时如实报告并禁用相关能力 |

## 三·三、S8 浏览器运行时 —— **已完成（2026-09-23）**

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| P33 | 浏览器运行时（进程/上下文/页槽位 + 观察 + 独立闸 + 预算） | **已完成** | `env/browser.py`；门禁 12/12（含真实 chromium 端到端：本地夹具双端口、被拦子资源**目标端零请求**） |
| P34 | 浏览器接进闭环（空壳页 → browser 阶段）+ 缺省即拒绝 | **已完成** | `runner._do_browser()` + 资源注册表；门禁 E1/E2 |
| P35 | 诚实 UA 与"读体会触发重取"的坑 | **已完成（并写进文件头）** | UA 两处给（context 头 + 启动参数）；只对 document/xhr/fetch 读体；门禁 C1 断言 UA 全诚实 + 资源不翻倍 |

## 三·四、S9 命令行 —— **已完成（2026-09-23）**

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| P36 | 引擎装配（CLI 与 GUI 共用）与关闭链唯一入口 | **已完成** | `core/app.py`（EngineApp）；`doctor/metrics/alerts/export/reparse/run_targets/shutdown` |
| P37 | 子命令 + 退出码语义 + `--json` 纯净性 | **已完成** | `cli.py`（8 个子命令）；门禁 A1–A3/C2/F1/F2；退出码 `0/1/2/3/4` 写死可依赖 |
| P38 | 跨命令共用一个数据根（先采集 → 再离线重扫） | **已完成** | 门禁 D1：`reparse` 扫描 2 份 → 落库 2 条，**网络增量 0** |
| P39 | **robots 自递归（全站被拒）** | **已修（S9 发现，S4 门禁 G1 钉住）** | `Fetcher.open_for_robots()` + 线程级重入守卫；闸与限速不受影响 |
| P40 | `collect` 收工条件（曾空转 `idle_timeout` 300s） | **已修** | `run_targets` 改成"队列空 **且** 无在飞任务"才收工；实测 0.8s 返回 |
| P41 | 写线程顶层崩溃保护 | **已修** | `SingleWriter._run` 兜住一切异常（死信 + `_crash` + 重连）；`run_now` 超时给明确原因 |

## 三·二、S7 观测与运维 —— **已完成（2026-09-23）**

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| P25 | 指标面（≥15 项含 p50/p95/p99、有界内存、序列基数闸） | **已完成** | `obs/metrics.py`（摘要 28 项）+ 接进咽喉/写线程/池/运行器；门禁 A1–A5 |
| P26 | 结构化 JSON 日志 + 轮转 + 先脱敏后落盘 | **已完成** | `obs/logs.py`（字段稳定、UTF-8、RotatingFileHandler）；门禁 B1–B5 |
| P27 | 告警阈值（队列/失败率/磁盘/内存/延迟）+ 缺失指标报 unknown | **已完成** | `obs/alerts.py` + `[alerts]` 配置段；门禁 C1/C2 |
| P28 | 资源计划：Σ(池规模×单任务峰值) ≤ 4GB 自证、无界队列被拒 | **已完成** | `core/limits.py` + `[limits]` 配置段；门禁 D1–D3 |
| P29 | 任务下钻 + 台账五态导出（JSONL） | **已完成** | `obs/drilldown.py`；门禁 E1–E5 |
| P30 | 优雅关闭链 + 在飞任务强引用（E9） | **已完成** | `core/lifecycle.py` + `Frontier.release()`；门禁 F1–F5 |
| P31 | 基准与回归对比、长跑冒烟（L5/L7） | **已完成** | `tools/bench.py`、`tools/soak.py`、`tools/gates.py`、`tools/lint.py`；门禁 G1/G2/H1 |
| P19 | **24h 长跑未执行** | 未验证（需机主点头） | 已有 1h 档（`python tools/soak.py --minutes 60`）；24h 需机主决定何时跑 |
| P32 | 中文引号统一为「」（纯风格，`tools/lint.py` 的 `cjk-quote` 软提示） | 未完成（约 190 处，全在注释/文案里） | 不影响功能与门禁（软规则，退出码不受影响）；顺手改即可，见 `python tools/lint.py --all` |

## 三·五、S10 GUI 与跨机打包 —— **已完成（2026-09-23）**

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| P42 | GUI 设计系统全规格（三层材质/强调色/底图管线/渲染调度/性能红线） | **已完成** | `src/daedalus/ui/*`；`s10_gate.py` 21/21；`tools/perf_probe.py` 四项红线实测通过 |
| P43 | PyInstaller 打包（one-dir + 双 EXE + 版本资源 + 图标） | **已完成** | `packaging/daedalus.spec`；产物 `dist/daedalus/`（413MB）；打包态 CLI 冒烟通过 |
| P44 | 自制 NSIS 安装向导 + 卸载向导（不静默 / `un.` 前缀 / 条件化清理 / 数据保留 / DPI / 三语言） | **已完成** | `packaging/installer.nsi`（0 警告编译）；`s10b_gate.py` 20/20；产物 `Daedalus-Setup-0.1.0.dev0.exe` |
| P45 | 版本号单一来源四件套校验 | **已完成** | `tools/version_check.py`（含全仓硬编码扫描）；门禁 A1/A2/B4 |
| P46 | **中文全文检索（FTS5 + 预分词）** | **已完成（清单 G5 驱动）** | 迁移 0002 + `store/search.py` + CLI `search`；门禁 S3 F1 |
| P47 | **每线程独立会话**（出网层不共享会话对象、无共享连接池） | **已完成（清单 I1 驱动）** | `net/ssrf_gate.py` 的 `_opener_for_thread()`；门禁 S1 H1 |
| P48 | **静态基线与覆盖率工具**（只报告不设阈值；只降不升） | **已完成（清单 O4 驱动）** | `tools/baseline.py` + `docs/16-静态基线与覆盖率.json`（覆盖率 68.05% 估计值；lint 硬拦 0） |
| P49 | **仓库零提交**（计划里「一修复=一 commit」从未执行） | **已修（2026-09-23）** | 首个提交 `fa08f7d`（130 文件/24526 行）；自该提交起每次修复单独提交 |
| P50 | **打包态 GBK 控制台编码崩溃** | **已修（打包冒烟发现）** | `cli._make_output_safe()` + ASCII 标记；门禁 S9 F3 |
| P51 | NSIS 脚本 BOM / MUI 宏顺序 / 悬空 File 指令 | **已修** | `tools/build.py` 的 `_ensure_bom()`；`installer.nsi` 0 警告 |
| P52 | **边界自证：playwright 必须是未改装的正版** | **已完成** | `env/browser.py` 的 `_playwright_is_genuine()`（本机同装 patchright，未被劫持但需防静默替换）；门禁 S8 D3 |
| P20 | 未装过的新机器上安装未验证 | 未验证 | 已能编译安装器、通过全部静态门禁；真机安装需一台干净机器/干净账户 |
| P19 | 24h 长跑未执行 | 未验证 | 1h 档命令就绪（`python tools/soak.py --minutes 60`） |

## 三·六、名单外的纪律缺口（如实登记）

| # | 事项 | 状态 | 说明 |
|---|---|---|---|
| P53 | **`docs/10` 检测清单执行结果** | 进行中 | 逐项 PASS/FAIL/未验证 见 `docs/10-完工检测清单-执行结果.md` |
| P32 | 中文引号统一为「」（软规则：281 处，全在注释/文案） | 未完成 | 不影响功能与门禁（软规则不改退出码）；`python tools/lint.py --all` 可查 |

## 三·七、安全审计（自审，2026-09-23）—— **已完成，5 修 1 记**

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| P54 | 安全自审探针（28 项对抗性电池：入网闸/逐跳/脱敏/检索注入/路径穿越/子进程/上限/压缩炸弹） | **已完成** | `tools/security_probe.py`（28/28）；`docs/17-安全审计（自审）.md` |
| P55 | **凭据不可关**（原来关掉两个开关就能让 token 明文进日志——红线被做成了选项） | **已修** | `sanitize_credentials()` 不吃开关；门禁 S1 **C2b** |
| P56 | `sanitize_url` 三类缺口：userinfo（Basic Auth 口令）/ 分号参数 / fragment 参数 + OAuth `code=` | **已修** | 探针逐形态验证；不误伤正常参数、不吃后续正文 |
| P57 | 文本里的 header 转储（`Authorization: Bearer X`）与键名凭据字段（`cookie=/token=`）不脱敏 | **已修** | `sanitize_credentials` + `_SECRET_KEYS` 整子树抹掉 |
| P58 | 浏览器槽位**只记账不强制**（并发 observe 能无限开上下文） | **已修** | `BoundedSemaphore` 非阻塞；门禁 S8 **D4** |
| P59 | 卸载器 `%TEMP%\…bat` 后执行（NSIS 延迟扫尾） | **记录不修**（有意取舍） | 理由见 `docs/17` §四：每用户私有 temp + 先截断再写 + 替代方案会留残骸 |
| P60 | 界面性能探针在机器忙时测不准（会把"机器忙"误读成"性能回归"） | **已修** | 探针输出 `machine_load_pct` / `measurement_trustworthy`；门禁据此 SKIP |
| P61 | **依赖供应链 CVE 扫描**（PySide6/numpy/opencv/playwright/psutil/qfluentwidgets） | **未做** | 需 `pip-audit` 或 OSV-Scanner（要联网）；本机自审未覆盖 |
| P62 | 二进制产物完整性（EXE/DLL 是否被篡改）+ fuzzing 原生解析器 | **未做** | 需签名/SBOM 工具链与专门 fuzzing；已在 `docs/17` §五 登记 |

## 四、可选 / 环境相关（已知缺失，不阻塞）

| # | 事项 | 现状 | 备注 |
|---|---|---|---|
| P16 | `aria2c` / `yt-dlp` 未安装 | 缺失 | 都是**可选助手**；yt-dlp 只在"站点专用提取"时用，aria2c 只在"多连接下载"时用。不用它们也能跑 |
| P17 | `duckdb` 未安装 | 缺失 | 分析层（S7 之后按需），当前用 JSONL 落变更台账 |
| P18 | `playwright` 已装但**浏览器二进制未确认** | 未验证 | S8 前必须：`python -m playwright install --dry-run` 或检查 `ms-playwright` 目录；**未装则媒体/网络观察环境不可用**（就绪探测要如实告知） |
| P19 | 24h 长跑未执行 | 未验证 | 需机主点头；先跑 1h 冒烟看内存斜率 |
| P20 | 跨机打包未验证 | 未开始 | S10；需要一台"未装过"的机器或干净测试账户 |
| P21 | `tools/make_icon.py --mask` （复用手工修好的掩膜）**未实现** | `docs/13-图标与美术.md` 里提到"手工修掩膜后可用 `--mask` 复用"——这个开关还没写 | 若需要"零残留全身透明图"：先手工修 `mask_preview.png`，再补这个开关（约 30 行）；或网络好时改用 `birefnet-general` |
| P22 | 图标全身版保留了原画设计层（`VOCALOID 02`/`01`/框线） | 自动抠图会把它们部分保留（模型当成前景）；图标小尺寸用胸像已规避 | 对图标无影响；要纯角色版见 P21 |

## 五、待确认（需要机主决定）

| # | 事项 | 默认 |
|---|---|---|
| Q1 | **授权目标清单**（自有/合作/已购授权/官方 API） | 为空 → 只能采公开数据，按礼貌预算跑 |
| Q2 | 异步执行格引入时点 | 暂不引入（触发条件：长连接并发数 / 阻塞等待占比） |
| Q3 | 进程池启用阈值 | 解析 CPU 占比 >30% 且单页 <2MB 才开 |
| Q4 | 分析层是否上 DuckDB/Parquet | 暂不上 |
