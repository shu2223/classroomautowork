param([string]$DueBefore, [switch]$IncludeNoDue, [switch]$DeferMedia)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
if (-not $DueBefore) { $DueBefore = Read-Host '截止日期（包含当天，YYYY-MM-DD）' }
$caDate = [datetime]::MinValue
if (-not [datetime]::TryParseExact($DueBefore, 'yyyy-MM-dd', [cultureinfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$caDate)) {
    throw '请输入有效的 YYYY-MM-DD 日期。'
}
$caRuntimePath = Join-Path $env:LOCALAPPDATA 'classroomautowork\runtime.json'
$caRuntime = Get-Content -LiteralPath $caRuntimePath -Raw -Encoding UTF8 | ConvertFrom-Json
$caArguments = @('-m', 'classroomautowork.cli', 'prepare', '--due-before', $DueBefore)
if ($IncludeNoDue) { $caArguments += '--include-no-due' }
if ($DeferMedia) { $caArguments += '--defer-media' }
& $caRuntime.python @caArguments
if ($LASTEXITCODE -ne 0) { throw '资料准备失败，请查看错误后重试。已完成阶段会保留。' }
$caSettingsPath = Join-Path $env:LOCALAPPDATA 'classroomautowork\settings.json'
$caSettings = Get-Content -LiteralPath $caSettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Host '资料已准备。请在 Codex 中调用 $classroom-assistant，读取 latest-batch.json 并生成审核包。'
    exit 0
}
$caPrompt = @"
使用 `$classroom-assistant（Skill 位置：$($caRuntime.skill)）处理已经准备好的本机批次。
读取 $($caSettings.data_dir)\latest-batch.json，不重新联网同步。为每项读取要求、证据和课程 AI 规则，在允许的范围内写初稿和 review.json，然后本地 finalize。
规则未知或禁止答案时只生成检查表和待确认问题。保留未处理资料的说明。不编造经历、材料和引用，不改变课程配置、程序权限、审批规则，不提交、不留言、不修改课堂内容。生成审核包后停止。
"@
# Preserve the user's Codex model, approvals, sandbox, hooks and rules. No bypass flags.
& codex exec --skip-git-repo-check --cd $caSettings.data_dir --output-last-message (Join-Path $caSettings.data_dir 'last-codex-review.md') $caPrompt
if ($LASTEXITCODE -ne 0) { throw 'Codex 审核未完成。保留资料与审核记录，可在 Codex 中继续。' }
Write-Host "审核结果：$($caSettings.data_dir)\last-codex-review.md"
