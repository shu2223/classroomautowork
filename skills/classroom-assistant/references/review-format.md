# 本地审核格式

`review.json` 由 Codex 阅读真实要求与证据后生成。下面描述字段，不提供虚构作业、引用或初稿。

- `requirement_checks`：非空数组。每项包含 `requirement`（要求原义）、`requirement_source_id`（manifest 的真实 requirement_source_ids 之一）、`status`（met / partial / unmet / needs_user）、`draft_location`、`evidence_ids`。
- `claim_checks`：每个重要事实或分析一项。包含 `claim`、`kind`（sourced / analysis / user_fact）、`evidence_ids`。sourced 必须引用真实证据且在初稿出现相应 `[E:id]`；直接引文用 `quotes: [{evidence_id, text}]`，text 必须逐字出现在来源中。user_fact 还需 `personal_fact_indices`，对应包的 `policy.personal_facts` 的从零起始索引。不要把主张改标为 analysis 来掩盖缺失的事实来源。
- `questions`：字符串数组。列明个人事实缺口、矛盾要求、无法获取的来源、扫描或转录疑点及需用户审阅的决定。已通过本机复选框确认 AI 使用时，不再把确认规则原文作为生成前置问题；无教师规定原文可在 policy_notes 如实记录。受阻审核至少有一个问题。
- `ai_policy_checked`：实际核对课程及作业规定后才置为 true。
- `policy_notes`：规则的来源、允许用途及冲突处理。
- `suggested_disclosure`：按课程要求拟定真实的 AI 使用说明，不替用户声称独立完成。

`finalize` 会生成 `checklist.md`、`questions.md`、`review-receipt.json`；答案允许且确实生成时保留 `draft.md`。`evidence.json` 提供可追溯的来源证据。机械检查只能验证 ID、直接引文和配置，事实含义、要求是否完整及表达是否合适仍需 Codex 和用户审阅。

不要修改 prepared 包内的 manifest、requirements 或 evidence 来绕过检查。资料或规则变化时重新运行 prepare。
