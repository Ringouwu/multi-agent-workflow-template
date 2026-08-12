# Multi-Agent Workflow Template

一套**可复用的多 Agent 软件开发流水线架构**：PM 需求 → Architect 方案 → Builder 实现 → Auditor 审计，通过 Kanban 任务板自动调度，审计不通过自动返工闭环。

> 本仓库只包含**可泛用**的内容（角色定义、流程、规则、启动脚本）。具体项目的内容（硬件缺陷、特定 API、某个项目的代码）不在此仓库，请参考各项目自己的仓库。

## 架构总览

```
┌─────────────────────────────────────────────────────────┐
│  Orchestrator (主对话/编排者)                             │
│  扮演 PM 做需求访谈 → 写 PRD → 完成 T1 → 接收完成通知        │
└──────────────────────────┬──────────────────────────────┘
                           │ 触发依赖引擎
                           ▼
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  T1 PM   │──→│ T2 Arch  │──→│ T3 Build │──→│ T4 Audit │
│ 需求梳理  │   │ 技术方案  │   │ 代码实现  │   │ 代码审计  │
└──────────┘   └──────────┘   └──────────┘   └────┬─────┘
                                                  │ 不通过
                                                  ▼
                    ┌────────────── 返工闭环（≤3轮）──────────────┐
                    │  auditor block + 建修复方案 → architect      │
                    │  → 出 fix-plan → [watchdog 自动桥接]         │
                    │  → builder 修复执行 → auditor 复审           │
                    │                                             │
                    │  ⚠️ 原生流水线的坑：方案做完了不会自动调      │
                    │     builder，需要 watchdog 脚本补上这一环     │
                    └───────────────────────────────────────────┘
                                                  │
                                                  ▼ 全部完成
                                    📩 自动桌面通知（watchdog）
```

## 角色（Profile）

| Profile | 职责 | 工具集 | 说明 |
|---------|------|--------|------|
| **pm** | 需求梳理，输出 PRD | file/web/skills/memory/kanban | 在主对话由编排者扮演（需求访谈是人机交互密集阶段） |
| **architect** | 技术方案 design.md + 修复方案 | file/web/skills/memory/kanban | **调研先行**（见下） |
| **builder** | 代码实现 | file/terminal/web/skills/memory/kanban | 唯一有 terminal 的角色 |
| **auditor** | 代码审计，P0-P3 分级 | file/skills/memory/kanban（只读） | 只找问题不修代码，**自动触发返工** |
| **teacher** | 旁路答疑（可选） | file/web/skills/memory/session_search | `teacher -z "问题"` 或独立窗口，不污染主对话 |

角色模板在 `profiles/templates/`（SOUL.md 泛用版 + config.yaml 模板），`profiles/examples/` 有嵌入式项目示例。

## 关键规则

### 1. 调研先行（Architect 出方案前必须做）

**禁止直接凭记忆写方案**。写 design.md 前先调研：

1. **GitHub 搜索可复用库/项目**（评估 star/维护状态/许可证）
2. **网络搜索已知坑**（`<技术> <功能> 问题|not working|bug`，看 Reddit/Stack Overflow/社区）
3. **查官方文档**确认 API/限制
4. 结论写进 design.md **第 0 章**（0.1 可复用库 / 0.2 已知坑与规避 / 0.3 社区经验）

> 为什么：硬件/框架的已知缺陷（如某开发板的射频问题）社区早有答案，不调研会浪费大量试错时间。

### 2. 审计自动返工闭环（Auditor 不通过时）

> ⚠️ **重要**：原生 kanban 流水线在返工环节有断链——auditor 创建「修复方案」任务给 architect 后，architect 完成方案时**不会自动触发 builder 去执行修复**。需要 `scripts/kanban_watchdog.py` 补上这一环（见下文「看板守护进程」）。

审计发现 P0/P1 问题时：

```
auditor:
  kanban_comment → kanban_block → kanban_create("修复方案", assignee=architect)
                                                    ↓
                              architect 判真伪（排除误报）→ docs/fix-plan.md → complete
                                                    ↓
                              🐶 watchdog 检测到方案完成 → 自动创建 builder 修复执行任务
                                                    ↓
                              builder 按计划修复 → complete → auditor 自动复审
                                                    ↓
                              不通过再循环；第 3 轮仍不过 → block 升级人工
```

Watchdog 桥接逻辑（自动执行，不花 LLM 钱）：
1. 检测到 auditor 任务 blocked
2. 检测到标题含「修复方案」的 architect 任务状态为 done
3. 创建同名「修复执行」任务给 builder
4. 链接依赖：修复方案 → 修复执行 → 审计任务
5. builder 完成后，审计任务自动 promoted 进入复审

### 3. 防误判：字节级验证

工具显示层可能把密钥样式内容打码（如 `Bearer xxx` → `Bearer ***`），导致：
- builder 误抄占位符
- auditor 误判"未修复"

**涉及认证头/密钥的判断，用字节级验证**（读原始字节查 `%s` / `\r\n`），不要只信显示内容。

### 4. Auditor 防放水（双重保障）

**问题**：Auditor 复审时可能只读代码不编译，明明编译失败却说"全部通过"。
- 根因：静态读代码发现不了依赖配置错误、API 版本不兼容等编译期问题
- 实际案例：v2 复审 auditor 说"3 个问题全是误报"，但 gradle 编译失败

**解决（三重保障）**：

| 层级 | 措施 | 谁来做 |
|------|------|--------|
| 流程层 | 编译前置验证：watchdog 在送 auditor 复审前先跑构建，失败直接打回 builder | watchdog 脚本 |
| 角色层 | auditor profile 加 system prompt，明确要求「先编译再审计，编译失败直接 P0」 | 编排者配置 |
| 最终门 | 流水线全部完成后，Orchestrator 自己再跑一次编译验证 | 编排者 |

**Auditor system prompt 模板**（放在 `~/.hermes/profiles/auditor/system_prompt.md`）：

```markdown
# Auditor 角色规则

1. **编译是底线**：审计前必须先跑构建命令。
   - Android/Kotlin: `./gradlew assembleDebug`
   - Python: `python -m py_compile` 或 pytest
   - Rust: `cargo build`
   - 其他: 找到对应编译/检查命令

2. **编译失败 = P0**：只要编译不通过，直接判 P0，不需要完整审计。

3. **不要假设**：不要凭"代码看起来没问题"就通过，必须实际验证。

4. **实事求是**：builder 说"已修复"不代表真修了，自己验证。
```

## 快速开始（新项目）

### 方式一：脚本一键启动

```bash
./scripts/start_project.sh <slug> <中文名> [工作目录]
# 示例
./scripts/start_project.sh my-webapp "我的 Web 应用" ~/projects/my-webapp
```

脚本自动：建目录 → 建看板 → 建 4 环任务链 → 打印下一步指引。

### 方式二：手动命令

```bash
# 1. 建项目目录 + 看板
mkdir -p ~/projects/<项目>/{docs,src,tmp}
hermes kanban boards create <slug> --display "<中文名>"
hermes kanban boards use <slug>

# 2. 建任务链（T1 完成后依赖引擎自动调度后续）
T1=$(hermes kanban create "需求梳理" --assignee pm --priority 10 --workspace "dir:~/projects/<项目>" --json | ...)
hermes kanban create "技术方案设计" --assignee architect --parent $T1 --priority 9 --workspace "dir:~/projects/<项目>"
hermes kanban create "代码实现" --assignee builder --parent $T2 --priority 8 --workspace "dir:~/projects/<项目>"
hermes kanban create "代码审计" --assignee auditor --parent $T3 --priority 7 --workspace "dir:~/projects/<项目>"

# 3. 编排者完成 T1 触发流水线
hermes kanban complete $T1 --result "PRD 已完成，见 docs/PRD.md"
```

### 流程

1. **需求访谈**（主对话）：编排者扮演 PM 提问（5-10 问），确认后写 `docs/PRD.md`
2. **流水线**：完成 T1 → 自动 T2（architect 调研+方案）→ T3（builder 实现）→ T4（auditor 审计）
3. **返工**：审计不通过自动循环（见规则 2）
4. **验收**：编排者验证代码 + 汇报，README 与代码版本核对

## 配置说明

### 模型

所有 profile 用同一模型。**注意**：某些 provider 的 codex_responses 模式在 kanban worker 会报 HTTP 400（`missing input.content`），推荐 OpenAI 兼容 chat_completions 的 provider（如 deepseek）。

### Profile 的 .env

每个 profile 有**独立 .env**（`~/.hermes/profiles/<role>/.env`），需手动追加 API key：
```bash
echo "<YOUR_API_KEY_ENV>=sk-..." >> ~/.hermes/profiles/<role>/.env
```

### 看板守护进程（推荐，必加）

`scripts/kanban_watchdog.py` 是流水线的**必要补充**，解决了多个原生 kanban 的断链问题：

| 功能 | 解决什么问题 |
|---|---|
| 🚀 **阻塞启动** | auditor block 时创建的修复方案任务，会把 auditor 设为父依赖 → 子任务永远无法 promote（父任务是 blocked 不是 done）。watchdog 自动解绑 + 启动。 |
| 🔗 **返工桥接** | architect 修复方案 done 后，不会自动触发 builder 执行。watchdog 自动创建修复执行任务并链接。 |
| 🔨 **编译前置验证** | auditor 复审可能放水（只读代码不编译，明明编译失败却说通过）。watchdog 在送复审前先跑构建命令，失败直接打回 builder，省 LLM token。 |
| 🔄 **自动复审** | builder 修复执行 done 后，blocked 的 auditor 不会自动 unblock（即使父依赖全 done）。watchdog 检测到后自动 unblock 进入复审。 |
| ✅ **完成汇报** | 看板全部 done 时自动发桌面通知，不用手动来问「做完了吗」。 |

**构建工具自动检测**（编译前置验证支持）：
Gradle (Android/JVM)、Cargo (Rust)、Go、Make、CMake、npm、Python 语法检查。
未检测到构建工具的项目自动跳过，不阻塞。

**部署方式（cron，推荐）**：
```bash
# 每个看板加一条 cron，每分钟跑一次（用完即走，几乎不占资源）
* * * * * /usr/bin/python3 /path/to/scripts/kanban_watchdog.py --board <your-board> >> ~/.hermes/kanban-watchdog/cron.log 2>&1
```

**守护进程模式**（不推荐，cron 更稳）：
```bash
python3 scripts/kanban_watchdog.py --board <your-board> --daemon --interval 60
```

> 资源占用（cron 模式）：内存 ~10MB × 每次运行 <1 秒，平时 0 占用。硬盘可忽略。

## 按项目定制

| 部分 | 怎么改 |
|------|--------|
| **SOUL.md** | 角色描述按项目领域改（嵌入式/Web/数据…），职责与规则保留 |
| **builder 工具** | 需要编译运行 → 保留 terminal；纯文档 → 去掉 |
| **任务链深度** | 简单项目 4 环够；复杂可加测试/部署环节 |
| **调研重点** | 硬件项目查硬件缺陷；Web 项目查框架/库的已知问题 |

## 目录结构

```
multi-agent-workflow-template/
├── README.md              # 本文件（架构+用法）
├── profiles/
│   ├── templates/         # 泛用 SOUL.md + config.yaml 模板
│   └── examples/          # 具体项目示例（嵌入式等）
├── scripts/
│   ├── start_project.sh   # 一键启动新项目
│   └── kanban_watchdog.py # 看板守护：返工桥接 + 完成通知 + 健康检查
└── docs/                  # 流程细节文档
```

## License

MIT
