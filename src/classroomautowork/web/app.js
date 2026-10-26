"use strict";
const $ = id => document.getElementById(id);
const state = {documentsAuthorized: false, pending: null, jobs: [], policies: {}, selected: new Set(), active: null, focus: null, view: "pending", busy: false, preview: null, policyCourse: null, images: [], models: null, modelsLoading: false, hideOverdue: localStorage.getItem("classroom-hide-overdue") === "true"};
const activeStates = new Set(["queued", "running", "stopping"]);
const labels = {queued: "等待开始", running: "正在处理", stopping: "正在暂停", waiting: "等待处理", preparing: "读取题目与资料", prepared: "资料已准备", drafting: "Codex 正在分析", ready: "答案已生成 · 打开答案审阅", filling_form: "正在自动填入原表单", form_opened: "已打开预填表单 · 在原表单审阅", needs_form: "初稿已保留 · 表单填入需重试", filling_document: "正在填入原文档", document_ready: "已填入 · 打开文档审阅", document_needs_user: "已填入可用答案 · 请审阅", needs_document: "初稿已保留 · 填入需处理", needs_user: "历史结果 · 可自动重试", materials_ready: "资料已整理 · 未调用 AI", paused: "已暂停", interrupted: "上次已中断", failed: "处理失败", completed: "处理已结束", completed_with_issues: "部分处理未完成"};
const keyOf = item => item.course_id + ":" + item.assignment_id;
let token = location.hash.slice(1);
if (token) { sessionStorage.setItem("classroom-access", token); history.replaceState(null, "", location.pathname); }
else token = sessionStorage.getItem("classroom-access") || "";
let polling = null, toastTimer = null;
let pollInFlight = false;
const connection = {lastSuccess: 0, error: null};
let shutdownPolling = null, shutdownPollInFlight = false, shutdownRequestVersion = 0;
state.shutdown = null; state.shutdownPending = false; state.shutdownError = null; state.shutdownReceivedAt = 0;

// Pure progress presentation; exercised by offline state tests.
function durationLabel(ms) { const s = Math.max(0, Math.floor(ms / 1000)); return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`; }
function ageOf(at, now) { const value = Date.parse(at); return Number.isFinite(value) ? Math.max(0, now - value) : null; }
function documentContentGaps(fill) {
  return ["remaining_required_fields", "unrecognized_questions"].flatMap(key => fill?.[key]?.length ? fill[key] : (fill?.documents || []).flatMap(doc => doc[key] || []));
}
function documentAnswersIncomplete(item) {
  const fill = item?.document_fill;
  return !!(item?.requires_regeneration || documentContentGaps(fill).length || fill?.status === "partial" || fill?.documents?.some(doc => doc.status === "partial"));
}
function formContentGaps(fill) {
  if (Object.hasOwn(fill || {}, "remaining_answer_fields")) return fill.remaining_answer_fields || [];
  return fill?.forms?.length ? fill.forms.flatMap(form => Object.hasOwn(form, "remaining_answer_fields") ? form.remaining_answer_fields || [] : (form.remaining_fields || []).filter(field => field.required)) : (fill?.remaining_fields || []).filter(field => field.required);
}
function formUserInputGaps(fill, keys = ["user_input_fields", "unsupported_fields"]) {
  return keys.flatMap(key => Object.hasOwn(fill || {}, key) ? fill[key] || [] : (fill?.forms || []).flatMap(form => form[key] || [])).filter(field => field.required);
}
function formSourceGaps(fill) {
  return formContentGaps(fill).filter(field => field.gap_reason === "missing_source");
}
function formSourceDetail(fields) {
  return [...new Set(fields.map(field => (field.review_note || "").replace(/^第\d+页第\d+题[：:]\s*/, "").trim()).filter(Boolean))].slice(0, 2).join("；").slice(0, 180);
}
function retryActionLabel(job) {
  return job.items?.some(item => formSourceGaps(item.form_fill).length) ? "再次读取资料并补全" : "重试 / 继续";
}
function formGenerationIncomplete(item) {
  const mapping = Object.hasOwn(item?.form_fill || {}, "form_mapping_complete") ? item.form_fill.form_mapping_complete : item?.generation?.form_mapping_complete;
  return !!((item?.form_requires_regeneration && item?.form_fill?.form_mapping_complete !== true) || mapping === false || formContentGaps(item?.form_fill).length);
}
function formAnswersIncomplete(item) {
  return formGenerationIncomplete(item) || !!formUserInputGaps(item?.form_fill).length;
}
function answersIncomplete(item) {
  return documentAnswersIncomplete(item) || formAnswersIncomplete(item);
}
function inputAction(item) {
  if (!item.package) return null;
  const count = formUserInputGaps(item.form_fill, ["user_input_fields"]).length;
  return {key:keyOf(item), count, label:count ? `补充 ${count} 栏必填信息` : "调整答案或说明（可选）"};
}
function completedSuccessor(job, jobs) {
  if (activeStates.has(job.status) || job.items?.length !== 1 || !["needs_user", "document_needs_user", "needs_form", "needs_document"].includes(job.items[0].status)) return null;
  const key = keyOf(job.items[0]);
  const latest = [...jobs].filter(x=>["fill", "review"].includes(x.kind) && x.created_at > job.created_at && x.items.some(item=>keyOf(item) === key)).sort((a,b)=>b.created_at.localeCompare(a.created_at))[0];
  const item = latest?.items.find(x=>keyOf(x) === key);
  return ["form_opened", "document_ready"].includes(item?.status) && !answersIncomplete(item) ? {job:latest,item} : null;
}
function aiActivity(generation) {
  if (!generation?.thread_id || !/^[a-f0-9-]{36}$/i.test(generation.thread_id)) return null;
  const completed = (generation.events || []).filter(x=>x.status === "completed");
  const tools = completed.filter(x=>["commandExecution", "mcpToolCall", "webSearch", "imageView", "dynamicToolCall"].includes(x.type)).length;
  const updates = Number.isSafeInteger(generation.activity_count) && generation.activity_count > 0 ? generation.activity_count : 0;
  return {generation, text:[updates ? `已收到 ${updates.toLocaleString("zh-CN")} 次实际活动更新` : "真实会话已创建，等待模型活动更新", tools ? `已完成 ${tools} 次工具活动` : null].filter(Boolean).join(" · ")};
}
function estimateDuration(seconds) { return durationLabel(Math.max(1, Math.ceil(seconds)) * 1000); }
function legacyTranscriptionEstimate(job, now) {
  const p = job.progress || {};
  if (p.stage !== "transcribe" || p.unit !== "segments" || !p.filename || !Number.isSafeInteger(p.current) || !Number.isSafeInteger(p.total) || p.current < 0 || p.current >= p.total) return null;
  const started = Date.parse(p.started_at), prefix = `${p.filename} · `, segments = new Map();
  if (!Number.isFinite(started)) return null;
  for (const event of job.events || []) {
    const at = Date.parse(event.at);
    if (!Number.isFinite(at) || at < started || at > now || !event.message?.startsWith(prefix)) continue;
    const message = event.message.slice(prefix.length);
    const begin = message.match(/^正在处理录像第 (\d+)\/(\d+) 段（([\d.]+)[–-]([\d.]+) 秒）$/);
    const infer = message.match(/^Buzz 大模型 .+ 正在本机转录第 (\d+)\/(\d+) 段，完成后缓存$/);
    const done = message.match(/^已完成并缓存 (\d+)\/(\d+) 段带时间戳转录$/);
    const match = begin || infer || done;
    if (!match || Number(match[2]) !== p.total) continue;
    const index = Number(match[1]);
    if (index < 1 || index > p.total) continue;
    if (begin) segments.set(index, {started:at, offset:Number(begin[3]), seconds:Number(begin[4]) - Number(begin[3])});
    else {
      const segment = segments.get(index);
      if (segment && infer) segment.infer = at;
      if (segment && done) segment.done = at;
    }
  }
  // Cached segments have no inference event. Never divide an old stage start by
  // a completion count, which can contain an arbitrary number of cache hits.
  const samples = [...segments.entries()].filter(([index, s]) => index <= p.current && s.seconds > 0 && s.infer >= s.started && s.done - s.infer >= 1000).slice(-5).map(([, s]) => ({rate:(s.done - s.started) / 1000 / s.seconds, seconds:s.seconds}));
  const current = segments.get(p.current + 1);
  if (samples.length < 2 || !current || current.done || current.seconds <= 0) return null;
  const weight = samples.reduce((sum, _, index) => sum + index + 1, 0);
  const rate = samples.reduce((sum, sample, index) => sum + sample.rate * (index + 1), 0) / weight;
  const lowerRate = Math.min(...samples.map(s => s.rate)) * 0.85, upperRate = Math.max(...samples.map(s => s.rate)) * 1.25;
  const elapsed = (now - current.started) / 1000;
  const raw = {observed_at:new Date(now).toISOString(), state:"estimating", overall:{scope_complete:false, unknown_tasks:1}, stage:{unit:"media_seconds", speed:1 / rate, source:"live", confidence:"low"}};
  if (elapsed > current.seconds * upperRate) return {...raw, state:"calibrating", reason:"当前分段已超过已测耗时范围，等待本段完成后重新校准。"};
  const futureSegments = p.total - p.current - 1;
  const segmentSeconds = Math.max(...samples.map(s => s.seconds), current.seconds);
  // The segment's start offset is stable. A live media counter can already
  // include this segment's progress, so subtracting elapsed from it again
  // would count the same work twice.
  const measuredTotal = Number.isFinite(p.media_total_seconds) && p.media_total_seconds > current.offset;
  const remainingMedia = measuredTotal ? p.media_total_seconds - current.offset : current.seconds + futureSegments * segmentSeconds;
  const shortestMedia = measuredTotal || !futureSegments ? remainingMedia : Math.max(current.seconds, remainingMedia - segmentSeconds);
  if (!futureSegments && elapsed >= remainingMedia * rate) return {...raw, state:"calibrating", reason:"最后一段已超过本次预测耗时，等待本段完成后重新校准。"};
  raw.stage.remaining_seconds = Math.max(1, Math.round(remainingMedia * rate - elapsed));
  raw.stage.lower_seconds = Math.max(1, Math.floor(shortestMedia * lowerRate - elapsed));
  raw.stage.upper_seconds = Math.max(raw.stage.remaining_seconds, Math.ceil(remainingMedia * upperRate - elapsed));
  Object.assign(raw.overall, {known_remaining_seconds:raw.stage.remaining_seconds, known_lower_seconds:raw.stage.lower_seconds, known_upper_seconds:raw.stage.upper_seconds, known_task_count:1});
  raw.reason = `依据本文件最近 ${samples.length} 段的实际转录与音轨准备耗时估算。${measuredTotal || !futureSegments ? "" : "未报告的末段按完整分段上限计入，实际可能更短。"}其余附件、课程和作答耗时尚未计入。`;
  return raw;
}
function estimateView(job, disconnected, now, stage) {
  const legacyEstimate = !job.estimate ? legacyTranscriptionEstimate(job, now) : null;
  const raw = job.estimate || legacyEstimate || {}, step = raw.stage || {}, overall = raw.overall || {};
  const nonnegative = value => Number.isFinite(value) && value >= 0;
  const positive = value => nonnegative(value) && value > 0;
  const active = activeStates.has(job.status), observed = Date.parse(raw.observed_at);
  let status = raw.state || "calibrating";
  if (!active) status = job.status === "completed" ? "complete" : job.status === "paused" ? "paused" : "stopped";
  else if (job.status === "stopping" || job.approval || ["approval", "authorization"].includes(stage)) status = "paused";
  else if (disconnected || (Number.isFinite(observed) && (now - observed > 12000 || observed - now > 5000))) status = "stale";
  else if (!Number.isFinite(observed)) status = "calibrating";
  else if (!["estimating", "calibrating", "paused", "stale"].includes(status)) status = "calibrating";
  const usable = active && status === "estimating";
  const range = value => nonnegative(value.lower_seconds) && positive(value.upper_seconds) && value.lower_seconds <= value.remaining_seconds && value.upper_seconds >= value.remaining_seconds ? `通常 ${estimateDuration(value.lower_seconds)}–${estimateDuration(value.upper_seconds)}` : null;
  const wholeStageRemaining = usable && positive(step.remaining_seconds) ? step.remaining_seconds : null;
  const phaseRemaining = usable && positive(step.phase_remaining_seconds) ? step.phase_remaining_seconds : null;
  const stageRemaining = wholeStageRemaining ?? phaseRemaining;
  const overallRemaining = usable && positive(overall.remaining_seconds) ? overall.remaining_seconds : null;
  const knownRemaining = usable && positive(overall.known_remaining_seconds) ? overall.known_remaining_seconds : null;
  const unknown = Number.isSafeInteger(overall.unknown_tasks) && overall.unknown_tasks > 0 ? overall.unknown_tasks : 0;
  const scopeComplete = overall.scope_complete === true && !unknown;
  const percent = status === "complete" ? 100 : usable && scopeComplete && overallRemaining !== null && nonnegative(overall.percent) && overall.percent < 100 ? overall.percent : null;
  let headline = {calibrating:"正在校准剩余时间", paused:job.status === "stopping" ? "正在暂停 · 剩余时间已冻结" : "等待继续 · 暂不估计完成时间", stale:"等待新状态 · 剩余时间已暂停更新", complete:"本次处理已结束", stopped:"本次处理已停止"}[status] || "正在估计剩余时间";
  if (overallRemaining !== null) headline = `${scopeComplete ? "预计总共还需" : "已知工作预计还需"} ${estimateDuration(overallRemaining)}`;
  else if (knownRemaining !== null) headline = `已知工作预计还需 ${estimateDuration(knownRemaining)}（其余未计入）`;
  else if (usable) headline = stageRemaining === null ? "正在重新校准剩余时间" : "总剩余时间尚待估计";
  let finish = null;
  if (overallRemaining !== null && scopeComplete) {
    const target = observed + overallRemaining * 1000;
    if (Number.isFinite(target) && target <= 8640000000000000) finish = `预计完成 ${new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Tokyo", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).format(new Date(target))}（日本时间）`;
  }
  let speed = null;
  const phase = step.phase || job.progress?.phase;
  if (usable && positive(step.speed)) {
    const speedNumber = value => value >= 10 ? value.toFixed(1) : value.toFixed(2);
    speed = step.unit === "media_seconds" ? `${phase === "prepare" ? "音频准备" : "Buzz"} ${speedNumber(step.speed)}× 实时` : step.unit === "bytes" ? `下载 ${speedNumber(step.speed / 1048576)} MB/s` : step.unit === "pages" ? `文档处理 ${speedNumber(step.speed)} 页/s` : null;
  }
  const source = {live:"本次实测速率", history:"同类历史耗时", mixed:"本次实测 + 同类历史"}[step.source];
  const confidence = {low:"可信度较低", medium:"可信度中等", high:"可信度较高"}[step.confidence];
  const stepLabel = stage === "transcribe" ? "本文件转录" : "本步骤";
  const phaseLabel = stage === "transcribe" ? phase === "prepare" ? "本段音频准备" : "本段语音识别" : "当前子步骤";
  const stageText = stageRemaining === null ? null : `${wholeStageRemaining === null ? phaseLabel : stepLabel}预计还需 ${estimateDuration(stageRemaining)}`;
  const phaseText = wholeStageRemaining !== null && phaseRemaining !== null && stage === "transcribe" ? `${phaseLabel}预计还需 ${estimateDuration(phaseRemaining)}` : null;
  const notes = [];
  if (usable && !scopeComplete) notes.push(legacyEstimate ? "还有课程或附件尚未发现完整工作量，总剩余时间尚不能确定。" : unknown ? `还有 ${unknown} 项工作量待确认，暂不显示总百分比。` : "完整工作量尚未确认，总时间会随资料发现而调整。");
  if (status === "calibrating") notes.push("正在收集本机速度或等待足够的同类记录，暂不提供不可靠的倒计时。");
  if (!job.estimate && !legacyEstimate && active && status === "calibrating") notes.push("本次任务由旧版服务启动，尚未提供完整时间估计；有足够的分段实测记录后可先估计当前文件。其余课程或附件工作量尚未确认。");
  if (status === "stale") notes.push("旧估计已冻结，收到新状态后重新计算。");
  const reasons = {measured_rate:"根据本机处理速度与同类记录动态估算。", awaiting_measurements:"速度样本不足，等待处理进展后重新估计。", remaining_phases_uncalibrated:"已测到当前速度；其他处理阶段仍在校准，暂时不能估计完整剩余时间。", progress_not_updated:"处理计数暂未更新，收到新进展后重新估计。", waiting_for_resume:"继续运行后重新计算剩余时间。", complete:"处理已结束。", stopped:"处理已停止。"};
  const reason = typeof raw.reason === "string" ? raw.reason.trim() : "";
  if (active && reason) { const message = reasons[reason] || (/[\u3400-\u9fff]/u.test(reason) ? reason : null); if (message) notes.push(message); }
  const finished = (job.items || []).filter(x => ["ready", "document_ready", "document_needs_user", "form_opened", "needs_form", "needs_document", "needs_user", "materials_ready", "failed"].includes(x.status)).length;
  const countText = (job.items || []).length ? `已处理 ${finished}/${job.items.length} 项` : "正在确认工作量";
  const knownRange = knownRemaining === null ? null : range({remaining_seconds:knownRemaining, lower_seconds:overall.known_lower_seconds, upper_seconds:overall.known_upper_seconds});
  return {state:status, headline, percent, progressTitle:percent === null ? "作业项进度" : "总进度（估计）", percentText:percent === null ? countText : `${Math.floor(percent)}%`, scopeComplete, unknown, stageRemaining, phaseRemaining, overallRemaining, knownRemaining, stageText, phaseText, stageRange:wholeStageRemaining === null ? null : range(step), range:overallRemaining === null ? knownRange : range(overall), finish, speed, basis:usable ? [source,confidence].filter(Boolean).join(" · ") || null : null, note:notes.join(" ") || null};
}
function progressView(job, network, now = Date.now()) {
  const items = job.items || [], p = job.progress || {}, active = activeStates.has(job.status);
  const item = items.find(x => ["preparing", "prepared", "drafting", "filling_document", "filling_form"].includes(x.status)) || items.find(x => x.status === "waiting");
  let stage = p.stage || (item?.status === "filling_form" ? "form_fill" : item?.status === "filling_document" ? "fill" : item?.status === "drafting" ? "ai" : "prepare");
  if (job.kind === "fill" && item?.status !== "filling_form") stage = "fill";
  if (job.kind === "document_auth") stage = "authorization";
  if (job.kind === "refresh") stage = "course_sync";
  if (job.approval) stage = "approval";
  if (!job.progress && !active && items.some(x => x.status === "failed" && x.generation?.turn_status === "completed")) stage = "validate";
  const descriptions = {
    prepare: ["准备题目与课堂资料", "读取作业要求、同课程资料、历史作业和公告；这一阶段还未调用 AI。"],
    course_sync: ["同步课程内容", "读取学校账户内的课程记录，查找本次作业的来源。"],
    course_prepared: ["本课程资料已准备", "本课程的资料与附件已核对，继续整理所选作业。"],
    form_read: ["读取表单的全部题目", "只读获取 Google Forms 所有分页的题目和选项，身份页之后的题目也会加入本次作业要求。"],
    attachment: ["核验课程附件", "检查文件权限、版本与缓存，只处理新增或修改的材料。"],
    download: ["下载课程附件", "下载量来自真实收到的字节；大文件可能耗时，之后还要校验完整性。"],
    download_verify: ["校验附件完整性", "核对下载大小和校验值，避免使用不完整文件。"],
    extract: ["提取文档文字和页图", "逐页读取内容并保留必要图像，供 Codex 阅读题目与课堂资料。"],
    transcribe: ["Buzz 大模型正在本机转录录像", "使用本地 Whisper 大模型分段识别语音并保存时间戳。单段处理期间计数可能暂时不变，完成后继续更新。"],
    attachment_done: ["附件内容已准备", "材料已处理并缓存，继续检查其他附件。"],
    attachment_failed: ["这份附件未能处理完成", "已保留具体原因，继续检查其他资料；请在处理记录中核对受影响的附件。"],
    document_inspect: ["核对作业填写方式", "检查有无个人 Google 文档答案栏；模型逐栏作答后，自动填入个人文档或原表单。"],
    document_create: ["创建本人作业文档", "在当前作业页面创建个人ドキュメントシート，核验实际附件和编辑权限后继续作答；不会提交。"],
    ai: ["Codex 正在阅读与作答", "使用所选模型分析真实题目和资料。AI 没有可测量的总百分比；显示实际活动与会话入口。"],
    validate: ["核验 AI 结果", "检查来源、格式与题目完整性，通过后才填入文档。"],
    form_fill: ["自动填入原 Google 表单", "核对原题与选项，打开已预填答案的原表单；在原表单审阅，程序不会点击提交。浏览器是否保存草稿以 Google 页面提示为准。"],
    fill: ["填写并回读 Google 文档", "填写个人作业的答案栏，回读核验后由你审阅并手动提交。"],
    approval: ["等待你的操作审批", "请在作业助手查看具体请求并批准或拒绝；等待期间不会继续。"],
    authorization: ["等待 Google 授权", "请在 Google 页面完成学校账户授权，然后返回助手。"],
  };
  let [title, explanation] = descriptions[stage] || descriptions.prepare;
  if (stage === "transcribe" && p.phase === "prepare") { title = "正在准备 Buzz 转录音频"; explanation = "先从录像提取本段音频，再交给 Buzz 识别；音频准备与语音识别分别计量。"; }
  const legacy = active && !job.approval && !job.progress && /^Checking attachment /.test(job.message || "");
  if (legacy) { title = "正在处理课程附件"; explanation = "这次由旧版启动，未报告附件内部的子步骤。下载、提取或转录可能耗时，保持任务运行，无需重新开始。"; }
  const ready = items.filter(x => !answersIncomplete(x) && (["document_ready", "document_needs_user", "form_opened"].includes(x.status) || x.document_fill?.documents?.length || x.form_fill?.forms?.some(f=>f.prefilled_fields > 0))).length;
  const localReady = items.filter(x => !answersIncomplete(x) && (x.status === "ready" || x.answer_document)).length;
  const incompleteDocuments = items.filter(documentAnswersIncomplete).length;
  const incompleteForms = items.filter(formAnswersIncomplete).length;
  const failed = items.filter(x => x.status === "failed").length;
  const needsInput = items.filter(x => ["needs_user", "document_needs_user", "needs_document", "needs_form"].includes(x.status)).length;
  const finished = items.filter(x => ["ready", "document_ready", "document_needs_user", "form_opened", "needs_form", "needs_document", "needs_user", "materials_ready", "failed"].includes(x.status)).length;
  const stepAt = job.approval?.requested_at || p.started_at || item?.stage_started_at || job.started_at || job.created_at;
  const ai = active && stage === "ai" ? aiActivity(item?.generation) : null;
  if (ai && job.status === "running") {
    title = ai.generation.activity_label || "Codex 会话已启动，等待模型响应";
    explanation = "活动计数来自本次模型的真实通知，不能换算成完成百分比。点击下方按钮可直接查看本次 Codex 会话。";
  }
  const activityAt = [p.activity_at, item?.generation?.activity_at, job.events?.at(-1)?.at, job.updated_at].filter(x=>Number.isFinite(Date.parse(x))).sort((a,b)=>Date.parse(b)-Date.parse(a))[0];
  const silence = ageOf(activityAt, now);
  const disconnected = !!network.error || !!(network.lastSuccess && now - network.lastSuccess > 12000);
  let warning = null;
  if (active && disconnected) warning = "暂时无法读取新状态；后台任务不一定停止。恢复连接后会自动重连，请先不要重复启动。";
  else if (active && ["approval", "authorization"].includes(stage)) warning = explanation;
  else if (active && silence !== null && silence >= (stage === "transcribe" ? 300000 : 120000)) warning = `已有 ${durationLabel(silence)} 没有新进展记录。${stage === "transcribe" ? "一段转录可能较慢，不能仅凭时间认定失败。" : "服务响应正常不代表这一步有进展，可展开处理记录核对。"}`;
  if (ai && warning && !disconnected) warning = `已有 ${durationLabel(silence)} 未收到新的模型活动通知。会话已创建，但尚未确认本次回合完成；请打开本次 Codex 会话查看，不能仅凭服务连接正常认定模型有进展。`;
  const outputAt = ai?.generation.output_stream?.output_started_at || [...(job.events || [])].reverse().find(x=>x.message === "Codex 正在输出回答，完整返回后再核验")?.at;
  const outputElapsed = ageOf(outputAt,now);
  if (ai && !disconnected && /输出回答/.test(ai.generation.activity_label || job.message || "") && outputElapsed !== null && outputElapsed >= 300000) warning = `答案已连续输出 ${durationLabel(outputElapsed)}，等待时间异常。仍未收到回合结束通知，活动更新增加也不代表答案能正常完成。本次回合继续保留，尚未核验或填入；可以打开真实会话查看。`;
  let measurement = null;
  if (active && Number.isFinite(p.current) && Number.isFinite(p.total) && p.total > 0 && p.current >= 0 && p.current <= p.total) {
    const unit = {pages:"页", segments:"段"}[p.unit] || "项", format = v => p.unit === "bytes" ? `${(v / 1048576).toFixed(1)} MB` : String(v);
    measurement = {value:p.current, max:p.total, text:p.unit === "bytes" ? `已下载 ${format(p.current)} / ${format(p.total)}` : `已完成 ${format(p.current)} / ${format(p.total)} ${unit}`};
  }
  const measurableSeconds = (current,total) => Number.isFinite(current) && Number.isFinite(total) && total > 0 && current >= 0 && current <= total;
  if (active && stage === "transcribe") {
    if (p.phase === "prepare") {
      measurement = measurableSeconds(p.phase_current_seconds,p.phase_total_seconds) ? {value:p.phase_current_seconds,max:p.phase_total_seconds,text:`本段音频已准备 ${durationLabel(p.phase_current_seconds * 1000)} / ${durationLabel(p.phase_total_seconds * 1000)}`} : null;
    } else if (measurableSeconds(p.media_current_seconds,p.media_total_seconds)) {
      measurement = {value:p.media_current_seconds,max:p.media_total_seconds,text:`本文件已转录 ${durationLabel(p.media_current_seconds * 1000)} / ${durationLabel(p.media_total_seconds * 1000)} 音频${Number.isSafeInteger(p.current) && Number.isSafeInteger(p.total) && p.total > 0 && p.current >= 0 && p.current <= p.total ? ` · ${p.current}/${p.total} 段` : ""}`};
    }
  }
  if (active && stage === "ai") measurement = null;
  if (measurement) { measurement.percent = Math.floor(measurement.value / measurement.max * 100); measurement.text += ` · ${measurement.percent}%（实测）`; }
  if (!active) {
    title = failed && stage === "validate" ? "结果核验未通过" : ready || localReady ? "答案已交付 · 请审阅" : labels[job.status] || job.status;
    explanation = failed ? `本次 ${failed} 项未完成；请查看具体原因。点击重试会自动核对原题和缓存资料后继续；已生成的答案入口仍保留。` : ready ? "请在已填入的原文档或预填原表单审阅，最后由你手动提交。" : localReady ? (items.some(x => x.answer_document) ? "本机答案文件已生成，请点击打开答案或下载 Word 审阅，最后手动提交。" : "逐题答案已核验；请继续自动填写后在原页面审阅，最后手动提交。") : needsInput ? "这条历史记录尚无可审阅的完整答案。点击重试会自动核对资料并继续作答；调整说明为可选操作。" : "请查看各项作业的结果与审阅提示。";
    if (incompleteDocuments) { title = "原文档仍有题目未完成"; explanation = `${incompleteDocuments} 项作业仍有未填写的题目或答案栏。已写入的答案已保留，点击重试会重新核对题目并补全。`; }
    if (incompleteForms) {
      const inputs = items.flatMap(x=>formUserInputGaps(x.form_fill, ["user_input_fields"])), native = items.flatMap(x=>formUserInputGaps(x.form_fill, ["unsupported_fields"])), sources = items.flatMap(x=>formSourceGaps(x.form_fill));
      title = sources.length ? "原表单的作答资料尚未读全" : "原表单仍有必填栏未完成";
      explanation = `${incompleteForms} 项表单尚未填完，已有答案已保留。`
        + (sources.length ? `${sources.length} 个必答题缺少作答资料。${formSourceDetail(sources)}点击“再次读取资料并补全”继续。` : items.some(formGenerationIncomplete) ? "点击重试会重新核对题目并补全答案。" : "")
        + (inputs.length ? `请补充 ${inputs.length} 栏必填信息后重新填入。` : "")
        + (native.length ? `另有 ${native.length} 栏需要在原表单使用原生控件完成。` : "");
    }
  }
  const index = !active && (ready || localReady) ? 3 : ["fill", "form_fill", "document_create"].includes(stage) ? 2 : ["ai", "validate", "approval"].includes(stage) ? 1 : 0;
  const nowOrEnd = job.finished_at ? Date.parse(job.finished_at) : now;
  const estimate = estimateView(job,disconnected,now,stage);
  const measurementLabel = `当前步骤 · ${measurement ? ["stale","paused"].includes(estimate.state) ? "上次实测进度" : "实测进度" : estimate.state === "stale" ? "等待新状态" : estimate.state === "paused" ? "等待继续" : "实际活动"}`;
  return {active, title, explanation, stage, index, measurement, measurementLabel, ai, estimate, ready, localReady, failed, finished, total:items.length, filename:p.filename || null, attachments:p.attachment_total ? `课程附件 ${p.attachment_index}/${p.attachment_total}` : null, elapsed:ageOf(job.started_at || job.created_at, nowOrEnd), stepElapsed:ageOf(stepAt, nowOrEnd), silence, warning, disconnected, workerAlive:job.runtime?.worker_alive === true, legacy};
}
// End pure progress presentation.

// Pure shutdown presentation: only the local backend can schedule or cancel power actions.
function shutdownView(snapshot, now = Date.now(), receivedAt = now) {
  const supported = snapshot?.supported === true, status = snapshot?.state || "off";
  const enabled = supported && snapshot?.enabled === true;
  const active = enabled || ["waiting", "armed", "countdown", "shutting_down"].includes(status);
  const seconds = Number.isFinite(snapshot?.remaining_seconds) ? Math.max(0, Math.ceil(snapshot.remaining_seconds - Math.max(0, now - receivedAt) / 1000)) : null;
  const deadline = Date.parse(snapshot?.deadline_at);
  const remaining = Number.isFinite(deadline) ? Math.max(0, Math.ceil((deadline - now) / 1000)) : seconds;
  let message = snapshot?.message || (supported ? "本次任务全部成功后倒计时 60 秒，可随时取消。" : "此设备暂不支持自动关机。");
  if (status === "countdown") message = remaining === null ? "任务已全部成功，正在读取关机倒计时，可随时取消。" : remaining > 0 ? `任务已全部成功，${remaining} 秒后自动关机。请保存其他工作，可随时取消。` : "关机倒计时已结束，正在等待系统响应；仍可尝试取消。";
  return {supported, status, enabled, active, message, remaining, canCancel:supported && active && status !== "shutting_down", urgent:status === "countdown" || status === "shutting_down"};
}
// End pure shutdown presentation.

function receiveShutdown(snapshot) {
  state.shutdown = snapshot; state.shutdownReceivedAt = Date.now(); state.shutdownError = null;
  renderShutdown();
}
function renderShutdown() {
  const view = shutdownView(state.shutdown, Date.now(), state.shutdownReceivedAt);
  $("shutdownControl").hidden = !!state.monitorOnly || !state.shutdown;
  $("shutdownControl").classList.toggle("shutdown-countdown", view.urgent);
  $("shutdownEnabled").checked = view.enabled;
  $("shutdownEnabled").disabled = !!state.monitorOnly || !view.supported || state.shutdownPending || view.status === "shutting_down";
  $("shutdownNote").textContent = state.shutdownError ? `${state.shutdownError} ${view.active ? "关机计划可能仍在后台生效；可重试取消。" : ""}` : view.message;
  $("shutdownCancel").hidden = !!state.monitorOnly || !view.canCancel;
  $("shutdownCancel").disabled = state.shutdownPending;
}
async function pollShutdown() {
  if (state.monitorOnly || !state.shutdown?.supported || shutdownPollInFlight || state.shutdownPending) return;
  shutdownPollInFlight = true; const version = shutdownRequestVersion;
  try { const snapshot = await api("/api/shutdown"); if (version === shutdownRequestVersion && !state.shutdownPending) receiveShutdown(snapshot); }
  catch (error) { if (version === shutdownRequestVersion && !state.shutdownPending) { state.shutdownError = error.status === 404 ? "当前后台尚未提供自动关机，请重新打开新版助手。" : "暂时无法读取自动关机状态。"; renderShutdown(); } }
  finally { shutdownPollInFlight = false; }
}
function startShutdownPolling() {
  if (!shutdownPolling && !state.monitorOnly && state.shutdown?.supported) shutdownPolling = setInterval(pollShutdown, 2000);
}
async function changeShutdown(enabled, cancel = false) {
  if (state.monitorOnly || state.shutdownPending || !state.shutdown?.supported) return;
  state.shutdownPending = true; state.shutdownError = null; shutdownRequestVersion += 1; renderShutdown();
  try {
    receiveShutdown(await api(cancel ? "/api/shutdown/cancel" : "/api/shutdown", "POST", cancel ? {} : {enabled}));
    toast(state.shutdown.message || (enabled ? "已开启本次任务完成后自动关机" : "已取消自动关机"));
  } catch (error) {
    try { receiveShutdown(await api("/api/shutdown")); } catch { /* Keep the last confirmed state so a failed cancel cannot look successful. */ }
    state.shutdownError = error.status === 404 ? "当前后台尚未提供自动关机，请重新打开新版助手。" : error.message;
    toast(state.shutdownError);
  } finally { state.shutdownPending = false; renderShutdown(); startShutdownPolling(); }
}

function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
function icon(name) { const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true"); const use = document.createElementNS(svg.namespaceURI, "use"); use.setAttribute("href", "#i-" + name); svg.append(use); return svg; }
function button(text, className, handler) { const el = node("button", text, className); el.type = "button"; el.addEventListener("click", handler); return el; }
function friendlyError(message) { return ({"A known AI policy requires the user's source evidence.": "请填写教师实际 AI 规定的原文与来源，才能确认允许或禁止使用。", "Limited AI use requires explicit limits.": "选择有限度允许时，请填写允许的具体用途和限制。", "Invalid attachment/page processing budget.": "附件上限需在 1 字节至 20GB 内；PDF 页数上限需在 1–5000 页内。", "Selected assignment is no longer confirmed pending; refresh the list before retrying.": "有选中作业的提交状态已变化，请刷新待办并重新勾选。", "Another local run is active; wait for it to finish.": "另一个本机处理正在运行，请等它结束后重试。", "Course policy changed since preparation; prepare the package again.": "课程规则已变化，请重新生成审核包。"})[message] || message; }
function toast(message) { clearTimeout(toastTimer); $("toast").textContent = message; $("toast").hidden = false; toastTimer = setTimeout(() => $("toast").hidden = true, 7000); }
async function api(path, method = "GET", body) {
  const options = {method, cache: "no-store", credentials: "omit", headers: {Authorization: "Bearer " + token}};
  if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  const controller = method === "GET" ? new AbortController() : null;
  if (controller) options.signal = controller.signal;
  const timer = controller ? setTimeout(() => controller.abort(), path === "/api/codex/models" ? 90000 : 10000) : null;
  let response;
  try { response = await fetch(path, options); }
  catch (error) { throw new Error(error.name === "AbortError" ? "本机状态读取超时，等待连接恢复。" : error.message); }
  finally { clearTimeout(timer); }
  if (!response.ok) { const data = await response.json().catch(() => ({})), error = new Error(friendlyError(data.error || "本机连接失败，请重新打开助手。")); error.status = response.status; throw error; }
  return response.headers.get("Content-Type").includes("application/json") ? response.json() : response.blob();
}
function dateLabel(value, time = false) { if (!value) return "无截止日期"; return new Intl.DateTimeFormat("zh-CN", {timeZone: state.timezone || "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", ...(time ? {hour: "2-digit", minute: "2-digit"} : {})}).format(new Date(value)); }
function localDay(value) { return value ? value.slice(0, 10) : ""; }
function safeLink(url, text, className) { try { const u = new URL(url); if (u.protocol !== "https:" || !(u.hostname.endsWith(".google.com") || ["google.com", "youtube.com", "www.youtube.com", "youtu.be"].includes(u.hostname))) return null; const a = node("a", text, className); a.href = u.href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; } catch { return null; } }
function pill(status) { return node("span", labels[status] || status, "pill " + ({ready: "success", needs_user: "warning", failed: "failure", interrupted: "warning", paused: "warning"}[status] || "")); }
function codexLink(generation) { const id = generation?.thread_id; if (!id || !/^[a-f0-9-]{36}$/i.test(id)) return null; const link = node("a", "在 Codex 中打开真实会话 ↗", "generation-link"); link.href = "codex://threads/" + id; return link; }
function overdue(item) { return !!item.due_utc && new Date(item.due_utc) < new Date(); }
function latestItems() { const found = new Map(); for (const job of [...state.jobs, ...(state.resultJobs || [])].sort((a,b) => b.created_at.localeCompare(a.created_at))) if (["review", "fill"].includes(job.kind)) for (const item of job.items) if (!found.has(keyOf(item))) found.set(keyOf(item), {job, item}); return found; }
// One-click selection: only visible, confirmed pending work without delivered results.
function hasAnswer(item) {
  return !!(item?.answer_document?.files?.["answer.docx"] || item?.document_fill?.documents?.length || item?.form_fill?.forms?.length);
}
function delivered(item) {
  return !answersIncomplete(item) && hasAnswer(item) && ["ready", "document_ready", "document_needs_user", "form_opened"].includes(item.status);
}
function selectTargets(visible, selected, latest) {
  return visible.filter(item => !delivered(latest.get(keyOf(item))?.item) && (!selected.size || selected.has(keyOf(item))));
}
function filtered() {
  const normalize = value => value.normalize("NFKC").replace(/\s+/g, " ").trim().toLocaleLowerCase();
  const q = normalize($("searchInput").value), course = $("courseFilter").value, date = $("dateFilter").value;
  return (state.pending?.assignments || []).filter(item => (!state.hideOverdue || !overdue(item)) && (!course || item.course_id === course) && (!q || normalize((item.title || "") + " " + (item.course_name || "")).includes(q)) && (item.due_local ? (!date || localDay(item.due_local) <= date) : $("includeNoDue").checked)).sort((a,b) => (a.due_utc || "9999").localeCompare(b.due_utc || "9999"));
}
function targets() { return selectTargets(filtered(), state.selected, latestItems()); }
function saveOptions() {
  localStorage.setItem("classroom-model", $("modelSelect").value);
  localStorage.setItem("classroom-effort", $("effortSelect").value);
  localStorage.setItem("classroom-process-media", String($("processMedia").checked));
}
function setButtons() {
  const busy = !!state.active || state.busy || !!state.monitorOnly, count = targets().length, ai = $("aiConfirmed").checked;
  $("authorizeDocuments").disabled = busy; $("authorizeDocuments").hidden = state.documentsAuthorized;
  $("documentPermissionNote").textContent = state.documentsAuthorized ? "已授权。完成后可直接打开个人文档审阅。" : "尚未授权文档写入。可先生成答案，也可在此授权自动填写。";
  $("refreshButton").disabled = busy; $("refreshButton").classList.toggle("spinner", !!state.active && state.jobs.find(x => x.id === state.active)?.kind === "refresh");
  $("generateButton").disabled = busy || !count || (ai && (!state.models || state.modelsLoading));
  $("generateButton").querySelector("span").textContent = state.active ? "正在处理…" : ai ? `一键完成${count ? ` ${count} 项` : ""}` : "仅整理资料";
  $("aiConfirmed").disabled = busy; $("processMedia").disabled = busy;
  $("aiChoiceNote").textContent = ai ? "生成答案并自动填写，最后由你审阅和提交。" : "只整理题目与资料，不生成答案。";
  for (const id of ["modelSelect", "effortSelect"]) $(id).disabled = busy || !state.models || state.modelsLoading;
  $("reloadModels").disabled = busy || state.modelsLoading;
  $("selectedCount").textContent = count;
  $("selectionLabel").textContent = state.active ? "项仍在当前列表" : state.selected.size ? "项已选择" : "项将自动完成";
  $("selectionHint").textContent = state.active ? "完成的答案会陆续进入“待审阅”" : state.selected.size ? "只处理勾选项" : "当前筛选范围 · 已有答案自动跳过";
  $("clearSelection").hidden = !state.selected.size;
  $("quitButton").disabled = busy;
  const model = state.models?.models.find(x => x.model === $("modelSelect").value);
  $("modelShortcut").textContent = !ai ? "仅整理资料 · 设置" : model ? `${model.displayName || model.model} · ${$("effortSelect").selectedOptions[0]?.textContent || "默认"}` : state.modelsLoading ? "读取模型…" : "选择模型";
}
function renderCourseFilters() {
  const courses = new Map(); for (const item of state.pending?.assignments || []) courses.set(item.course_id, item.course_name || "未命名课程");
  const current = $("courseFilter").value; $("courseFilter").replaceChildren(node("option", "全部课程")); $("courseFilter").firstChild.value = "";
  for (const [id, name] of courses) { const option = node("option", name); option.value = id; $("courseFilter").append(option); }
  if (courses.has(current)) $("courseFilter").value = current;
}
function renderDiscovery() {
  if (state.monitorOnly) return;
  const pending = state.pending, omitted = pending?.needs_confirmation || [], failed = pending?.inaccessible_courses || [];
  $("notice").hidden = !failed.length;
  if (failed.length) $("notice").textContent = `${failed.length} 门课程读取失败，当前不是完整待办列表。请查看下方说明并重试刷新。`;
  $("discoveryDetails").hidden = !omitted.length && !failed.length;
  $("discoverySummary").textContent = `${omitted.length} 项提交状态需确认 · ${failed.length} 门课程访问失败`;
  $("discoveryContent").replaceChildren();
  for (const item of omitted) { const p = node("p", `${item.course_name || ""} · ${item.title || "未命名作业"}（${item.submission_state || "状态不明"}，未自动加入待办） `); const link = safeLink(item.url, "去课堂确认"); if (link) p.append(link); $("discoveryContent").append(p); }
  for (const item of failed) $("discoveryContent").append(node("p", `课程 ${item.course_id}：${item.error}`));
}
function resultAction(job, item) {
  const doc = item.document_fill?.documents?.[0], form = item.form_fill?.forms?.[0];
  const link = safeLink(doc?.url || form?.url, doc ? "打开文档 ↗" : "打开表单 ↗", "button primary small-button");
  return link || button("打开答案", "button primary small-button", () => openReview(job.id, keyOf(item), "draft"));
}
function documentFillSummary(fill) {
  const counts = {}, documents = fill?.documents || [];
  for (const doc of documents) for (const [kind, count] of Object.entries(doc.verified_by_kind || {})) counts[kind] = (counts[kind] || 0) + count;
  const labels = {choice:"处勾选", reason:"条理由", keyword:"个关键词", paragraph:"项文字答案", identity:"栏姓名/学号"};
  const parts = Object.entries(labels).filter(([kind]) => counts[kind]).map(([kind, label]) => `${counts[kind]} ${label}`);
  if (!parts.length) {
    const total = documents.reduce((sum, doc) => sum + (doc.verified_fields || 0), 0);
    if (total) parts.push(`${total} 栏答案`);
  }
  return parts.length ? "已回读核验：" + parts.join(" · ") : "";
}
function appendDocumentReceipt(parent, fill) {
  const summary = documentFillSummary(fill);
  if (summary) parent.append(node("p", summary, "small"));
  const gaps = documentContentGaps(fill);
  if (gaps.length) parent.append(node("p", `仍有 ${gaps.length} 个题目或答案栏未完成：` + gaps.map(gap => typeof gap === "string" ? gap : gap.label || gap.id || "未识别的题目").join("；"), "small warning"));
  for (const warning of fill?.identity_warnings || []) parent.append(node("p", warning, "small warning"));
}
function appendFormReceipt(parent, fill) {
  const sources = formSourceGaps(fill);
  if (sources.length) parent.append(node("p", `${sources.length} 个必答题缺少作答资料。${formSourceDetail(sources)}`, "small warning"));
  for (const [fields, message] of [[formContentGaps(fill).filter(field => field.gap_reason !== "missing_source"), "必答题未填写"], [formUserInputGaps(fill, ["user_input_fields"]), "必填信息需要补充"], [formUserInputGaps(fill, ["unsupported_fields"]), "必填栏需要在原表单操作"]]) {
    if (fields.length) parent.append(node("p", `${fields.length} 栏${message}：` + fields.map(field => field.title || field.label || field.entry_id).filter(Boolean).join("；"), "small warning"));
  }
}
function assignmentRow(item, record, result = false) {
  const key = keyOf(item), row = node("article", undefined, "assignment-row");
  row.classList.toggle("selected", state.selected.has(key));
  const body = node("div", undefined, "assignment-main"), title = node("div", item.title || item.assignment_id, "assignment-title"), meta = node("div", item.course_name || "课程作业", "assignment-meta");
  body.append(title, meta);
  appendDocumentReceipt(body, record?.item.document_fill);
  appendFormReceipt(body, record?.item.form_fill);
  if (!result) {
    const label = node("label"), check = node("input"); check.type = "checkbox"; check.checked = state.selected.has(key); check.disabled = !!state.active || delivered(record?.item);
    check.setAttribute("aria-label", "选择作业 " + (item.title || item.assignment_id));
    check.addEventListener("change", () => { check.checked ? state.selected.add(key) : state.selected.delete(key); renderPending(); }); label.append(check, body); row.append(label);
  } else row.append(body);
  const side = node("div", undefined, "assignment-aside"), actions = node("div", undefined, "row-actions");
  if (!result) side.append(node("span", item.due_local ? `${overdue(item) ? "已逾期 · " : ""}${dateLabel(item.due_local, true)}` : "无截止日期", "due-label" + (overdue(item) ? " overdue" : "")));
  if (result) {
    side.append(node("span", delivered(record.item) ? "答案可审阅 · 未自动提交" : "已有答案 · 填入尚未完成", "result-status"));
    actions.append(resultAction(record.job, record.item));
    const savedForm = safeLink(record.item.form_fill?.forms?.[0]?.original_url, "继续已改草稿 ↗", "text-button small");
    if (savedForm) actions.append(savedForm);
    if (!record.item.document_fill?.documents?.length && !record.item.form_fill?.forms?.length) actions.append(button("Word", "button secondary small-button", () => downloadAnswer(record.job.id, key)));
  } else if (record) {
    if (delivered(record.item)) {
      side.append(node("span", "答案已生成 · Classroom 尚未提交", "result-status"));
      actions.append(resultAction(record.job, record.item));
    } else if (answersIncomplete(record.item) || ["failed", "paused", "interrupted", "needs_user", "needs_document", "needs_form"].includes(record.item.status)) {
      side.append(node("span", answersIncomplete(record.item) ? "仍有必填栏未完成 · 请继续处理" : labels[record.item.status] || "上次未完成", "small warning"));
      if (record.item.error) body.append(node("p", friendlyError(record.item.error), "small warning"));
      if (hasAnswer(record.item)) actions.append(resultAction(record.job, record.item));
    }
    else if (activeStates.has(record.job.status)) side.append(pill(record.item.status));
  }
  if (record?.item.package) actions.append(button("详情", "text-button small", () => openReview(record.job.id, key)));
  else if (record?.item.error) actions.append(button("查看原因", "text-button small", () => showJob(record.job.id)));
  else { const link = safeLink(item.url, "查看课堂 ↗", "text-button small"); if (link) actions.append(link); }
  const input = record && inputAction(record.item);
  if (input?.count && !state.monitorOnly) actions.append(button(input.label, "button primary small-button", () => openSupplement(record.job.id, key)));
  side.append(actions); row.append(side); return row;
}
function renderResults() {
  const records = [...latestItems().values()].filter(x => hasAnswer(x.item));
  $("readyCount").textContent = records.length;
  if (state.view !== "results") return;
  const list = $("resultsList"); list.replaceChildren();
  if (!records.length) { const empty = node("div", undefined, "empty-state"); empty.append(node("h3", "答案完成后会出现在这里"), node("p", "从待完成列表一键开始，先完成的答案会先显示。")); list.append(empty); }
  for (const record of records) {
    const pending = state.pending?.assignments.find(x => keyOf(x) === keyOf(record.item));
    list.append(assignmentRow({...pending, ...record.item}, record, true));
  }
}
function renderPending() {
  const pending = state.pending, items = filtered(), latest = latestItems(), eligible = items.filter(x => !delivered(latest.get(keyOf(x))?.item)), visible = new Set(eligible.map(keyOf));
  // A filter change must never process a checked assignment hidden from the user.
  state.selected = new Set([...state.selected].filter(key => visible.has(key)));
  $("navCount").textContent = pending?.assignments.length || 0;
  $("visibleCount").textContent = items.length;
  $("refreshTime").textContent = pending ? `同步于 ${dateLabel(pending.refreshed_at, true)}` : "首次使用，点击刷新读取真实作业";
  $("listHint").textContent = `已同步 ${pending?.assignments.length || 0} 项未提交作业 · 当前显示 ${items.length} 项 · ${items.length - eligible.length} 项已有答案，一键完成会跳过`;
  $("clearFilters").hidden = !($("courseFilter").value || $("searchInput").value || $("dateFilter").value || !$("includeNoDue").checked || state.hideOverdue);
  $("hideOverdue").setAttribute("aria-pressed", String(state.hideOverdue)); $("hideOverdue").textContent = state.hideOverdue ? "已隐藏逾期" : "隐藏已逾期";
  const checked = eligible.filter(x => state.selected.has(keyOf(x))).length;
  $("selectAll").checked = !!eligible.length && checked === eligible.length; $("selectAll").indeterminate = checked > 0 && checked < eligible.length; $("selectAll").disabled = !eligible.length || !!state.active;
  const list = $("assignmentList"); list.replaceChildren();
  if (!items.length) {
    const empty = node("div", undefined, "empty-state");
    empty.append(node("h3", !pending ? "先同步你的作业" : "当前筛选范围没有作业"), node("p", !pending ? "点击右上方“刷新作业”，读取学校账户的真实待办。" : "可以重置筛选查看所有已同步作业，或刷新课堂状态。")); list.append(empty);
  }
  for (const item of items) list.append(assignmentRow(item, latest.get(keyOf(item))));
  renderResults(); renderDiscovery(); setButtons();
}
function updateJob(job) { const at = state.jobs.findIndex(x => x.id === job.id); if (at < 0) state.jobs.unshift(job); else state.jobs[at] = job; }
function estimatePanel(estimate) {
  const panel = node("section", undefined, "job-estimate"); panel.dataset.estimateState = estimate.state;
  const heading = node("div", undefined, "job-estimate-heading");
  heading.append(node("span", estimate.progressTitle), node("strong", estimate.percentText)); panel.append(heading);
  const bar = node(estimate.percent === null ? "div" : "progress", undefined, "job-estimated-progress");
  if (estimate.percent === null) { bar.classList.add("unknown"); bar.setAttribute("aria-hidden", "true"); }
  else { bar.max = 100; bar.value = estimate.percent; bar.setAttribute("aria-label", `总进度估计 ${estimate.percentText}，会随实际速度调整`); }
  panel.append(bar, node("p", estimate.headline, "job-eta-main"));
  if (estimate.range || estimate.finish) panel.append(node("p", [estimate.range,estimate.finish].filter(Boolean).join(" · "), "job-eta-detail"));
  if (estimate.stageText) panel.append(node("p", [estimate.stageText,estimate.stageRange].filter(Boolean).join(" · "), "job-eta-stage"));
  if (estimate.phaseText) panel.append(node("p", estimate.phaseText, "job-eta-stage"));
  if (estimate.speed || estimate.basis) panel.append(node("p", [estimate.speed,estimate.basis].filter(Boolean).join(" · "), "job-speed"));
  if (estimate.note) panel.append(node("p", estimate.note, "job-estimate-note"));
  return panel;
}
function jobProgress(job) {
  const v = progressView(job, connection), box = node("div", undefined, "job-live-detail");
  box.dataset.jobId = job.id; box.dataset.progressState = v.estimate.state;
  if (["review", "fill"].includes(job.kind) && job.ai_confirmed) {
    const steps = node("ol", undefined, "job-steps");
    const localOnly = !v.active && v.localReady && !v.ready;
    for (const [i, title] of ["准备资料", "Codex 作答", localOnly ? "生成答案文件" : "自动填写", localOnly ? "打开答案审阅" : "原页面审阅"].entries()) { const step = node("li", undefined, i === v.index ? "current" : i < v.index ? "past" : ""); step.append(node("span", i < v.index ? "✓" : String(i + 1)), node("b", title)); steps.append(step); }
    box.append(steps);
  }
  box.append(node("strong", v.title), node("p", v.explanation));
  box.append(estimatePanel(v.estimate));
  if (v.active) {
    box.append(node("p", v.measurementLabel, "job-stage-label"));
    const progress = node(v.measurement ? "progress" : "div", undefined, "job-progress");
    progress.setAttribute("aria-label", v.measurement?.text || "活动指示，不代表完成百分比");
    if (v.measurement) { progress.max = v.measurement.max; progress.value = v.measurement.value; }
    else { progress.classList.add("indeterminate"); const sweep = node("span", undefined, "job-progress-sweep"); sweep.style.animationDelay = `-${Date.now() % 1800}ms`; progress.append(sweep); progress.setAttribute("aria-hidden", "true"); }
    box.append(progress, node("p", v.measurement?.text || (v.ai ? v.ai.text : "正在处理 · 当前步骤没有可测量的总百分比"), "job-measurement"));
  }
  if (v.ai) {
    const g = v.ai.generation, session = node("div", undefined, "job-session");
    session.append(node("span", `${g.thread_title || "本次 Codex 会话"} · ${g.model || "等待核验模型"} · ${g.reasoning_effort || "默认强度"}`, "job-session-meta"));
    const link = codexLink(g); if (link) { link.textContent = "打开本次 Codex 会话 ↗"; link.className = "button secondary small-button"; session.append(link); }
    box.append(session);
  }
  if (v.filename) box.append(node("p", `${v.attachments ? v.attachments + " · " : ""}${v.filename}`, "job-file"));
  if (job.items.length) box.append(node("p", `已处理 ${v.finished}/${v.total} 项 · ${v.ready} 项已填入可审阅${v.localReady ? ` · ${v.localReady} 项本机答案可审阅` : ""}${v.failed ? ` · ${v.failed} 项失败` : ""}`, "small"));
  if (v.active && !v.legacy) box.append(node("p", friendlyError(job.message), "job-current-message"));
  const times = node("div", undefined, "job-times"); times.append(node("span", `总耗时 ${durationLabel(v.elapsed || 0)}`));
  if (v.active) times.append(node("span", `本步骤 ${durationLabel(v.stepElapsed || 0)}`), node("span", v.silence === null ? "尚无处理记录" : `最近进展 ${durationLabel(v.silence)}前`), node("span", v.disconnected ? "状态连接中断 · 自动重连中" : `服务连接正常${v.workerAlive ? " · 工作线程仍在运行" : ""}`, "connection-status" + (v.disconnected ? " warning" : "")));
  box.append(times);
  const warning = node("p", v.warning || "", "job-warning"); warning.hidden = !v.warning; warning.setAttribute("role", "status"); box.append(warning);
  return box;
}
function jobCard(job, full = false) {
  const card = node("section", undefined, full ? "history-card" : ""); card.dataset.jobCard = job.id; const heading = node("div", undefined, "job-heading"), intro = node("div");
  const later = completedSuccessor(job,state.jobs);
  intro.append(node("h3", job.kind === "refresh" ? "同步真实待办" : job.kind === "document_auth" ? "Google 文档授权" : `所选 ${job.items.length} 项作业 · ${later ? "后续已完成（历史记录）" : labels[job.status] || job.status}`));
  if (full) intro.append(node("span", dateLabel(job.created_at, true), "muted small")); heading.append(intro);
  if (activeStates.has(job.status) && !state.monitorOnly) { const pause = button(job.status === "stopping" ? "正在暂停…" : "暂停", "button secondary small-button", () => jobAction(job.id, "pause")); pause.disabled = job.status === "stopping"; heading.append(pause); }
  else if (!state.monitorOnly && later) heading.append(button("查看最新结果", "button secondary", ()=>showJob(later.job.id)));
  else if (!state.monitorOnly && (job.kind === "review" || job.status !== "completed")) heading.append(button(retryActionLabel(job), "button secondary small-button", () => jobAction(job.id, "retry")));
  card.append(heading);
  if (later) { const note = node("div", undefined, "job-live-detail"); note.append(node("strong", "旧记录中的缺口已在后续处理补齐"),node("p", `最新结果：${dateLabel(later.job.created_at,true)}。这条记录保留当时的处理状态，无需再次填写。`)); card.append(note); } else card.append(jobProgress(job));
  const items = node("div", undefined, "job-items");
  for (const item of job.items) { const row = node("div", undefined, "job-item"); const title = node("span", item.title || item.assignment_id); if (item.error) title.append(node("p", friendlyError(item.error))); row.append(title, later ? node("span", "历史状态：" + (labels[item.status] || item.status), "pill") : pill(item.status)); if (item.package) { const actual = !!item.answer_document || (item.status === "ready" && item.generation?.status === "completed"); row.append(button(actual ? "打开答案" : "查看资料与分析", actual ? "button primary small-button" : "text-button", () => openReview(job.id, keyOf(item), actual ? "draft" : "assignment"))); if (actual && !state.monitorOnly) row.append(button("下载 Word", "button secondary small-button", () => downloadAnswer(job.id, keyOf(item)))); if (item.answer_document?.pending_requirements?.length) row.append(node("p", item.answer_document.personal_templates ? "答案已交付，请在审阅时核对个人部分；调整答案或说明为可选操作。" : "答案已交付，另有审阅提示；请打开答案核对。", "small warning")); } const g = item.generation; if (g?.thread_id) { const meta = node("div", undefined, "job-model"); meta.append(node("span", `实际模型 ${g.model} · ${g.reasoning_effort || "默认强度"} · ${g.status === "completed" ? "AI 回合完成" : g.status === "running" ? "正在生成" : "尚未成功"} `)); const link = codexLink(g); if (link) meta.append(link); row.append(meta); } else if (item.package) row.append(node("span", "资料已准备 · 没有已核验的 AI 生成记录", "job-model")); for (const f of (later?.item.form_fill || item.form_fill)?.forms || []) { const link = safeLink(f.url, "在原表单审阅答案 ↗", "button primary small-button"); if (link) row.prepend(link); const saved = safeLink(f.original_url, "继续已保存的草稿 ↗", "text-button"); if (saved) row.append(saved); } for (const d of (later?.item.document_fill || item.document_fill)?.documents || []) { const link = safeLink(d.url, "在原文档审阅 ↗", "button primary small-button"); if (link) row.prepend(link); } if (item.package && g?.status === "completed" && ["needs_document", "needs_form"].includes(item.status)) row.append(button("填入已有初稿", "text-button", () => action(`/api/jobs/${job.id}/items/${keyOf(item)}/fill`, {}))); if (item.browser_read?.thread_id) { const readLink = codexLink(item.browser_read); if (readLink) { readLink.textContent = "查看登录题目读取会话 ↗"; row.append(readLink); } } const input = inputAction(item); if (!state.monitorOnly && !later) row.append(button(input?.label || "调整答案或说明（可选）", input ? "button primary small-button" : "text-button", () => openSupplement(job.id, keyOf(item)))); items.append(row); } card.append(items);
  if (job.approval && !state.monitorOnly) { const box = node("section", undefined, "approval-card"); box.append(node("h3", "Codex 请求本次操作审批"), node("p", job.approval.reason), node("pre", job.approval.command || job.approval.cwd), node("p", "此请求来自 Codex 的现有权限规则；批准仅适用于本次，不会扩大长期权限。")); const actions = node("div", undefined, "approval-actions"); for (const [choice, label] of [["accept", "批准本次"], ["decline", "拒绝"], ["cancel", "取消操作"]]) actions.append(button(label, "button secondary small-button", async () => { try { await api(`/api/jobs/${job.id}/approval`, "POST", {id: job.approval.id, decision: choice}); await pollJob(); } catch(error) { toast(error.message); } })); box.append(actions); card.append(box); }
  if (job.cache) card.append(node("p", `资料缓存：复用 ${job.cache.reused} 个阶段 · 新增 ${job.cache.processed} · 缓存阶段失败 ${job.cache.failed}`, "small"));
  if (job.events.length) { const details = node("details"), summary = node("summary", "查看处理记录"), events = node("div", undefined, "event-list"); for (const entry of job.events) { const line = node("div", undefined, "event-line"); line.append(node("time", new Date(entry.at).toLocaleTimeString("zh-CN", {timeZone: state.timezone || "Asia/Tokyo", hour: "2-digit", minute: "2-digit", second: "2-digit"})), node("span", entry.message)); events.append(line); } details.dataset.jobLog = job.id; details.append(summary, events); card.append(details); }
  return card;
}
function renderJobs() {
  const expanded = new Set([...document.querySelectorAll("details[data-job-log][open]")].map(x => x.dataset.jobLog));
  const focus = state.jobs.find(x => x.id === state.active);
  $("jobPanel").hidden = !focus; $("jobPanel").replaceChildren();
  if (focus) $("jobPanel").append(jobCard(focus));
  if (state.view === "history") {
    const opened = new Set([...document.querySelectorAll("details[data-history-job][open]")].map(x => x.dataset.historyJob));
    $("historyList").replaceChildren();
    if (!state.jobs.length) $("historyList").append(node("div", "还没有处理记录。", "empty-state muted"));
    for (const job of [...state.jobs].sort((a,b) => b.created_at.localeCompare(a.created_at))) {
      const details = node("details", undefined, "history-entry"); details.dataset.historyJob = job.id;
      const summary = node("summary"), title = job.kind === "review" || job.kind === "fill" ? job.items.map(x => x.title || x.assignment_id).join("、") : job.kind === "refresh" ? "刷新作业" : "文档授权";
      summary.append(node("span", title, "history-title"), node("span", labels[job.status] || job.status, "small"), node("time", dateLabel(job.created_at,true), "muted small")); details.append(summary);
      const expand = () => { if (details.open && !details.querySelector("section")) details.append(jobCard(job, true)); };
      details.addEventListener("toggle", expand); details.open = opened.has(job.id) || job.id === state.focus; expand(); $("historyList").append(details);
    }
  }
  setButtons(); for (const details of document.querySelectorAll("details[data-job-log]")) details.open = expanded.has(details.dataset.jobLog);
}
async function showJob(id) {
  try {
    if (!state.jobs.some(x => x.id === id)) updateJob(await api("/api/jobs/" + id));
    state.focus = id; setView("history");
    document.querySelector(`details[data-history-job="${id}"]`)?.scrollIntoView({block:"center",behavior:"smooth"});
  } catch (error) { toast(error.message); }
}
function updateProgressClock() {
  for (const box of document.querySelectorAll(".job-live-detail")) {
    const job = state.jobs.find(x => x.id === box.dataset.jobId); if (!job || !activeStates.has(job.status)) continue;
    const v = progressView(job, connection), spans = box.querySelectorAll(".job-times span");
    box.dataset.progressState = v.estimate.state;
    const estimate = box.querySelector(".job-estimate");
    if (estimate && estimate.dataset.estimateState !== v.estimate.state) estimate.replaceWith(estimatePanel(v.estimate));
    const stageLabel = box.querySelector(".job-stage-label"); if (stageLabel) stageLabel.textContent = v.measurementLabel;
    if (spans.length === 4) { spans[0].textContent = `总耗时 ${durationLabel(v.elapsed || 0)}`; spans[1].textContent = `本步骤 ${durationLabel(v.stepElapsed || 0)}`; spans[2].textContent = v.silence === null ? "尚无处理记录" : `最近进展 ${durationLabel(v.silence)}前`; spans[3].textContent = v.disconnected ? "状态连接中断 · 自动重连中" : `服务连接正常${v.workerAlive ? " · 工作线程仍在运行" : ""}`; spans[3].classList.toggle("warning", v.disconnected); }
    const warning = box.querySelector(".job-warning"); warning.textContent = v.warning || ""; warning.hidden = !v.warning;
  }
}
setInterval(updateProgressClock, 1000);
setInterval(renderShutdown, 1000);
async function loadState() {
  const shutdownVersion = shutdownRequestVersion;
  const data = await api("/api/bootstrap"); connection.lastSuccess = Date.now(); connection.error = null; state.monitorOnly = !!data.monitor_only; state.documentsAuthorized = !!data.documents_authorized; state.pending = data.pending; state.jobs = data.jobs; state.resultJobs = data.result_jobs || []; state.policies = data.policies; state.active = data.active_job_id; state.timezone = data.timezone; $("accountEmail").textContent = data.account;
  if (shutdownVersion === shutdownRequestVersion && !state.shutdownPending) receiveShutdown(data.shutdown || {enabled:false, state:"off", supported:false, message:"当前后台尚未提供自动关机，请重新打开新版助手。"}); startShutdownPolling();
  const running = state.jobs.find(x => x.id === state.active); if (running && ["review", "fill"].includes(running.kind)) $("aiConfirmed").checked = !!running.ai_confirmed;
  if (state.monitorOnly) { document.body.classList.add("monitor-mode"); $("notice").hidden = false; $("notice").replaceChildren(node("span", "这是只读进度窗口，不会影响原任务。审批和作业操作请返回原作业助手。 ")); const back = node("a", "返回作业助手 ↗"); back.href = data.source_url; back.target = "_blank"; back.rel = "noopener noreferrer"; $("notice").append(back); }
  if (state.pending) { const available = new Set(state.pending.assignments.map(keyOf)); state.selected = new Set([...state.selected].filter(x => available.has(x))); }

  renderCourseFilters(); renderPending(); renderJobs(); if (state.active) startPolling();
}
function startPolling() { if (polling) return; polling = setInterval(pollJob, 1400); }
async function pollJob() {
  if (!state.active) { clearInterval(polling); polling = null; return; }
  if (pollInFlight) return;
  pollInFlight = true;
  try { const job = await api("/api/jobs/" + state.active); connection.lastSuccess = Date.now(); connection.error = null; updateJob(job); renderJobs(); renderPending(); if (!activeStates.has(job.status)) { state.active = null; state.focus = job.id; await loadState(); renderPending(); if (job.kind === "refresh") toast(job.status === "failed" ? job.message : `已刷新 ${state.pending?.assignments.length || 0} 项真实待办`); else toast(job.message); } } catch (error) { connection.error = error.message; renderJobs(); } finally { pollInFlight = false; }
}
async function action(path, body) {
  if (state.monitorOnly) { toast("这是只读进度窗口，请返回原作业助手操作。"); return; }
  if (state.busy) return; state.busy = true; setButtons();
  try { const job = await api(path, "POST", body); connection.lastSuccess = Date.now(); connection.error = null; updateJob(job); state.active = activeStates.has(job.status) ? job.id : null; state.focus = job.id; renderJobs(); renderPending(); startPolling(); return job; } catch (error) { toast(error.message); } finally { state.busy = false; setButtons(); }
}
function pendingRefreshDue(pending, now = Date.now()) {
  const age = ageOf(pending?.refreshed_at, now);
  return age === null || age > 15 * 60 * 1000;
}
let syncChecking = false, lastSyncCheck = 0, lastRefreshAttempt = 0;
async function syncWhenIdle() {
  if (!token || state.monitorOnly || state.active || state.busy || syncChecking || document.hidden || document.querySelector("dialog[open]") || Date.now() - lastSyncCheck < 30000) return;
  syncChecking = true; lastSyncCheck = Date.now();
  try {
    await loadState(); // Also pick up work/refreshes started in another window.
    if (!state.active && !state.busy && pendingRefreshDue(state.pending) && Date.now() - lastRefreshAttempt > 60000) {
      lastRefreshAttempt = Date.now();
      await action("/api/refresh", {});
    }
  } catch (error) { connection.error = error.message; }
  finally { syncChecking = false; }
}
setInterval(syncWhenIdle, 60000);
window.addEventListener("focus", syncWhenIdle);
document.addEventListener("visibilitychange", () => { if (!document.hidden) syncWhenIdle(); });
async function jobAction(id, verb) { await action(`/api/jobs/${id}/${verb}`, {}); }
function renderEfforts(preferred) { const model = state.models.models.find(x => x.model === $("modelSelect").value); $("effortSelect").replaceChildren(); const labels = {none: "无", minimal: "最少", low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高", ultra: "极高"}; for (const value of model?.supportedReasoningEfforts || []) { const option = node("option", labels[value.reasoningEffort] || value.reasoningEffort); option.value = value.reasoningEffort; $("effortSelect").append(option); } if (!$("effortSelect").children.length) { const option = node("option", "沿用模型默认"); option.value = ""; $("effortSelect").append(option); } const selected = preferred || model?.defaultReasoningEffort; if ([...$("effortSelect").options].some(x => x.value === selected)) $("effortSelect").value = selected; }
async function loadModels() { state.modelsLoading = true; setButtons(); $("modelStatus").textContent = "正在从本机 Codex 读取模型与当前配置…"; try { const catalog = await api("/api/codex/models"); state.models = catalog; $("modelSelect").replaceChildren(); for (const model of catalog.models) { const option = node("option", model.displayName || model.model); option.value = model.model; $("modelSelect").append(option); } const saved = localStorage.getItem("classroom-model"); const selected = catalog.models.some(x => x.model === saved) ? saved : catalog.default_model; if (catalog.models.some(x => x.model === selected)) $("modelSelect").value = selected; renderEfforts(saved === selected ? localStorage.getItem("classroom-effort") : selected === catalog.default_model ? catalog.default_effort : null); $("modelStatus").textContent = "来自本机 Codex 目录与配置；实际模型、会话和生成记录会保存在每份结果中。"; } catch (error) { state.models = null; $("modelStatus").textContent = error.message; toast(error.message); } finally { state.modelsLoading = false; setButtons(); } }
function setView(view) {
  state.view = view;
  for (const name of ["pending", "results", "history"]) $(name + "View").hidden = view !== name;
  for (const [id, name] of [["navPending","pending"],["navResults","results"],["navHistory","history"]]) $(id).classList.toggle("active", view === name);
  document.querySelector(".selection-bar").hidden = view !== "pending";
  renderResults(); renderJobs();
}
function clearImages() { for (const url of state.images) URL.revokeObjectURL(url); state.images = []; }
async function openReview(jobId, key, initialTab) {
  try {
    const data = await api(`/api/jobs/${jobId}/items/${key}`); state.preview = {jobId, key, data};
    $("reviewTitle").textContent = data.manifest.assignment.title || "资料与结果";
    $("reviewCourse").textContent = data.manifest.course_name || "";
    const g = data.generation;
    $("reviewStatus").textContent = data.policy_changed ? "补充或设置已变化，请继续处理" : data.text["draft.md"] && g?.status === "completed" ? `实际初稿已核验 · ${g.model} · ${g.reasoning_effort || "默认强度"} · 尚未提交` : g?.status === "completed" ? `Codex 资料分析已完成 · ${g.model} · 尚无答案初稿` : "资料与待确认包 · 没有已核验的 AI 生成结果";
    $("downloadButton").textContent = "下载辅助证据（可选）";
    const filled = data.document_fill?.documents?.[0];
    const attached = data.requirements.student_submission?.assignmentSubmission?.attachments?.find(x => x.driveFile)?.driveFile;
    const responseForm = data.requirements.response_forms?.[0];
    const prefilled = data.form_fill?.forms?.[0];
    const original = safeLink(filled?.url || prefilled?.url || attached?.alternateLink || responseForm?.url, filled ? "打开已填入的文档 ↗" : prefilled ? "打开预填答案表单 ↗" : attached ? "打开原作业文档 ↗" : "打开原作业表单 ↗", "button primary");
    $("originalDocumentAction").replaceChildren(); if (original) $("originalDocumentAction").append(original);
    $("fillDocumentButton").hidden = !data.text["draft.md"] || !!filled || (!responseForm && !data.generation?.document_answers_sha256) || (!!responseForm && !data.generation?.form_answers_sha256);
    if (filled) {
      $("reviewStatus").textContent = `${data.document_fill.identity_warnings?.length ? "答案已填入 · 姓名/学号需核对" : "已自动填入并回读核验"} · ${g?.model || "原模型"} · 尚未提交`;
      appendDocumentReceipt($("originalDocumentAction"), data.document_fill);
    }
    else if (prefilled) $("reviewStatus").textContent += ` · 原表单已准备 ${prefilled.prefilled_fields} 栏预填答案，请在原表单审阅`;
    if (prefilled) appendFormReceipt($("originalDocumentAction"), data.form_fill);
    if (prefilled) { const note = node("p", "预填入口会重新带入候选答案；已在 Google 中修改过的内容请从下方原始链接继续审阅。浏览器草稿保存状态以 Google 页面为准。", "small"); $("originalDocumentAction").append(note); const saved = safeLink(prefilled.original_url, "继续 Google 中保存的草稿 ↗"); if (saved) $("originalDocumentAction").append(saved); for (const question of prefilled.needs_user || []) $("originalDocumentAction").append(node("p", question, "small warning")); }
    $("fillDocumentButton").textContent = responseForm ? "自动填入原表单" : "填入已有初稿";
    $("downloadAnswerButton").hidden = !data.text["draft.md"] || state.monitorOnly;
    if (!filled && data.answer_document?.pending_requirements?.length) $("reviewStatus").textContent = `答案已生成 · ${g?.model || "原模型"} · ${data.answer_document.pending_requirements.length} 项审阅提示 · 未提交`;
    $("reviewDialog").querySelector(".details-options").open = false; renderTab(initialTab || (data.text["draft.md"] ? "draft" : "assignment")); $("reviewDialog").showModal();
  } catch (error) { toast(error.message); }
}
function renderTab(tab) {
  clearImages(); for (const b of document.querySelectorAll(".review-tabs button")) { b.classList.toggle("active", b.dataset.tab === tab); b.setAttribute("aria-selected", String(b.dataset.tab === tab)); }
  const content = $("reviewContent"); content.replaceChildren(); const {data, jobId, key} = state.preview;
  if (tab === "assignment") { const assignment = data.manifest.assignment; content.append(node("h3", assignment.title), node("pre", assignment.description || "此作业没有文字说明，请检查附件与课堂链接。")); const link = safeLink(assignment.alternateLink, "打开真实 Classroom 作业 ↗"); if (link) content.append(link); for (const material of assignment.materials || []) { const ref = material.driveFile?.driveFile || material.link || material.youtubeVideo || material.form; if (!ref) continue; const materialLink = safeLink(ref.alternateLink || ref.url || ref.formUrl, ref.title || "查看课程附件"); if (materialLink) { const line = node("p"); line.append(materialLink); content.append(line); } } const original = node("details"); original.append(node("summary", "查看要求的原始来源片段")); for (const source of data.requirements.sources) { const box = node("div", undefined, "source-card"); box.append(node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); original.append(box); } content.append(original); }
  if (tab === "draft") { if (data.answer_document?.blocks?.length) { for (const block of data.answer_document.blocks) { if (block.kind === "table") { const table = node("table", undefined, "answer-table"); for (const values of block.rows) { const row = node("tr"); for (const value of values) row.append(node("td", value)); table.append(row); } content.append(table); } else content.append(node(block.kind === "heading" ? `h${Math.min(block.level, 6)}` : "p", block.text, "answer-paragraph")); } } else content.append(node("pre", data.text["draft.md"] || "尚未生成答案初稿。需要使用 AI 时，在主界面勾选确认，再开始任务。资料包不能代表作业已经完成。")); }
  if (tab === "assignment") for (const form of data.requirements.response_forms || []) {
    const box = node("section", undefined, "source-card"); box.append(node("h3", form.title), node("p", `已读取 ${form.page_count} 页、${form.question_count} 个题目或身份栏 · ${form.read_complete ? "全部题目结构已读取" : "部分题型需要目视核对"}`));
    const link = safeLink(form.url, "打开原作业表单 ↗"); if (link) box.append(link);
    for (const source of data.requirements.sources.filter(x => x.source_id === form.source_id).sort((a,b) => { try { const x = JSON.parse(a.text), y = JSON.parse(b.text); return (x.page || 0) - (y.page || 0) || (x.title || "").normalize("NFKC").localeCompare((y.title || "").normalize("NFKC"), "ja", {numeric:true}); } catch { return 0; } })) {
      try {
        const question = JSON.parse(source.text);
        if (!question.item_id) { if (question.description) box.append(node("p", question.description)); continue; }
        box.append(node("div", `第 ${question.page} 页 · ${{short_text:"简答",paragraph:"文字回答",single_choice:"单选",dropdown:"下拉选择",checkbox:"多选",scale:"量表",grid:"网格",date:"日期",time:"时间",file_upload:"文件上传"}[question.type] || "题目"}`, "locator"), node("h4", question.title));
        if (question.description) box.append(node("p", question.description));
        const choices = question.fields.flatMap(field => field.choices || []);
        if (choices.length) box.append(node("pre", choices.map(value => "• " + value).join("\n")));
      } catch { box.append(node("pre", source.text)); }
    }
    content.prepend(box);
  }
  if (tab === "assignment" && data.manifest.student_profile) {
    const profile = data.manifest.student_profile, fields = [["学籍番号", "student_id"], ["氏名", "name"], ["学科", "department"], ["クラス", "class_name"]];
    const box = node("section", undefined, "source-card"); box.append(node("h3", "本次作答使用的身份"));
    for (const [label, field] of fields) if (profile[field]) box.append(node("p", `${label}：${profile[field]}`));
    content.prepend(box);
  }
  if (tab === "assignment") { const submission = data.requirements.student_submission; if (submission?.assignmentSubmission?.attachments?.some(x => x.driveFile)) { const box = node("section", undefined, "source-card"); box.append(node("h3", "你的个人作业文档与具体题目")); for (const attachment of submission.assignmentSubmission?.attachments || []) { const file = attachment.driveFile; if (!file) continue; const link = safeLink(file.alternateLink, "打开个人作业文档 ↗"); if (link) box.append(link); for (const source of data.requirements.sources.filter(x => x.source_id === "file:" + file.id)) box.append(node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); } content.prepend(box); } const index = data.manifest.source_index || []; const materials = node("section", undefined, "source-card"); materials.append(node("h3", "作业附件与同课程授课资料")); for (const source of index.filter(x => x.source_id.startsWith("file:"))) { const sent = source.evidence_ids.some(id => data.generation?.input?.source_ids.includes(id)); const line = node("p", `${source.title} · ${source.locators.length} 个片段 · ${sent ? "已送入本次 AI" : "已读取，保存在本机证据"} `); const link = safeLink(source.url, "原文件 ↗"); if (link) line.append(link); for (const parent of source.parents || []) { const parentLink = safeLink(parent.url, "课堂来源 ↗"); if (parentLink) line.append(node("span", " · "), parentLink); } materials.append(line); } if (index.length) content.append(materials); }
  if (tab === "questions") { content.append(button("调整答案或说明（可选）", "button secondary", () => openSupplement(jobId, key))); const questions = data.review?.questions; if (questions?.length) for (const q of questions) content.append(node("p", "• " + q)); else content.append(node("pre", data.text["questions.md"] || "暂无审核问题，请先完成审核。")); }
  if (tab === "checklist") { if (data.review?.requirement_checks) for (const c of data.review.requirement_checks) { const box = node("div", undefined, "source-card"); box.append(node("div", ({met: "已满足", partial: "部分满足", unmet: "未满足", needs_user: "待你确认"}[c.status] || c.status) + " · E:" + c.requirement_source_id, "locator"), node("pre", c.requirement), node("p", c.draft_location || "详见审阅提示")); content.append(box); } else content.append(node("pre", data.text["checklist.md"] || "审核尚未完成。")); }
  if (tab === "warnings") { if (!data.manifest.warnings.length) content.append(node("p", "本次没有记录资料缺口。引用和内容仍需你审阅。")); for (const warning of data.manifest.warnings) { const box = node("div", undefined, "source-card"); box.append(node("div", warning.source, "locator"), node("pre", warning.error)); content.append(box); } }
  if (tab === "generation") { const g = data.generation; if (!g || g.status === "not_invoked") { content.append(node("h3", "没有调用 AI"), node("p", "这份包只包含已读取的资料和待确认项，不能当作已完成的作业或 AI 初稿。")); } else { content.append(node("h3", g.status === "completed" ? "真实 Codex 回合已完成" : "Codex 回合尚未成功"), node("p", `实际模型：${g.model || "尚未确认"}\n请求模型：${g.requested_model}\n推理强度：${g.reasoning_effort || "默认"}\n会话 ID：${g.thread_id || "尚未创建"}\n回合 ID：${g.turn_id || "尚未开始"}\n开始：${g.started_at ? dateLabel(g.started_at, true) : "—"}\n结束：${g.finished_at ? dateLabel(g.finished_at, true) : "—"}\n模式：${g.mode === "draft" ? "初稿生成" : "资料与题目分析"}`)); const link = codexLink(g); if (link) content.append(link); if (g.error) content.append(node("p", g.error)); content.append(node("h3", "实际送入模型的来源")); const ids = new Set(g.input?.source_ids || []); for (const source of data.manifest.source_index || []) if (source.evidence_ids.some(id => ids.has(id))) content.append(node("p", `${source.title} · ${source.locators.join("、")}`)); content.append(node("p", `另有 ${g.input?.images?.length || 0} 张实际页图送入模型。${g.input?.omitted_source_count ? "部分较长文本仅保存在可检索的本机证据中。" : ""}`), node("h3", "Codex 生成说明"), node("pre", data.text["codex-summary.md"] || "尚未返回完整说明。")); if (g.token_usage) { const details = node("details"); details.append(node("summary", "查看 Codex 返回的用量记录"), node("pre", JSON.stringify(g.token_usage, null, 2))); content.append(details); } } }
  if (tab === "evidence") for (const source of data.evidence.sources) { const box = node("section", undefined, "source-card"), head = node("header"); head.append(node("span", source.title || source.source_kind)); const link = safeLink(source.url, "查看原来源 ↗"); if (link) head.append(link); box.append(head, node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); if (source.image_path) { const show = button("查看保留的页面图像", "text-button small", async () => { show.disabled = true; try { const blob = await api(`/api/jobs/${jobId}/items/${key}/image/${source.id}`); const url = URL.createObjectURL(blob); state.images.push(url); const image = node("img"); image.src = url; image.alt = source.title + " · " + source.locator; box.append(image); show.remove(); } catch (error) { toast(error.message); show.disabled = false; } }); box.append(show); } content.append(box); }
}
async function openPolicy(courseId, courseName) {
  try { const policy = await api("/api/policies/" + courseId); state.policyCourse = courseId; $("policyTitle").textContent = courseName || "课程 AI 规则"; $("policyUse").value = policy.ai_use; $("policyEvidence").value = policy.policy_evidence; $("policyLimits").value = policy.limitations; $("policyDisclosure").value = policy.disclosure; $("personalFacts").value = policy.personal_facts.join("\n"); $("attachmentBudget").value = policy.max_attachment_bytes / 1048576; $("pdfBudget").value = policy.max_pdf_pages; $("policyError").hidden = true; $("policyDialog").showModal(); } catch (error) { toast(error.message); }
}
$("refreshButton").addEventListener("click", () => action("/api/refresh", {}));
$("generateButton").addEventListener("click", () => { saveOptions(); action("/api/review", {selected: targets().map(({course_id, assignment_id}) => ({course_id, assignment_id})), defer_media: !$("processMedia").checked, ai_confirmed: $("aiConfirmed").checked, ...($("aiConfirmed").checked ? {model: $("modelSelect").value, effort: $("effortSelect").value || null} : {})}); });
$("aiConfirmed").checked = localStorage.getItem("classroom-ai-enabled") !== "false";
$("aiConfirmed").addEventListener("change", () => { localStorage.setItem("classroom-ai-enabled", String($("aiConfirmed").checked)); setButtons(); renderPending(); });
$("shutdownEnabled").addEventListener("change", event => changeShutdown(event.currentTarget.checked));
$("shutdownCancel").addEventListener("click", () => changeShutdown(false, true));
$("modelSelect").addEventListener("change", () => { renderEfforts(); saveOptions(); setButtons(); }); $("effortSelect").addEventListener("change", () => { saveOptions(); setButtons(); }); $("reloadModels").addEventListener("click", loadModels);
$("hideOverdue").addEventListener("click", () => { state.hideOverdue = !state.hideOverdue; localStorage.setItem("classroom-hide-overdue", String(state.hideOverdue)); if (state.hideOverdue) { let removed = 0; for (const item of state.pending?.assignments || []) if (overdue(item) && state.selected.delete(keyOf(item))) removed++; if (removed) toast(`已取消 ${removed} 项逾期作业的勾选，避免处理隐藏的作业。`); } renderPending(); });
$("searchInput").addEventListener("input", renderPending); for (const id of ["courseFilter", "dateFilter", "includeNoDue"]) $(id).addEventListener("change", renderPending);
$("selectAll").addEventListener("change", () => { for (const item of selectTargets(filtered(), new Set(), latestItems())) $("selectAll").checked ? state.selected.add(keyOf(item)) : state.selected.delete(keyOf(item)); renderPending(); });
$("clearSelection").addEventListener("click", () => { state.selected.clear(); renderPending(); });
$("clearFilters").addEventListener("click", () => { $("courseFilter").value = ""; $("dateFilter").value = ""; $("searchInput").value = ""; $("includeNoDue").checked = true; state.hideOverdue = false; localStorage.setItem("classroom-hide-overdue", "false"); renderPending(); });
$("navResults").addEventListener("click", () => setView("results"));
$("navPending").addEventListener("click", () => setView("pending")); $("navHistory").addEventListener("click", () => setView("history"));
for (const close of document.querySelectorAll(".close-dialog")) close.addEventListener("click", () => close.closest("dialog").close()); $("reviewDialog").addEventListener("close", clearImages);
for (const tab of document.querySelectorAll(".review-tabs button")) tab.addEventListener("click", () => renderTab(tab.dataset.tab));
async function downloadAnswer(jobId, key) {
  try {
    const job = await api(`/api/jobs/${jobId}/items/${key}/export-answer`, "POST", {});
    updateJob(job); renderJobs(); renderPending();
    const blob = await api(`/api/jobs/${jobId}/items/${key}/answer-docx`);
    const url = URL.createObjectURL(blob), link = node("a");
    const item = job.items.find(x => keyOf(x) === key);
    link.href = url; link.download = (item?.title || "Classroom-答案").replace(/[\\/:*?"<>|]/g, "_") + "-审阅稿.docx";
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (state.preview?.jobId === jobId && state.preview?.key === key) {
      state.preview.data = await api(`/api/jobs/${jobId}/items/${key}`); renderTab("draft");
    }
    toast("Word 审阅稿已下载；若有待补充项，请先补齐再手动提交。");
  } catch (error) { toast(error.message); }
}
$("downloadAnswerButton").addEventListener("click", () => { const {jobId, key} = state.preview; downloadAnswer(jobId, key); });
$("downloadButton").addEventListener("click", async () => { try { const {jobId, key, data} = state.preview; const blob = await api(`/api/jobs/${jobId}/items/${key}/download`); const url = URL.createObjectURL(blob), link = node("a"); link.href = url; link.download = (data.text["draft.md"] ? "Classroom-初稿证据包-" : "Classroom-资料待确认包-") + key.replace(":", "-") + ".zip"; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); } catch (error) { toast(error.message); } });
$("policyForm").addEventListener("submit", async event => { event.preventDefault(); const submit = event.submitter; submit.disabled = true; try { await api("/api/policies/" + state.policyCourse, "PUT", {ai_use: $("policyUse").value, policy_evidence: $("policyEvidence").value, limitations: $("policyLimits").value, disclosure: $("policyDisclosure").value, personal_facts: $("personalFacts").value.split("\n").map(x => x.trim()).filter(Boolean), max_attachment_bytes: Math.round(Number($("attachmentBudget").value) * 1048576), max_pdf_pages: Number($("pdfBudget").value)}); $("policyDialog").close(); await loadState(); toast("已保存课程设置；重新生成所选审核包时会应用这些设置。"); } catch (error) { $("policyError").textContent = error.message; $("policyError").hidden = false; } finally { submit.disabled = false; } });
$("quitButton").addEventListener("click", async () => { try { await api("/api/shutdown", "POST", {}); $("notice").textContent = "助手已退出。下次双击快捷方式即可重新打开，任务记录与缓存会保留。"; $("notice").hidden = false; $("refreshButton").disabled = true; $("generateButton").disabled = true; $("quitButton").disabled = true; } catch (error) { toast(error.message); } });
for (const id of ["openSettings", "modelShortcut"]) $(id).addEventListener("click", () => $("settingsDialog").showModal());
$("processMedia").checked = localStorage.getItem("classroom-process-media") !== "false";
$("processMedia").addEventListener("change", saveOptions);
if (!token) { $("notice").textContent = "请双击“Classroom 作业助手”快捷方式打开本机界面。此页面没有访问学校账户的本机凭证。"; $("notice").hidden = false; $("refreshButton").disabled = true; }
else {
  loadState().then(() => {
    if (state.monitorOnly) { $("modelStatus").textContent = "只读进度窗口"; return; }
    loadModels();
    syncWhenIdle();
  }).catch(error => { $("notice").textContent = error.message; $("notice").hidden = false; toast(error.message); });
}

$("authorizeDocuments").addEventListener("click", () => { $("settingsDialog").close(); setView("pending"); action("/api/documents/authorize", {}); });
$("fillDocumentButton").addEventListener("click", () => { const {jobId, key} = state.preview; $("reviewDialog").close(); action(`/api/jobs/${jobId}/items/${key}/fill`, {}); });

async function openSupplement(jobId, key) {
  if (state.monitorOnly) { toast("这是只读进度窗口，请返回原作业助手填写信息。"); return; }
  try {
    const job = state.jobs.find(x => x.id === jobId) || await api(`/api/jobs/${jobId}`);
    const item = job.items.find(x => keyOf(x) === key);
    if (!item) throw new Error("找不到这项作业");
    const data = item.package ? await api(`/api/jobs/${jobId}/items/${key}`) : null; const supplement = data?.supplement || await api(`/api/jobs/${jobId}/items/${key}/supplement`);
    state.supplement = {jobId, key, model: state.models?.models.some(x=>x.model === $("modelSelect").value) ? $("modelSelect").value : job.model || null, effort: $("effortSelect").value || job.effort || null, actualModel:data?.generation?.model || item.generation?.model, actualEffort:data?.generation?.reasoning_effort || item.generation?.reasoning_effort, editors: [], savedText:supplement.text || "", savedFacts:supplement.personal_facts || []};
    $("supplementTitle").textContent = item.title || key;
    $("supplementText").value = supplement.text || "";
    $("supplementFacts").value = (supplement.personal_facts || []).join("\n");
    renderAnswerInputs(data); updateSupplementActions();
    $("supplementQuestions").replaceChildren(); if (data?.review?.questions?.length || item.error) $("supplementQuestions").append(node("strong", "上次处理留下的问题（本次继续会重新核验）"));
    for (const question of data?.review?.questions || []) $("supplementQuestions").append(node("p", question));
    if (item.error) $("supplementQuestions").append(node("p", friendlyError(item.error))); if (item.browser_read?.thread_id) { const readLink = codexLink(item.browser_read); if (readLink) { readLink.textContent = "打开登录题目读取会话 ↗"; $("supplementQuestions").append(readLink); } }
    $("supplementAi").checked = !!job.ai_confirmed;
    $("supplementError").hidden = true;
    $("followupNote").textContent = "已在这项作业的 Codex 会话里补充过？点击读取已完成的回复，随后继续核验与作答。";
    $("receiveFollowup").disabled = !item.generation?.thread_id || !!state.active;
    const links = $("restrictedFormLinks"); links.replaceChildren();
    const urls = [...new Set([...(data?.requirements?.response_forms || []).map(x=>x.url), ...(data?.manifest?.warnings || []).map(x=>x.url)].filter(url=>/^https:\/\/docs\.google\.com\/forms\//.test(url || "")))];
    for (const url of urls) { const link = safeLink(url, "用学校账户打开原表单 ↗", "text-button"); if (link) links.append(link); }
    $("captureUrl").value = urls[0] || ""; $("captureScript").value = "";
    $("browserQuestionSection").open = false;
    if ($("reviewDialog").open) $("reviewDialog").close();
    $("supplementDialog").showModal();
  } catch (error) { toast(error.message); }
}
async function supplementAction(button, operation) {
  button.disabled = true; $("supplementError").hidden = true;
  try { await operation(); } catch (error) { $("supplementError").textContent = error.message; $("supplementError").hidden = false; }
  finally { button.disabled = false; }
}
async function saveSupplement(continueRun) {
  const {jobId, key, model, effort} = state.supplement;
  if (state.supplement.editors.length && !supplementNotesChanged()) return saveFormInputs(continueRun);
  if (state.supplement.editors.length) await saveFormInputs(false, false);
  const payload = {text: $("supplementText").value, personal_facts: $("supplementFacts").value.split("\n").map(x=>x.trim()).filter(Boolean)};
  if (continueRun) Object.assign(payload, {ai_confirmed: $("supplementAi").checked, model, effort});
  await api(`/api/jobs/${jobId}/items/${key}/${continueRun ? "continue" : "supplement"}`, "POST", payload);
  $("supplementDialog").close(); await loadState();
  if (continueRun) setView("history");
  toast(continueRun ? "补充已保存，正在继续处理这项作业" : "补充已保存到本机，下次处理时使用");
}
// Original native fields and choices are data; none can change destinations or permissions.
function supplementFields(data) {
  const known = new Map((data?.model_form_answers || []).filter(x=>x.values?.length).map(x=>[`${x.form_url}:${x.entry_id}`, x]));
  const supplied = new Map((data?.user_form_answers?.answers || []).map(x=>[`${x.form_url}:${x.entry_id}`, x]));
  return (data?.response_fields || []).flatMap(form=>form.questions.flatMap(question=>question.fields.map(field=>({form, question, field, supplied: supplied.get(`${form.url}:${field.entry_id}`), generated: known.has(`${form.url}:${field.entry_id}`)})))).filter(x=>!x.generated || x.supplied).sort((a,b)=>a.question.page - b.question.page || a.question.title.localeCompare(b.question.title, "ja", {numeric:true}));
}
function renderAnswerInputs(data) {
  const section = $("answerInputsSection"), content = $("answerInputs"); content.replaceChildren();
  const fields = supplementFields(data); section.hidden = !fields.length && !data?.response_fields_error;
  $("answerInputNote").textContent = data?.response_fields_error || "按原题填写或选择。回答只保存在本机，自动带入原表单；个人情况采用你提供的内容，提交由你操作。";
  $("extraSupplement").open = !fields.length;
  for (const {form, question, field, supplied} of fields) {
    const group = node("div", undefined, "answer-field"), id = `answer-input-${state.supplement.editors.length}`;
    const label = node("label", `第 ${question.page} 页 · ${question.title}${field.required ? "（必填）" : "（选填）"}`); label.htmlFor = id; group.append(label);
    if (question.description) group.append(node("p", question.description, "small muted"));
    let control, read;
    if (["single_choice", "dropdown", "scale"].includes(question.type) && field.choices.length) {
      control = node("select"); const empty = node("option", "请选择你的真实情况"); empty.value = ""; control.append(empty);
      for (const value of field.choices) { const option = node("option", value); option.value = value; control.append(option); }
      control.value = supplied?.values?.[0] || ""; read = ()=>control.value ? [control.value] : [];
    } else if (["short_text", "paragraph"].includes(question.type)) {
      control = node(question.type === "paragraph" ? "textarea" : "input"); if (question.type === "paragraph") control.rows = 3; else control.type = "text";
      control.maxLength = 20000; control.value = supplied?.values?.[0] || ""; control.placeholder = "在这里填写你的回答"; read = ()=>control.value.trim() ? [control.value.trim()] : [];
    } else if (question.type === "checkbox" && field.choices.length) {
      control = node("fieldset", undefined, "answer-options"); const options = [];
      for (const value of field.choices) { const option = node("label", undefined, "checkbox-label"), input = node("input"); input.type = "checkbox"; input.value = value; input.checked = supplied?.values?.includes(value) || false; option.append(input, node("span", value)); control.append(option); options.push(input); }
      read = ()=>options.filter(x=>x.checked).map(x=>x.value);
    } else { group.append(node("p", "此题需要在原表单使用原生控件填写。", "small muted")); content.append(group); continue; }
    control.id = id; group.append(control); content.append(group);
    state.supplement.editors.push({form_url:form.url, entry_id:field.entry_id, context_sha256:form.context_sha256, read});
  }
}
function supplementNotesChanged() { return $("supplementText").value !== state.supplement.savedText || JSON.stringify($("supplementFacts").value.split("\n").map(x=>x.trim()).filter(Boolean)) !== JSON.stringify(state.supplement.savedFacts); }
function updateSupplementActions() {
  const answersOnly = state.supplement.editors.length && !supplementNotesChanged();
  $("saveSupplement").textContent = answersOnly ? "保存回答" : "保存补充";
  $("continueSupplement").textContent = answersOnly ? "保存回答并自动填入" : "保存并继续处理";
  $("supplementAiRow").hidden = !!answersOnly;
  const s = state.supplement;
  $("supplementModel").textContent = answersOnly ? `已有答案来自 ${s.actualModel || "任务所选模型"} · ${s.actualEffort || "默认强度"}。只合并你的回答，不重新调用 AI。` : `继续使用模型：${s.model || "未选择（只整理资料）"} · ${s.effort || "默认强度"}。可先关闭窗口在主界面选择其他模型。`;
}
async function saveFormInputs(apply, close = true) {
  const {jobId, key, editors} = state.supplement;
  const answers = editors.map(({read, ...mapping})=>({...mapping, values:read()})).filter(x=>x.values.length);
  const result = await api(`/api/jobs/${jobId}/items/${key}/answers`, "POST", {answers, apply});
  if (!close) return result;
  $("supplementDialog").close(); await loadState(); if (apply) setView("history");
  toast(result.apply_queued ? "回答已保存，当前任务结束后自动填入；不会提交" : apply ? "回答已保存，正在自动填入原表单；不会提交" : "逐题回答已保存到本机");
}
$("supplementButton").addEventListener("click", () => openSupplement(state.preview.jobId, state.preview.key));
for (const id of ["supplementText", "supplementFacts"]) $(id).addEventListener("input", updateSupplementActions);
$("saveSupplement").addEventListener("click", event => supplementAction(event.currentTarget, () => saveSupplement(false)));
$("supplementForm").addEventListener("submit", event => { event.preventDefault(); supplementAction(event.submitter, () => saveSupplement(true)); });
$("receiveFollowup").addEventListener("click", event => supplementAction(event.currentTarget, async () => {
  const {jobId, key} = state.supplement;
  // Persist the user's current typing before merging a completed follow-up.
  await api(`/api/jobs/${jobId}/items/${key}/supplement`, "POST", {text: $("supplementText").value, personal_facts: $("supplementFacts").value.split("\n").map(x=>x.trim()).filter(Boolean)});
  const result = await api(`/api/jobs/${jobId}/items/${key}/followup`, "POST", {});
  $("supplementText").value = result.supplement.text;
  updateSupplementActions();
  $("followupNote").textContent = `已接收实际回合 ${result.followup.turn_id} · 模型 ${result.followup.model || "原会话"}。点击保存并继续，重新核验来源并作答。`;
  toast("已接收 Codex 补充，尚未提交");
}));
$("importQuestions").addEventListener("click", event => supplementAction(event.currentTarget, async () => {
  const {jobId, key} = state.supplement;
  const result = await api(`/api/jobs/${jobId}/items/${key}/questions`, "POST", {url: $("captureUrl").value, question_script: $("captureScript").value});
  $("captureScript").value = ""; toast(`已导入真实题目结构：${result.page_count} 页、${result.question_count} 栏。点击保存并继续。`);
}));

$("courseSettingsButton").addEventListener("click", () => { const {data} = state.preview; openPolicy(data.manifest.course_id, data.manifest.course_name); });
