# 本地审核格式

`review.json` 由 Codex 阅读真实要求与证据后生成。下面描述字段，不提供虚构作业、引用或初稿。

- `requirement_checks`：非空数组。每项包含 `requirement`（要求原义）、`requirement_source_id`（manifest 的真实 requirement_source_ids 之一）、`status`（met / partial / unmet / needs_user）、`draft_location`、`evidence_ids`。
- `claim_checks`：每个重要事实或分析一项。包含 `claim`、`kind`（sourced / analysis / user_fact）、`evidence_ids`。sourced 必须引用真实证据且在初稿出现相应 `[E:id]`；直接引文用 `quotes: [{evidence_id, text}]`，text 必须逐字出现在来源中。user_fact 还需 `personal_fact_indices`，对应 policy.personal_facts、supplement.personal_facts、可信身份合并后的从零起始索引。不要把主张改标为 analysis 来掩盖缺失的事实来源。
- `questions`：字符串数组。列明个人事实缺口、矛盾要求、无法获取的来源、扫描或转录疑点及需用户审阅的决定。已通过本机复选框确认 AI 使用时，不再把确认规则原文作为生成前置问题；无教师规定原文可在 policy_notes 如实记录。受阻审核至少有一个问题。
- `ai_policy_checked`：实际核对课程及作业规定后才置为 true。
- `policy_notes`：规则的真实来源及用户作答选择；课程 AI 禁止或限制仅作说明，不阻止已勾选的作答。
- `suggested_disclosure`：按课程要求拟定真实的 AI 使用说明，不替用户声称独立完成。

`finalize` 会生成 `checklist.md`、`questions.md`、`review-receipt.json`；答案允许且确实生成时保留 `draft.md`。`evidence.json` 提供可追溯的来源证据。机械检查只能验证 ID、直接引文和配置，事实含义、要求是否完整及表达是否合适仍需 Codex 和用户审阅。

不要修改 prepared 包内的 manifest、requirements 或 evidence 来绕过检查。资料或规则变化时重新运行 prepare。

`document_answers` 是前端结果 schema 的独立数组，不属于 `review.json`。每项复制原生答案栏的 document_id、field_id 和 context_sha256，并提供适合该栏的单段 text。内部证据标记保留在本机 draft/checklist 中，原文档只填写作业答案。写入后给出原文档审阅入口，证据包下载可选。

`form_answers` 为原表单逐栏答案数组：form_url、entry_id、context_sha256 逐字复制程序字段；values 为原选项或文字，needs_user 和 review_note 单独记录审阅提示。原表单不放内部引用或审核说明。程序负责原生预填并打开，用户在原页面审阅和手动提交；不得将打开浏览器等同于已经回读或保存草稿。

实际 `document_answers.text` 与 `form_answers.values` 中的新写正文不用「・」，用「、」或自然句子连接；原选项、专名、直接引文和可信身份原值不改。仅在当前作业明确要求引用时标注引用；未要求时绝不追加「参照：」「参考：」「出典：」、来源文件名、引用尾注或参考文献列表。正常句子中按题意说明讲义内容可以保留，不另外列来源。本机 draft/review 继续保留完整证据及页码/时间戳，审核解释和统一的真实性声明只放 questions/review_note，必要的事实限定留在相关答案句中。
