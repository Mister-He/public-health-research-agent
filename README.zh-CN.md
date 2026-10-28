# 公共卫生研究情报助手

[English](README.md) · [示例周报](docs/examples/fixture-digest-2026-10-21.md) · [架构说明](docs/architecture.md) · [公开前风险审查](docs/public-release-review.zh-CN.md)

为计算公共卫生研究者持续检索、筛选和评估学术文献，生成可追溯、经过校验的 Markdown 周报。默认通过 GitHub Actions 在每天 **06:00（Asia/Shanghai，UTC+8）**运行，每天检索和评估新文献，每周一（`reporting.digest_weekday`）从本周评估过的论文中发布一份周报；实际启动时间可能受 GitHub 调度延迟影响。

当前研究画像涵盖生物统计、流行病学建模、微观模拟、传染病传播模型、卫生经济学、公共卫生政策，以及可迁移到这些领域的 AI 方法。研究主题、期刊和评分规则均可配置。

## 工作流程

1. **增量检索**：从多个文献索引和预印本平台获取记录，按数据源和查询保存进度，失败后继续补抓。
2. **规范化与去重**：统一 DOI、PMID、PMCID 等标识，合并重复论文，保留字段来源与修订记录。
3. **预筛选与 AI 评估**：先在本地预筛选，再将有限数量的候选交给 Claude 或可选的 DeepSeek，要求返回结构化结果和原文证据。
4. **趋势跟踪**：建立默认 180 天的初始基线，按日更新、按周重新评估趋势标签。
5. **评分与推荐**：由 Python 计算加权总分，结合相关性、时效性、多样性和历史推荐，最多选择 10 篇论文。
6. **生成与校验**：先生成暂存周报，再检查结构、元数据、来源、论文身份、链接和分数；通过后才发布到 `reports/YYYY-MM-DD.md`。

AI 服务不可用时，已抓取的数据和分析队列仍会保存。系统可以使用已有合格评估继续筛选；合格论文不足时不发布周报。联网身份查询临时失败、出版商反爬等情况可能记录为警告，具体规则见[校验策略](docs/validation_policy.md)。

## 支持的数据源

| 数据源 | 用途 | 增量依据 |
|---|---|---|
| PubMed | 按主题检索，支持标题/摘要和 MeSH | Entrez 收录日期 EDAT |
| Europe PMC | 按主题检索，包含预印本 | 首次索引日期 |
| OpenAlex | 按主题检索、提供外部趋势信号 | 发表日期，加重叠窗口 |
| medRxiv | 抓取日期窗口内的记录，在本地筛选 | 预印本发布日期 |
| Crossref | 按期刊 ISSN 检索，核验 DOI | DOI 注册日期 `created` |
| arXiv | 可选的计算方法来源，默认关闭 | 提交日期 |

重点期刊在 [config/journals.yaml](config/journals.yaml) 中维护，包括 The Lancet Public Health、PLOS Medicine、BMC Medicine、JAMA Network Open，以及相关医学、卫生经济学、生物统计和信息学期刊。

## 本地安装

需要 Python **3.11 或以上版本**、Git 和 [uv](https://docs.astral.sh/uv/)。GitHub Actions 使用 Python 3.12。

在仓库根目录执行：

```bash
uv sync --locked --extra dev
cp .env.example .env
```

编辑 `.env`，填写 `RESEARCH_AGENT_CONTACT_EMAIL`，按需添加 NCBI、OpenAlex 或 DeepSeek 的密钥，然后执行：

```bash
set -a; . ./.env; set +a
uv run research-agent init
uv run research-agent doctor
uv run pytest -q
```

程序不会自动加载 `.env`，需要先按上面的方法导出环境变量。`.env` 已被 Git 忽略；不要把密钥写入配置文件、Issue 或提交历史。联系邮箱会随部分数据源请求发送。

`pytest` 默认跳过联网测试，不消耗 AI 服务额度。显式执行 `uv run pytest -m live` 才会访问真实文献 API。

### 示例与本地运行

先查看[合成数据示例](docs/examples/fixture-digest-2026-10-21.md)。也可以完全离线重新生成它：

```bash
uv run python scripts/generate_fixture_example.py
```

该脚本使用临时数据库，并更新示例 Markdown 文件。论文记录、评分和概述都是测试模拟数据，模型标记为 `fixture-simulated`，不能作为真实研究推荐。

抓取真实文献并建立初始基线：

```bash
uv run research-agent research-landscape --lookback-days 180 --time-budget 2700
uv run research-agent queue status
```

首次基线可能需要多次运行，后续会从持久化进度继续。默认的 Claude 自动评估通过 GitHub Actions 执行；本地可生成评估清单、导入评估结果，或使用 `--provider fixture` 做模拟。已有缓存评估时，可以跳过新的 AI 分析：

```bash
uv run research-agent run --dry-run --skip-llm
```

这条命令仍会抓取真实文献；缓存中的合格评估不足时不会生成周报。

## 部署到 GitHub Actions

完整步骤见[部署文档](docs/setup.md)。当前默认会复制完整摘要，并将运行状态写入同一仓库的 `research-data` 分支，建议先在私有仓库运行。准备公开时，先阅读[风险审查](docs/public-release-review.zh-CN.md)。

1. 将项目推送到自己的 GitHub 仓库。
2. 在已登录 Claude Code、具备适用订阅的本地机器上运行 `claude setup-token`。
3. 将 token 保存为仓库 Secret `CLAUDE_CODE_OAUTH_TOKEN`。可运行 `scripts/set_claude_token.sh`，按隐藏输入提示粘贴 token；脚本会更新 Secret 并触发认证检查。
4. 设置仓库 Variable `RESEARCH_AGENT_CONTACT_EMAIL`，按需设置 Secret `OPENALEX_API_KEY` 和 `NCBI_API_KEY`。
5. 在 Settings → Actions 中允许所需 Actions，并确保写入任务有权推送周报和状态分支。配置分支保护时，需要同时安排自动发布的写入权限。
6. 在 Actions → **Daily research digest** 手动运行，首次选择 `dry_run: true`，检查抓取、分析和校验结果，再开启正常发布。

也可以用 GitHub CLI 设置变量和 Secret：

```bash
gh secret set CLAUDE_CODE_OAUTH_TOKEN
gh variable set RESEARCH_AGENT_CONTACT_EMAIL --body you@example.org
gh secret set OPENALEX_API_KEY
gh secret set NCBI_API_KEY
```

不要设置 `ANTHROPIC_API_KEY`：当前工作流会在检测到该仓库 Secret 时停止。默认 Claude 路径使用订阅 OAuth，没有自动切换到付费 API 的机制。

**`dry_run` 仍会抓取数据、调用已启用的 AI 服务并持久化进度及合格评估**，只是跳过周报正式发布和周报提交。它不能用于隔离私有数据或避免服务用量。

### 可选：使用 DeepSeek

在 [config/llm.yaml](config/llm.yaml) 的 `llm` 节点同时设置：

```yaml
provider: deepseek
auth: api_key
```

然后添加仓库 Secret：

```bash
gh secret set DEEPSEEK_API_KEY
```

每次运行只选择一个分析服务，不会自动回退。DeepSeek 和 Claude 使用相同的任务提示与输出校验流程，但按模型分别缓存评估。DeepSeek 按 token 计费；论文数量和 token 配置用于控制用量，其中 token 阈值在请求之间检查，并非严格的账户支出上限。切换服务也意味着论文内容和研究画像会发送给所选服务。

## 自定义配置

配置文件位于 `config/`，启动时会进行类型和跨文件校验。详见[配置说明](docs/configuration.md)。

| 文件 | 配置内容 |
|---|---|
| `research_profile.yaml` | 核心/探索主题、重要性、关键词、MeSH 和排除项 |
| `search_queries.yaml` | 各数据源查询词组 |
| `journals.yaml` | 期刊名称、ISSN 和启用状态 |
| `scoring.yaml` | 评分维度、权重、规则版本和缺失数据策略 |
| `ranking.yaml` | 推荐数量、时效窗口、相关性和多样性约束 |
| `trends.yaml` | 趋势标签、时间窗口和每周重评规则 |
| `llm.yaml` | 分析服务、模型、批次大小和重试策略 |
| `sources.yaml` | 数据源地址、分页、限速和网络重试 |
| `reporting.yaml` | 输出目录、概述长度和摘要复制策略 |
| `validation.yaml` | 联网身份核验、链接检查和修复轮数 |
| `runtime.yaml` | 时区、存储位置、保留期限和 Git 分支 |

默认评分权重为：研究契合度 30%、方法创新 25%、趋势相关性 15%、时效性 15%、证据与方法严谨性 10%、跨学科可迁移性 5%。启用维度的权重总和必须为 1。详见[评分方法](docs/scoring_methodology.md)和[趋势方法](docs/trend_methodology.md)。

## 常用命令

日期参数使用 `YYYY-MM-DD`。

| 命令 | 用途 |
|---|---|
| `init`、`doctor [--offline]` | 初始化存储，检查配置、数据库和连接 |
| `research-landscape --lookback-days 180` | 建立初始文献与趋势基线 |
| `fetch --date D` | 增量抓取和去重 |
| `score --date D --provider manifest` | 生成待评估清单 |
| `score --date D --provider import` | 校验并导入现有评估文件 |
| `score --date D --provider deepseek` | 使用已配置的 DeepSeek 进行付费评估 |
| `trends update --date D [--weekly]` | 更新趋势快照 |
| `digest generate --date D` | 生成并校验暂存周报 |
| `digest validate --date D` | 重新校验已生成的周报 |
| `run --date D [--dry-run] [--skip-llm]` | 运行本地完整流程 |
| `backfill --start A --end B` | 按历史时点补生成周报 |
| `queue status`、`queue retry --eligible` | 查看队列、重新释放可重试任务 |
| `history [--paper ID]` | 查看推荐历史和论文审计记录 |
| `tags list` | 查看趋势标签；其他管理子命令见 `tags --help` |
| `feedback add/import/suggest`、`evaluate` | 记录人工反馈、评估推荐质量 |
| `replay --date D` | 在临时数据库副本上重放历史排序 |
| `state restore/persist` | 从远端数据分支恢复/向其推送状态 |

查看参数：`uv run research-agent <命令> --help`。`state restore` 会替换本地数据库，`state persist` 会推送数据分支，执行前应明确需要保留的状态。

退出码：`0` 成功，`1` 阻断或校验失败，`2` 输入/配置错误，`3` 分析服务不可用而暂缓，`4` 合格论文不足，`5` 数据分支持续发生写入冲突。排查方法见[运维文档](docs/operations.md)。

## 人工反馈与质量评估

```bash
uv run research-agent feedback add --canonical-id doi:10.5555/example --label relevant
uv run research-agent feedback import
uv run research-agent evaluate
```

上面的 DOI 仅演示命令格式，请替换成真实论文的规范标识。反馈保存在 `feedback/feedback.csv`；人工标注集位于 `feedback/gold.csv`。`feedback suggest` 只在 `feedback/proposals/` 写出建议，不会自动修改研究画像。没有标注数据时，相应质量指标返回 `None`。

这些文件可能包含个人阅读偏好和笔记，公开提交前应检查内容。

## 存储、用量与公开边界

- 本地状态位于 `state/research.sqlite3`；CI 将完整数据库序列化到 **`research-data` 分支**。压缩用于节省空间，不提供访问控制。
- Claude 消耗订阅额度，和其他 Claude Code 使用共享；DeepSeek 为可选付费服务。调用失败后会保留队列，按策略重试。
- 外部数据源可能有限速、配额或认证要求；GitHub Actions 的可用额度和计费以账户当前规则为准。
- `reporting.abstract_policy: link_only` 只让新生成的周报展示摘要链接，**不会清除数据库、历史提交或 Actions 产物中的摘要**。
- 公开仓库前，应分别检查代码与提交历史、所有分支、研究画像与反馈、Actions 日志和产物。若只想公开代码，最省事的做法是新建经过筛选的代码仓库，把现有自动化及状态留在私有仓库。

## 目录结构

```text
config/              YAML 配置
prompts/             版本化任务提示和评分规则
src/research_agent/  抓取、去重、筛选、评估、趋势、报告和校验
tests/               单元、集成、端到端和显式联网测试
reports/             正式周报；.staging/ 为被 Git 忽略的暂存目录
feedback/            人工反馈、标注集和调整建议
docs/                架构、方法、部署、运维和示例
scripts/             认证辅助和合成示例生成脚本
.github/workflows/   每日流程（每周发布周报）、测试和认证检查
```

## 许可证

项目代码采用 [MIT 许可证](LICENSE)。论文摘要和其他第三方内容仍受各自许可约束，项目的 MIT 许可证不授予这些内容的再分发权。
