# M3 联网事实核查设计

## 目标

为 Goldbook 增加一个可复用的联网事实核查子系统。只有当视频观点依赖外部事件、公告、数据发布或其他无法由本地行情直接验证的事实时，系统才启动该子系统。MiniMax-M3 负责识别核查需求、规划搜索、交叉比对证据和选择预测分支；程序负责限制工具调用、保存来源、校验结构并执行价格条件。

首个验收案例是视频 `BV1uhuy6AEA6`：核查视频发布当晚的美国 CPI 数据相对市场预期是利好、利空还是中性，再分别判断 4350–4400 整理分支或 4400 → 4450 → 4500 上破分支是否被触发及兑现。

## 范围

### 包含

- 识别必须联网验证的条件事实，例如 CPI、非农、利率决议、财报、监管裁决或案件结果。
- 通过 MiniMax 官方搜索端点（其 `web_search` MCP 使用的同一后端）和现有 `MINIMAX_API_KEY` 搜索网页。
- 让 M3 生成查询、读取搜索结果、要求补充搜索并输出结构化结论。
- 保存每项证据的标题、URL、发布日期、摘要、查询词和抓取时间。
- 将“事实条件是否触发”和“触发后的价格预测是否兑现”分开判定。
- 在单视频页面展示事实、来源、冲突、条件分支和价格核验。
- 为未来资产与事件复用同一个事实核查接口。

### 不包含

- 不为 BLS、FRED、Trading Economics 等网站分别编写业务适配器。
- 不对普通趋势预测或纯点位预测进行联网搜索。
- 不把搜索摘要当作结构化行情的长期替代品。
- 不允许 M3 在没有可定位来源时补写事实。
- 不将“条件未触发”统计为预测未命中。

## 总体架构

数据流分为五个阶段：

1. `FactCheckGate` 读取视频发布时间、结构化观点和字幕证据，判断是否存在外部条件。
2. `M3FactCheckAgent` 把 `web_search(query)` 暴露为只读工具，让 M3 规划并执行有限搜索。
3. `EvidenceValidator` 校验 URL、标题、摘要、时间、查询来源及结论引用，拒绝无来源事实。
4. `ConditionalClaimResolver` 根据事实结论选择 `triggered`、`not_triggered`、`conflicting` 或 `insufficient` 分支。
5. `ConditionalPriceEvaluator` 只对已触发分支执行价格条件；其他分支保留为反事实说明，不进入准确率。

模块边界：

- `goldbook/fact_check.py`：领域模型、门控、证据校验和条件分支解析。
- `goldbook/minimax_search.py`：MiniMax 通用搜索 HTTP 边界、超时、重试和结果净化。
- `goldbook/fact_check_agent.py`：M3 搜索循环、结构化输出解析和证据绑定。
- `goldbook/db.py`：事实核查任务、搜索证据、结果和 claim 关联的持久化。
- `goldbook/jobs.py`：后台执行事实核查，不在 Web 请求线程联网。
- `goldbook/web.py` 与 `goldbook/templates/video.html`：呈现核查过程与结论。

## 启动条件

门控输出 `FactCheckNeed`：

- `required`: 是否需要联网事实核查。
- `event_description`: 原文中的事件描述。
- `expected_at`: 由视频发布时间和“今晚/明天/发布后”等时间表达解析出的 UTC 时间范围。
- `condition_branches`: 每个条件分支关联的 claim ID。
- `reason`: 启动或跳过的原因。

满足以下任一条件才允许启动：

- 观点显式使用“如果/若/取决于/公布后”等条件，并引用外部事件。
- 观点依赖一个在视频发布时尚未公开的数值或结果。
- 本地行情无法单独判断条件真假。

纯技术分析、无外部条件的趋势和点位观点必须返回 `required=false`。

## M3 搜索代理

MiniMax-M3 不被视为自带互联网。应用向模型提供 `web_search` 工具，并直接调用 MiniMax 官方 MCP 源码所使用的 `/v1/coding_plan/search` 后端。M3 决定搜索词和是否需要补充查询，工具只执行搜索并返回结果。直接使用同源 HTTP 边界可规避官方 MCP 包与 `mcp 2.0` 的当前启动兼容问题，也避免本地 `uvx` 子进程依赖。

限制：

- 每个事实核查最多 6 次搜索。
- 无依赖的查询最多 3 路并行。
- 同一标准化查询只执行一次。
- 单次搜索 20 秒超时；最多一次可重试的传输重试。
- 搜索服务错误、429、超时或格式错误不得被转写为事实结论。
- 搜索结果和模型输出中不得记录或返回 API Key。

M3 的最终 JSON 必须包含：

- `question`
- `event_name`
- `event_time_utc`
- `facts[]`：名称、actual、forecast、previous、unit
- `impact`：`supportive`、`adverse`、`neutral`、`conflicting` 或 `insufficient`
- `reasoning_summary`
- `evidence_ids[]`
- `branch_decisions[]`：claim ID、`triggered` 或 `not_triggered`、理由
- `confidence`

若事实值来自不同口径，例如 headline CPI 与 core CPI，必须分别列出；M3 不得把不同口径合并成单一数字。来源矛盾时必须输出 `conflicting`。

## 证据规则

每个用于结论的事实必须引用已保存的搜索结果 ID。一个搜索结果必须具有有效的 HTTP(S) URL、非空标题和非空摘要。页面发布日期未知时可以保存，但会降低置信度。

默认要求至少两个相互独立的结果支持核心事实。若只有一个结果，状态为 `insufficient`，除非该结果明确标识为事件发布主体且内容包含所需实际值和发布时间。系统不维护站点专用解析器，但会保存域名，让 UI 清楚展示来源。

不保存完整网页，只保存核查所需摘要与元数据。所有摘要限制长度，错误信息经过净化，禁止保存 Cookie、Authorization 或密钥字符串。

## 条件分支与评分

事实核查状态与价格评价状态相互独立：

- `triggered`：事实条件成立，相关 claim 进入价格评价。
- `not_triggered`：事实条件不成立，显示“条件未触发”，不算命中或未命中。
- `conflicting`：来源冲突，显示“事实证据冲突”，不评分。
- `insufficient`：证据不足或搜索失败，显示“事实证据不足”，不评分。

条件语义必须区分 `supportive`、`adverse` 与 `not_supportive`。例如“若 CPI 利好则突破；若不利好则整理”中，`neutral` 会触发 `not_supportive` 分支，但不会触发仅限 `adverse` 的分支。

已触发分支使用事件实际发布时间作为观察起点。现有结构化行情足够时，程序继续执行点位、区间和顺序条件；没有对应资产的结构化行情时，M3 可以搜索价格事实并形成带来源的事实结论，但该结论标记为 `search_based`，与程序化 OHLC 判定分开统计。

## CPI 首个案例

对 `BV1uhuy6AEA6`：

1. 搜索 2026-08-12 美国 CPI 的发布时间、headline/core 的月率和年率、实际值、市场预期及前值。
2. 把不同口径分别列出，判断结果是利好黄金、利空黄金、中性还是存在冲突。
3. 解析原文：
   - `supportive` 分支：4400 → 4450 → 4500。
   - `not_supportive` 分支：4350–4400 整理。
4. 从公布时间而非视频发布时间开始核验价格路径。
5. 未触发分支展示为反事实，不进入命中率。

## 持久化

新增三类记录：

- `fact_check_runs`：run ID、bvid、事件描述、状态、模型、次数、创建/完成时间、净化错误。
- `fact_check_evidence`：evidence ID、run ID、query、title、URL、domain、published_at、snippet、fetched_at。
- `fact_check_results`：run ID、结构化事实、impact、理由、置信度、证据 ID、分支决定。

结果以视频最新 claim revision 为边界。观点被人工修订或重新提取后，旧结果保留审计记录但不再驱动当前评分。相同 revision 的完成结果可缓存；用户可手动“重新联网核查”。

## 后台任务与界面

事实核查只能在后台 worker 中运行。视频页提供“联网事实核查”按钮和状态；请求线程仅入队。

完成后页面在判定链之前展示：

- 事件与实际公布时间。
- 实际值 / 市场预期 / 前值表格。
- M3 的事件影响结论与简短理由。
- 使用的搜索词及可点击来源。
- 来源冲突或证据不足警告。
- 每个预测分支的“已触发/未触发”。
- 已触发分支的价格判定链与事件后价格图。

UI 不显示内部 prompt、模型思维过程、密钥或原始供应商响应。

## 错误处理

- 搜索服务不可用：任务失败并保留可重试状态，页面显示“搜索服务不可用”。
- 搜索成功但证据不足：任务完成，事实状态为 `insufficient`。
- M3 输出不符合 schema：重试一次；仍失败则任务失败并可重试。
- 来源冲突：不得自动选择更符合预测的一方。
- 价格数据不足：事实条件仍可判定，价格部分单独显示待补数据。
- 用户取消：停止后续搜索，不删除已经保存的审计证据。

## 安全与隐私

- Key 只从服务端环境读取，不进入模板、日志、数据库或搜索词。
- 仅允许 HTTP(S) 搜索结果；阻止本地地址、环回地址、私网地址和 `file:` URL，防止 SSRF。
- 不自动登录网站，不使用 Cookie，不绕过付费墙或访问控制。
- 页面明确标注“自动事实核查，不构成投资建议”。

## 测试与验收

测试全部使用确定性 fake 搜索和 fake M3，不依赖真实网络：

- 普通观点不会启动联网核查。
- CPI 条件观点会生成事实核查任务。
- M3 可发起多次搜索但不能超过 6 次或 3 路并发。
- 证据没有 URL、摘要或引用关系时拒绝结论。
- 两个来源一致时选择正确分支。
- 来源冲突、单源不足、MCP 失败均不会被算作未命中。
- `not_supportive` 正确包含 neutral，`adverse` 不包含 neutral。
- 条件未触发不进入命中率。
- claim revision 变化后旧核查结果不再生效。
- Web 请求只入队且受 CSRF 保护。
- 页面展示来源和分支，不泄露 Key 或内部响应。
- SSRF 地址被拒绝。

完成离线测试后，使用当前 Key 对 `BV1uhuy6AEA6` 做一次受控真实核查，保存来源并人工检查页面；真实探针失败不得伪装为功能通过。

## 依赖与部署

通过 MiniMax 官方 `web_search` MCP 所调用的同一搜索端点使用现有 Key，不增加本地 MCP helper 依赖。若当前 Key 套餐不支持搜索，页面显示明确阻塞，不回退为无来源猜测。现有本地绑定、并发上限、密钥加载和日志净化规则保持不变。

参考：

- MiniMax Web Search MCP: https://platform.minimax.io/docs/token-plan/mcp-guide
- MiniMax M 系列工具调用建议: https://platform.minimax.io/docs/token-plan/prompting-best-practices
