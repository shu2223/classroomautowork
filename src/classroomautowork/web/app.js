"use strict";
const $ = id => document.getElementById(id);
const state = {documentsAuthorized: false, pending: null, jobs: [], policies: {}, selected: new Set(), active: null, focus: null, view: "pending", busy: false, preview: null, policyCourse: null, images: [], models: null, modelsLoading: false, hideOverdue: localStorage.getItem("classroom-hide-overdue") === "true"};
const activeStates = new Set(["queued", "running", "stopping"]);
const labels = {queued: "等待开始", running: "正在处理", stopping: "正在暂停", waiting: "等待处理", preparing: "读取题目与资料", prepared: "资料已准备", drafting: "Codex 正在分析", ready: "答案已生成 · 待你审阅", filling_document: "正在填入原文档", document_ready: "已填入 · 打开文档审阅", document_needs_user: "已填入可用答案 · 尚有空栏待确认", needs_document: "初稿已保留 · 填入需处理", needs_user: "需要补充", materials_ready: "资料已整理 · 未调用 AI", paused: "已暂停", interrupted: "上次已中断", failed: "处理失败", completed: "处理已结束", completed_with_issues: "部分处理未完成"};
const keyOf = item => item.course_id + ":" + item.assignment_id;
let token = location.hash.slice(1);
if (token) { sessionStorage.setItem("classroom-access", token); history.replaceState(null, "", location.pathname); }
else token = sessionStorage.getItem("classroom-access") || "";
let polling = null, toastTimer = null;
let pollInFlight = false;
const connection = {lastSuccess: 0, error: null};

// Pure progress presentation; exercised by offline state tests.
function durationLabel(ms) { const s = Math.max(0, Math.floor(ms / 1000)); return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`; }
function ageOf(at, now) { const value = Date.parse(at); return Number.isFinite(value) ? Math.max(0, now - value) : null; }
function progressView(job, network, now = Date.now()) {
  const items = job.items || [], p = job.progress || {}, active = activeStates.has(job.status);
  const item = items.find(x => ["preparing", "prepared", "drafting", "filling_document"].includes(x.status)) || items.find(x => x.status === "waiting");
  let stage = p.stage || (item?.status === "filling_document" ? "fill" : item?.status === "drafting" ? "ai" : "prepare");
  if (job.kind === "fill") stage = "fill";
  if (job.kind === "document_auth") stage = "authorization";
  if (job.kind === "refresh") stage = "course_sync";
  if (job.approval) stage = "approval";
  if (!job.progress && !active && items.some(x => x.status === "failed" && x.generation?.turn_status === "completed")) stage = "validate";
  const descriptions = {
    prepare: ["准备题目与课堂资料", "读取作业要求、同课程资料、历史作业和公告；这一阶段还未调用 AI。"],
    course_sync: ["同步课程内容", "读取学校账户内的课程记录，查找本次作业的来源。"],
    form_read: ["读取表单的全部题目", "只读获取 Google Forms 所有分页的题目和选项，身份页之后的题目也会加入本次作业要求。"],
    attachment: ["核验课程附件", "检查文件权限、版本与缓存，只处理新增或修改的材料。"],
    download: ["下载课程附件", "下载量来自真实收到的字节；大文件可能耗时，之后还要校验完整性。"],
    download_verify: ["校验附件完整性", "核对下载大小和校验值，避免使用不完整文件。"],
    extract: ["提取文档文字和页图", "逐页读取内容并保留必要图像，供 Codex 阅读题目与课堂资料。"],
    transcribe: ["Buzz 正在本机转录录像", "分段识别语音并保存时间戳。单段处理期间计数可能暂时不变，完成后继续更新。"],
    attachment_done: ["附件内容已准备", "材料已处理并缓存，继续检查其他附件。"],
    document_inspect: ["核对作业填写方式", "检查有无个人 Google 文档答案栏；Forms 题目已读取，逐题答案在本机审阅后由你填写。"],
    ai: ["Codex 正在阅读与作答", "使用所选模型分析真实题目和资料。AI 没有可测量的总百分比；显示实际活动与会话入口。"],
    validate: ["核验 AI 结果", "检查来源、格式与题目完整性，通过后才填入文档。"],
    fill: ["填写并回读 Google 文档", "填写个人作业的答案栏，回读核验后由你审阅并手动提交。"],
    approval: ["等待你的操作审批", "请在作业助手查看具体请求并批准或拒绝；等待期间不会继续。"],
    authorization: ["等待 Google 授权", "请在 Google 页面完成学校账户授权，然后返回助手。"],
  };
  let [title, explanation] = descriptions[stage] || descriptions.prepare;
  const legacy = active && !job.approval && !job.progress && /^Checking attachment /.test(job.message || "");
  if (legacy) { title = "正在处理课程附件"; explanation = "这次由旧版启动，未报告附件内部的子步骤。下载、提取或转录可能耗时，保持任务运行，无需重新开始。"; }
  const ready = items.filter(x => ["document_ready", "document_needs_user"].includes(x.status)).length;
  const localReady = items.filter(x => x.status === "ready").length;
  const failed = items.filter(x => x.status === "failed").length;
  const finished = items.filter(x => ["ready", "document_ready", "document_needs_user", "needs_document", "needs_user", "materials_ready", "failed"].includes(x.status)).length;
  const stepAt = job.approval?.requested_at || p.started_at || item?.stage_started_at || job.started_at || job.created_at;
  const activityAt = p.activity_at || item?.generation?.activity_at || job.events?.at(-1)?.at || job.updated_at;
  const silence = ageOf(activityAt, now);
  const disconnected = !!network.error || !!(network.lastSuccess && now - network.lastSuccess > 12000);
  let warning = null;
  if (active && disconnected) warning = "暂时无法读取新状态；后台任务不一定停止。恢复连接后会自动重连，请先不要重复启动。";
  else if (active && ["approval", "authorization"].includes(stage)) warning = explanation;
  else if (active && silence !== null && silence >= (stage === "transcribe" ? 300000 : 120000)) warning = `已有 ${durationLabel(silence)} 没有新进展记录。${stage === "transcribe" ? "一段转录可能较慢，不能仅凭时间认定失败。" : "服务响应正常不代表这一步有进展，可展开处理记录核对。"}`;
  let measurement = null;
  if (active && Number.isFinite(p.current) && Number.isFinite(p.total) && p.total > 0 && p.current >= 0 && p.current <= p.total) {
    const unit = {pages:"页", segments:"段"}[p.unit] || "项", format = v => p.unit === "bytes" ? `${(v / 1048576).toFixed(1)} MB` : String(v);
    measurement = {value:p.current, max:p.total, text:p.unit === "bytes" ? `已下载 ${format(p.current)} / ${format(p.total)}` : `已完成 ${format(p.current)} / ${format(p.total)} ${unit}`};
  }
  if (!active) { title = failed && stage === "validate" ? "结果核验未通过" : labels[job.status] || job.status; explanation = failed ? `本次 ${failed} 项失败；请查看下方具体原因。失败项没有填入文档，可以在原助手补齐资料后重试。` : ready ? "请打开已填入的原文档审阅，最后由你手动提交。" : localReady ? "逐题答案已核验。点击查看初稿审阅，Forms 需要你在原表单填写并手动提交。" : "请查看各项作业的结果与待处理事项。"; }
  const index = !active && (ready || localReady) ? 3 : stage === "fill" ? 2 : ["ai", "validate", "approval"].includes(stage) ? 1 : 0;
  const nowOrEnd = job.finished_at ? Date.parse(job.finished_at) : now;
  return {active, title, explanation, stage, index, measurement, ready, localReady, failed, finished, total:items.length, filename:p.filename || null, attachments:p.attachment_total ? `课程附件 ${p.attachment_index}/${p.attachment_total}` : null, elapsed:ageOf(job.started_at || job.created_at, nowOrEnd), stepElapsed:ageOf(stepAt, nowOrEnd), silence, warning, disconnected, workerAlive:job.runtime?.worker_alive === true, legacy};
}
// End pure progress presentation.

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
  if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(friendlyError(error.error || "本机连接失败，请重新打开助手。")); }
  return response.headers.get("Content-Type").includes("application/json") ? response.json() : response.blob();
}
function dateLabel(value, time = false) { if (!value) return "无截止日期"; return new Intl.DateTimeFormat("zh-CN", {timeZone: state.timezone || "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", ...(time ? {hour: "2-digit", minute: "2-digit"} : {})}).format(new Date(value)); }
function localDay(value) { return value ? value.slice(0, 10) : ""; }
function safeLink(url, text, className) { try { const u = new URL(url); if (u.protocol !== "https:" || !(u.hostname.endsWith(".google.com") || ["google.com", "youtube.com", "www.youtube.com", "youtu.be"].includes(u.hostname))) return null; const a = node("a", text, className); a.href = u.href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; } catch { return null; } }
function pill(status) { return node("span", labels[status] || status, "pill " + ({ready: "success", needs_user: "warning", failed: "failure", interrupted: "warning", paused: "warning"}[status] || "")); }
function codexLink(generation) { const id = generation?.thread_id; if (!id || !/^[a-f0-9-]{36}$/i.test(id)) return null; const link = node("a", "在 Codex 中打开真实会话 ↗", "generation-link"); link.href = "codex://threads/" + id; return link; }
function overdue(item) { return !!item.due_utc && new Date(item.due_utc) < new Date(); }
function latestItems() { const found = new Map(); for (const job of [...state.jobs].sort((a,b) => b.created_at.localeCompare(a.created_at))) if (["review", "fill"].includes(job.kind)) for (const item of job.items) if (!found.has(keyOf(item))) found.set(keyOf(item), {job, item}); return found; }
function filtered() {
  const normalize = value => value.normalize("NFKC").replace(/\s+/g, " ").trim().toLocaleLowerCase();
  const q = normalize($("searchInput").value), course = $("courseFilter").value, date = $("dateFilter").value;
  return (state.pending?.assignments || []).filter(item => (!state.hideOverdue || !overdue(item)) && (!course || item.course_id === course) && (!q || normalize((item.title || "") + " " + (item.course_name || "")).includes(q)) && (item.due_local ? (!date || localDay(item.due_local) <= date) : $("includeNoDue").checked));
}
function setButtons() {
  const busy = !!state.active || state.busy || !!state.monitorOnly; $("authorizeDocuments").disabled = busy; $("authorizeDocuments").hidden = state.documentsAuthorized; $("documentPermissionNote").textContent = state.documentsAuthorized ? "自动填入已授权：答案写入个人副本，你打开原文档审阅并手动提交。" : "首次填入需 Google Docs 读写授权。Google 授权范围涵盖可编辑文档；本程序只填入本人待完成作业副本。";
  $("refreshButton").disabled = busy; $("refreshButton").classList.toggle("spinner", !!state.active && state.jobs.find(x => x.id === state.active)?.kind === "refresh");
  $("generateButton").disabled = busy || !state.selected.size || ($("aiConfirmed").checked && (!state.models || state.modelsLoading));
  $("aiConfirmed").disabled = busy;
  $("generateButton").querySelector("span").textContent = $("aiConfirmed").checked ? "生成答案并准备审阅" : "准备所选资料";
  $("aiChoiceNote").textContent = $("aiConfirmed").checked ? "已勾选：使用所选模型作答；个人 Google 文档自动填入，Forms 生成逐题答案供你审阅填写，最后均由你手动提交。" : "未勾选：只读取和整理资料，不调用 AI。无需填写规则原文。";
  $("modelSelect").disabled = busy || !state.models || state.modelsLoading; $("effortSelect").disabled = busy || !state.models || state.modelsLoading; $("reloadModels").disabled = busy || state.modelsLoading;
  $("selectedCount").textContent = state.selected.size;
  $("clearSelection").disabled = !state.selected.size;
  $("quitButton").disabled = busy;
}
function renderCourseFilters() {
  const courses = new Map(); for (const item of state.pending?.assignments || []) { const prior = courses.get(item.course_id); courses.set(item.course_id, {name: item.course_name || "未命名课程", count: (prior?.count || 0) + 1}); }
  const current = $("courseFilter").value; $("courseFilter").replaceChildren(node("option", "全部课程")); $("courseFilter").firstChild.value = "";
  $("courseNav").replaceChildren();
  for (const [id, course] of courses) { const option = node("option", course.name); option.value = id; $("courseFilter").append(option); const b = button("", "course-button", () => { $("courseFilter").value = id; setView("pending"); renderPending(); }); b.append(node("span", "", "course-dot"), node("span", course.name), node("b", course.count)); b.title = course.name; b.dataset.course = id; $("courseNav").append(b); }
  if (courses.has(current)) $("courseFilter").value = current;
  if (!courses.size) $("courseNav").append(node("span", "刷新后显示你的课程", "muted small"));
  $("courseCount").textContent = courses.size ? courses.size + " 门课程" : "等待刷新";
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
function renderPending() {
  const pending = state.pending, items = filtered(), latest = latestItems();
  $("totalCount").textContent = pending ? pending.assignments.length : "—"; $("navCount").textContent = pending ? pending.assignments.length : "—";
  $("overdueCount").textContent = pending ? pending.assignments.filter(x => x.due_utc && new Date(x.due_utc) < new Date()).length : "—";
  $("readyCount").textContent = [...latest.values()].filter(x => ["ready", "document_ready", "document_needs_user"].includes(x.item.status)).length;
  $("visibleCount").textContent = items.length;
  $("refreshTime").textContent = pending ? `上次实时刷新 ${dateLabel(pending.refreshed_at, true)} · 列表保留到下次刷新` : "点击刷新，从 Classroom 读取实时待办";
  $("clearFilters").hidden = !($("courseFilter").value || $("searchInput").value || $("dateFilter").value || !$("includeNoDue").checked || state.hideOverdue);
  $("hideOverdue").setAttribute("aria-pressed", String(state.hideOverdue)); $("hideOverdue").textContent = state.hideOverdue ? "已隐藏逾期 · 显示全部" : "隐藏已逾期";
  for (const b of $("courseNav").children) b.classList.toggle("active", b.dataset.course === $("courseFilter").value);
  const checked = items.filter(x => state.selected.has(keyOf(x))).length;
  $("selectAll").checked = !!items.length && checked === items.length; $("selectAll").indeterminate = checked > 0 && checked < items.length; $("selectAll").disabled = !items.length;
  if (!pending) { setButtons(); return; }
  const list = $("assignmentList"); list.replaceChildren();
  if (!items.length) { const empty = node("div", undefined, "empty-state"); empty.append(node("h3", pending.assignments.length ? "当前筛选下没有作业" : "当前没有可自动处理的待办"), node("p", pending.assignments.length ? "调整课程、关键词或截止日期试试。" : "可以稍后刷新；提交状态不明的项目会在下方单独列出。")); list.append(empty); }
  for (const item of items) {
    const key = keyOf(item), row = node("article", undefined, "assignment-row"); row.classList.toggle("selected", state.selected.has(key));
    const label = node("label"), check = node("input"); check.type = "checkbox"; check.checked = state.selected.has(key); check.setAttribute("aria-label", "选择作业 " + (item.title || item.assignment_id)); check.addEventListener("change", () => { check.checked ? state.selected.add(key) : state.selected.delete(key); renderPending(); });
    const courseIcon = node("span", undefined, "course-icon"); courseIcon.append(icon("book"));
    const body = node("div", undefined, "assignment-main"), meta = node("div", undefined, "assignment-meta"); body.append(node("span", item.title || "未命名作业", "assignment-title")); meta.append(node("span", item.course_name || "未命名课程")); const link = safeLink(item.url, "查看课堂 ↗"); if (link) meta.append(link); body.append(meta); label.append(check, courseIcon, body);
    const side = node("div", undefined, "assignment-aside"), overdue = item.due_utc && new Date(item.due_utc) < new Date(); side.append(node("span", (overdue ? "已逾期 · " : "截止 · ") + dateLabel(item.due_local, true), "due-label" + (overdue ? " overdue" : "")));
    const completed = latest.get(key), policy = state.policies[item.course_id];
    if (completed) side.append(pill(completed.item.status));
    else side.append(node("span", policy?.ai_use === "forbidden" ? "课程禁止生成答案" : $("aiConfirmed").checked ? "可生成初稿" : "仅准备资料", "pill " + (policy?.can_draft ? "success" : "warning")));
    const actions = node("div", undefined, "row-actions"); const policyButton = button("课程规则", "row-action", () => openPolicy(item.course_id, item.course_name)); policyButton.disabled = !!state.active; actions.append(policyButton);
    if (completed?.item.package) actions.append(button(completed.item.status === "ready" ? "查看初稿" : "查看资料与分析", "row-action", () => openReview(completed.job.id, key)));
    for (const d of completed?.item.document_fill?.documents || []) { const link = safeLink(d.url, "打开已填入的文档 ↗", "row-action"); if (link) actions.prepend(link); } side.append(actions); row.append(label, side); list.append(row);
  }
  renderDiscovery(); setButtons();
}
function updateJob(job) { const at = state.jobs.findIndex(x => x.id === job.id); if (at < 0) state.jobs.unshift(job); else state.jobs[at] = job; }
function jobProgress(job) {
  const v = progressView(job, connection), box = node("div", undefined, "job-live-detail");
  box.dataset.jobId = job.id;
  if (["review", "fill"].includes(job.kind) && job.ai_confirmed) {
    const steps = node("ol", undefined, "job-steps");
    for (const [i, title] of ["准备资料", "Codex 作答", "准备审阅", "人工审阅"].entries()) { const step = node("li", undefined, i === v.index ? "current" : i < v.index ? "past" : ""); step.append(node("span", i < v.index ? "✓" : String(i + 1)), node("b", title)); steps.append(step); }
    box.append(steps);
  }
  box.append(node("strong", v.title), node("p", v.explanation));
  if (v.active) { const progress = node("progress", undefined, "job-progress"); progress.setAttribute("aria-label", v.measurement?.text || "当前步骤没有可测量的总进度"); if (v.measurement) { progress.max = v.measurement.max; progress.value = v.measurement.value; } else progress.classList.add("indeterminate"); box.append(progress, node("p", v.measurement?.text || "处理中 · 当前步骤没有可测量的总百分比", "job-measurement")); }
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
  const card = node("section", undefined, full ? "history-card" : ""); const heading = node("div", undefined, "job-heading"), intro = node("div");
  intro.append(node("h3", job.kind === "refresh" ? "同步真实待办" : job.kind === "document_auth" ? "Google 文档授权" : `所选 ${job.items.length} 项作业 · ${labels[job.status] || job.status}`));
  if (full) intro.append(node("span", dateLabel(job.created_at, true), "muted small")); heading.append(intro);
  if (activeStates.has(job.status) && !state.monitorOnly) { const pause = button(job.status === "stopping" ? "正在暂停…" : "暂停", "button secondary small-button", () => jobAction(job.id, "pause")); pause.disabled = job.status === "stopping"; heading.append(pause); }
  else if (!state.monitorOnly && (job.kind === "review" || job.status !== "completed")) heading.append(button("重试 / 继续", "button secondary small-button", () => jobAction(job.id, "retry")));
  card.append(heading);
  card.append(jobProgress(job));
  const items = node("div", undefined, "job-items");
  for (const item of job.items) { const row = node("div", undefined, "job-item"); const title = node("span", item.title || item.assignment_id); if (item.error) title.append(node("p", friendlyError(item.error))); row.append(title, pill(item.status)); if (item.package) row.append(button(item.status === "ready" ? "查看实际初稿" : "查看资料与分析", "text-button", () => openReview(job.id, keyOf(item)))); const g = item.generation; if (g?.thread_id) { const meta = node("div", undefined, "job-model"); meta.append(node("span", `实际模型 ${g.model} · ${g.reasoning_effort || "默认强度"} · ${g.status === "completed" ? "AI 回合完成" : g.status === "running" ? "正在生成" : "尚未成功"} `)); const link = codexLink(g); if (link) meta.append(link); row.append(meta); } else if (item.package) row.append(node("span", "资料已准备 · 没有已核验的 AI 生成记录", "job-model")); for (const d of item.document_fill?.documents || []) { const link = safeLink(d.url, "在原文档审阅 ↗", "button primary small-button"); if (link) row.prepend(link); } if (item.package && g?.status === "completed" && item.status === "needs_document") row.append(button("填入已有初稿", "text-button", () => action(`/api/jobs/${job.id}/items/${keyOf(item)}/fill`, {}))); items.append(row); } card.append(items);
  if (job.approval && !state.monitorOnly) { const box = node("section", undefined, "approval-card"); box.append(node("h3", "Codex 请求本次操作审批"), node("p", job.approval.reason), node("pre", job.approval.command || job.approval.cwd), node("p", "此请求来自 Codex 的现有权限规则；批准仅适用于本次，不会扩大长期权限。")); const actions = node("div", undefined, "approval-actions"); for (const [choice, label] of [["accept", "批准本次"], ["decline", "拒绝"], ["cancel", "取消操作"]]) actions.append(button(label, "button secondary small-button", async () => { try { await api(`/api/jobs/${job.id}/approval`, "POST", {id: job.approval.id, decision: choice}); await pollJob(); } catch(error) { toast(error.message); } })); box.append(actions); card.append(box); }
  if (job.cache) card.append(node("p", `资料缓存：复用 ${job.cache.reused} 个阶段 · 新增 ${job.cache.processed} · 缓存阶段失败 ${job.cache.failed}`, "small"));
  if (job.events.length) { const details = node("details"), summary = node("summary", "查看处理记录"), events = node("div", undefined, "event-list"); for (const entry of job.events) { const line = node("div", undefined, "event-line"); line.append(node("time", new Date(entry.at).toLocaleTimeString("zh-CN", {timeZone: state.timezone || "Asia/Tokyo", hour: "2-digit", minute: "2-digit", second: "2-digit"})), node("span", entry.message)); events.append(line); } details.dataset.jobLog = job.id; details.append(summary, events); card.append(details); }
  return card;
}
function renderJobs() {
  const expanded = new Set([...document.querySelectorAll("details[data-job-log][open]")].map(x => x.dataset.jobLog));
  const focus = state.jobs.find(x => x.id === (state.active || state.focus)); $("jobPanel").hidden = !focus || (focus.kind === "refresh" && ["completed", "completed_with_issues"].includes(focus.status)); $("jobPanel").replaceChildren(); if (!$("jobPanel").hidden) $("jobPanel").append(jobCard(focus));
  $("historyList").replaceChildren(); if (!state.jobs.length) $("historyList").append(node("div", "还没有审核记录。刷新待办并勾选作业后，这里会保留处理进度与结果。", "empty-state muted small"));
  for (const job of [...state.jobs].sort((a,b) => b.created_at.localeCompare(a.created_at))) $("historyList").append(jobCard(job, true)); setButtons();
  for (const details of document.querySelectorAll("details[data-job-log]")) details.open = expanded.has(details.dataset.jobLog);
}
function updateProgressClock() {
  for (const box of document.querySelectorAll(".job-live-detail")) {
    const job = state.jobs.find(x => x.id === box.dataset.jobId); if (!job || !activeStates.has(job.status)) continue;
    const v = progressView(job, connection), spans = box.querySelectorAll(".job-times span");
    if (spans.length === 4) { spans[0].textContent = `总耗时 ${durationLabel(v.elapsed || 0)}`; spans[1].textContent = `本步骤 ${durationLabel(v.stepElapsed || 0)}`; spans[2].textContent = v.silence === null ? "尚无处理记录" : `最近进展 ${durationLabel(v.silence)}前`; spans[3].textContent = v.disconnected ? "状态连接中断 · 自动重连中" : `服务连接正常${v.workerAlive ? " · 工作线程仍在运行" : ""}`; spans[3].classList.toggle("warning", v.disconnected); }
    const warning = box.querySelector(".job-warning"); warning.textContent = v.warning || ""; warning.hidden = !v.warning;
  }
}
setInterval(updateProgressClock, 1000);
async function loadState() {
  const data = await api("/api/bootstrap"); connection.lastSuccess = Date.now(); connection.error = null; state.monitorOnly = !!data.monitor_only; state.documentsAuthorized = !!data.documents_authorized; state.pending = data.pending; state.jobs = data.jobs; state.policies = data.policies; state.active = data.active_job_id; state.timezone = data.timezone; $("accountEmail").textContent = data.account;
  if (state.active) $("aiConfirmed").checked = !!state.jobs.find(x => x.id === state.active)?.ai_confirmed;
  if (state.monitorOnly) { document.body.classList.add("monitor-mode"); $("notice").hidden = false; $("notice").replaceChildren(node("span", "这是只读进度窗口，不会影响原任务。审批和作业操作请返回原作业助手。 ")); const back = node("a", "返回作业助手 ↗"); back.href = data.source_url; back.target = "_blank"; back.rel = "noopener noreferrer"; $("notice").append(back); }
  if (state.pending) { const available = new Set(state.pending.assignments.map(keyOf)); state.selected = new Set([...state.selected].filter(x => available.has(x))); }
  if (!state.focus) state.focus = state.jobs[0]?.id;
  renderCourseFilters(); renderPending(); renderJobs(); if (state.active) startPolling();
}
function startPolling() { if (polling) return; polling = setInterval(pollJob, 1400); }
async function pollJob() {
  if (!state.active) { clearInterval(polling); polling = null; return; }
  if (pollInFlight) return;
  pollInFlight = true;
  try { const job = await api("/api/jobs/" + state.active); connection.lastSuccess = Date.now(); connection.error = null; updateJob(job); renderJobs(); if (!activeStates.has(job.status)) { state.active = null; state.focus = job.id; await loadState(); renderPending(); if (job.kind === "refresh") toast(job.status === "failed" ? job.message : `已刷新 ${state.pending?.assignments.length || 0} 项真实待办`); else toast(job.message); } } catch (error) { connection.error = error.message; renderJobs(); } finally { pollInFlight = false; }
}
async function action(path, body) {
  if (state.monitorOnly) { toast("这是只读进度窗口，请返回原作业助手操作。"); return; }
  if (state.busy) return; state.busy = true; setButtons();
  try { const job = await api(path, "POST", body); updateJob(job); state.active = activeStates.has(job.status) ? job.id : null; state.focus = job.id; renderJobs(); renderPending(); startPolling(); return job; } catch (error) { toast(error.message); } finally { state.busy = false; setButtons(); }
}
async function jobAction(id, verb) { await action(`/api/jobs/${id}/${verb}`, {}); }
function renderEfforts(preferred) { const model = state.models.models.find(x => x.model === $("modelSelect").value); $("effortSelect").replaceChildren(); const labels = {none: "无", minimal: "最少", low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高", ultra: "极高"}; for (const value of model?.supportedReasoningEfforts || []) { const option = node("option", labels[value.reasoningEffort] || value.reasoningEffort); option.value = value.reasoningEffort; $("effortSelect").append(option); } if (!$("effortSelect").children.length) { const option = node("option", "沿用模型默认"); option.value = ""; $("effortSelect").append(option); } const selected = preferred || model?.defaultReasoningEffort; if ([...$("effortSelect").options].some(x => x.value === selected)) $("effortSelect").value = selected; }
async function loadModels() { state.modelsLoading = true; setButtons(); $("modelStatus").textContent = "正在从本机 Codex 读取模型与当前配置…"; try { const catalog = await api("/api/codex/models"); state.models = catalog; $("modelSelect").replaceChildren(); for (const model of catalog.models) { const option = node("option", model.displayName || model.model); option.value = model.model; $("modelSelect").append(option); } const saved = localStorage.getItem("classroom-model"); const selected = catalog.models.some(x => x.model === saved) ? saved : catalog.default_model; if (catalog.models.some(x => x.model === selected)) $("modelSelect").value = selected; renderEfforts(saved === selected ? localStorage.getItem("classroom-effort") : selected === catalog.default_model ? catalog.default_effort : null); $("modelStatus").textContent = "来自本机 Codex 目录与配置；实际模型、会话和生成记录会保存在每份结果中。"; } catch (error) { state.models = null; $("modelStatus").textContent = error.message; toast(error.message); } finally { state.modelsLoading = false; setButtons(); } }
function setView(view) { state.view = view; $("pendingView").hidden = view !== "pending"; $("historyView").hidden = view !== "history"; $("navPending").classList.toggle("active", view === "pending"); $("navHistory").classList.toggle("active", view === "history"); $("breadcrumb").textContent = "工作台 / " + (view === "pending" ? "待完成作业" : "审核记录"); }
function clearImages() { for (const url of state.images) URL.revokeObjectURL(url); state.images = []; }
async function openReview(jobId, key) {
  try {
    const data = await api(`/api/jobs/${jobId}/items/${key}`); state.preview = {jobId, key, data};
    $("reviewTitle").textContent = data.manifest.assignment.title || "资料与结果";
    $("reviewCourse").textContent = data.manifest.course_name || "";
    const g = data.generation;
    $("reviewStatus").textContent = data.policy_changed ? "课程设置已变化，请重新准备" : data.text["draft.md"] && g?.status === "completed" ? `实际初稿已核验 · ${g.model} · ${g.reasoning_effort || "默认强度"} · 尚未提交` : g?.status === "completed" ? `Codex 资料分析已完成 · ${g.model} · 尚无答案初稿` : "资料与待确认包 · 没有已核验的 AI 生成结果";
    $("downloadButton").textContent = "下载辅助证据（可选）";
    const filled = data.document_fill?.documents?.[0];
    const attached = data.requirements.student_submission?.assignmentSubmission?.attachments?.find(x => x.driveFile)?.driveFile;
    const responseForm = data.requirements.response_forms?.[0];
    const original = safeLink(filled?.url || attached?.alternateLink || responseForm?.url, filled ? "打开已填入的文档 ↗" : attached ? "打开原作业文档 ↗" : "打开原作业表单 ↗", "button primary");
    $("originalDocumentAction").replaceChildren(); if (original) $("originalDocumentAction").append(original);
    $("fillDocumentButton").hidden = !data.text["draft.md"] || !!filled || (!!responseForm && !attached);
    if (filled) $("reviewStatus").textContent = `已自动填入并回读核验 · ${g.model} · 尚未提交`;
    else if (responseForm && data.text["draft.md"] && g?.status === "completed") $("reviewStatus").textContent += " · 表单逐题答案在初稿中，需本人确认并填写";
    renderTab("assignment"); $("reviewDialog").showModal();
  } catch (error) { toast(error.message); }
}
function renderTab(tab) {
  clearImages(); for (const b of document.querySelectorAll(".review-tabs button")) { b.classList.toggle("active", b.dataset.tab === tab); b.setAttribute("aria-selected", String(b.dataset.tab === tab)); }
  const content = $("reviewContent"); content.replaceChildren(); const {data, jobId, key} = state.preview;
  if (tab === "assignment") { const assignment = data.manifest.assignment; content.append(node("h3", assignment.title), node("pre", assignment.description || "此作业没有文字说明，请检查附件与课堂链接。")); const link = safeLink(assignment.alternateLink, "打开真实 Classroom 作业 ↗"); if (link) content.append(link); for (const material of assignment.materials || []) { const ref = material.driveFile?.driveFile || material.link || material.youtubeVideo || material.form; if (!ref) continue; const materialLink = safeLink(ref.alternateLink || ref.url || ref.formUrl, ref.title || "查看课程附件"); if (materialLink) { const line = node("p"); line.append(materialLink); content.append(line); } } const original = node("details"); original.append(node("summary", "查看要求的原始来源片段")); for (const source of data.requirements.sources) { const box = node("div", undefined, "source-card"); box.append(node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); original.append(box); } content.append(original); }
  if (tab === "draft") content.append(node("pre", data.text["draft.md"] || "尚未生成答案初稿。需要使用 AI 时，在主界面勾选确认，再开始任务。资料包不能代表作业已经完成。"));
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
  if (tab === "questions") { const questions = data.review?.questions; if (questions?.length) for (const q of questions) content.append(node("p", "• " + q)); else content.append(node("pre", data.text["questions.md"] || "暂无审核问题，请先完成审核。")); }
  if (tab === "checklist") { if (data.review?.requirement_checks) for (const c of data.review.requirement_checks) { const box = node("div", undefined, "source-card"); box.append(node("div", ({met: "已满足", partial: "部分满足", unmet: "未满足", needs_user: "待你确认"}[c.status] || c.status) + " · E:" + c.requirement_source_id, "locator"), node("pre", c.requirement), node("p", c.draft_location || "待补充")); content.append(box); } else content.append(node("pre", data.text["checklist.md"] || "审核尚未完成。")); }
  if (tab === "warnings") { if (!data.manifest.warnings.length) content.append(node("p", "本次没有记录资料缺口。引用和内容仍需你审阅。")); for (const warning of data.manifest.warnings) { const box = node("div", undefined, "source-card"); box.append(node("div", warning.source, "locator"), node("pre", warning.error)); content.append(box); } }
  if (tab === "generation") { const g = data.generation; if (!g || g.status === "not_invoked") { content.append(node("h3", "没有调用 AI"), node("p", "这份包只包含已读取的资料和待确认项，不能当作已完成的作业或 AI 初稿。")); } else { content.append(node("h3", g.status === "completed" ? "真实 Codex 回合已完成" : "Codex 回合尚未成功"), node("p", `实际模型：${g.model || "尚未确认"}\n请求模型：${g.requested_model}\n推理强度：${g.reasoning_effort || "默认"}\n会话 ID：${g.thread_id || "尚未创建"}\n回合 ID：${g.turn_id || "尚未开始"}\n开始：${g.started_at ? dateLabel(g.started_at, true) : "—"}\n结束：${g.finished_at ? dateLabel(g.finished_at, true) : "—"}\n模式：${g.mode === "draft" ? "初稿生成" : "资料与题目分析"}`)); const link = codexLink(g); if (link) content.append(link); if (g.error) content.append(node("p", g.error)); content.append(node("h3", "实际送入模型的来源")); const ids = new Set(g.input?.source_ids || []); for (const source of data.manifest.source_index || []) if (source.evidence_ids.some(id => ids.has(id))) content.append(node("p", `${source.title} · ${source.locators.join("、")}`)); content.append(node("p", `另有 ${g.input?.images?.length || 0} 张实际页图送入模型。${g.input?.omitted_source_count ? "部分较长文本仅保存在可检索的本机证据中。" : ""}`), node("h3", "Codex 生成说明"), node("pre", data.text["codex-summary.md"] || "尚未返回完整说明。")); if (g.token_usage) { const details = node("details"); details.append(node("summary", "查看 Codex 返回的用量记录"), node("pre", JSON.stringify(g.token_usage, null, 2))); content.append(details); } } }
  if (tab === "evidence") for (const source of data.evidence.sources) { const box = node("section", undefined, "source-card"), head = node("header"); head.append(node("span", source.title || source.source_kind)); const link = safeLink(source.url, "查看原来源 ↗"); if (link) head.append(link); box.append(head, node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); if (source.image_path) { const show = button("查看保留的页面图像", "text-button small", async () => { show.disabled = true; try { const blob = await api(`/api/jobs/${jobId}/items/${key}/image/${source.id}`); const url = URL.createObjectURL(blob); state.images.push(url); const image = node("img"); image.src = url; image.alt = source.title + " · " + source.locator; box.append(image); show.remove(); } catch (error) { toast(error.message); show.disabled = false; } }); box.append(show); } content.append(box); }
}
async function openPolicy(courseId, courseName) {
  try { const policy = await api("/api/policies/" + courseId); state.policyCourse = courseId; $("policyTitle").textContent = courseName || "课程 AI 规则"; $("policyUse").value = policy.ai_use; $("policyEvidence").value = policy.policy_evidence; $("policyLimits").value = policy.limitations; $("policyDisclosure").value = policy.disclosure; $("personalFacts").value = policy.personal_facts.join("\n"); $("attachmentBudget").value = policy.max_attachment_bytes / 1048576; $("pdfBudget").value = policy.max_pdf_pages; $("policyError").hidden = true; $("policyDialog").showModal(); } catch (error) { toast(error.message); }
}
$("refreshButton").addEventListener("click", () => action("/api/refresh", {}));
$("generateButton").addEventListener("click", () => { localStorage.setItem("classroom-model", $("modelSelect").value); localStorage.setItem("classroom-effort", $("effortSelect").value); action("/api/review", {selected: [...state.selected].map(key => { const [course_id, assignment_id] = key.split(":"); return {course_id, assignment_id}; }), defer_media: !$("processMedia").checked, ai_confirmed: $("aiConfirmed").checked, ...($("aiConfirmed").checked ? {model: $("modelSelect").value, effort: $("effortSelect").value || null} : {})}); });
$("aiConfirmed").addEventListener("change", () => { setButtons(); renderPending(); });
$("modelSelect").addEventListener("change", () => renderEfforts()); $("reloadModels").addEventListener("click", loadModels);
$("hideOverdue").addEventListener("click", () => { state.hideOverdue = !state.hideOverdue; localStorage.setItem("classroom-hide-overdue", String(state.hideOverdue)); if (state.hideOverdue) { let removed = 0; for (const item of state.pending?.assignments || []) if (overdue(item) && state.selected.delete(keyOf(item))) removed++; if (removed) toast(`已取消 ${removed} 项逾期作业的勾选，避免处理隐藏的作业。`); } renderPending(); });
$("searchInput").addEventListener("input", renderPending); for (const id of ["courseFilter", "dateFilter", "includeNoDue"]) $(id).addEventListener("change", renderPending);
$("selectAll").addEventListener("change", () => { for (const item of filtered()) $("selectAll").checked ? state.selected.add(keyOf(item)) : state.selected.delete(keyOf(item)); renderPending(); });
$("clearSelection").addEventListener("click", () => { state.selected.clear(); renderPending(); });
$("clearFilters").addEventListener("click", () => { $("courseFilter").value = ""; $("dateFilter").value = ""; $("searchInput").value = ""; $("includeNoDue").checked = true; state.hideOverdue = false; localStorage.setItem("classroom-hide-overdue", "false"); renderPending(); });
$("navPending").addEventListener("click", () => setView("pending")); $("navHistory").addEventListener("click", () => setView("history"));
for (const close of document.querySelectorAll(".close-dialog")) close.addEventListener("click", () => close.closest("dialog").close()); $("reviewDialog").addEventListener("close", clearImages);
for (const tab of document.querySelectorAll(".review-tabs button")) tab.addEventListener("click", () => renderTab(tab.dataset.tab));
$("downloadButton").addEventListener("click", async () => { try { const {jobId, key, data} = state.preview; const blob = await api(`/api/jobs/${jobId}/items/${key}/download`); const url = URL.createObjectURL(blob), link = node("a"); link.href = url; link.download = (data.text["draft.md"] ? "Classroom-初稿证据包-" : "Classroom-资料待确认包-") + key.replace(":", "-") + ".zip"; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); } catch (error) { toast(error.message); } });
$("policyForm").addEventListener("submit", async event => { event.preventDefault(); const submit = event.submitter; submit.disabled = true; try { await api("/api/policies/" + state.policyCourse, "PUT", {ai_use: $("policyUse").value, policy_evidence: $("policyEvidence").value, limitations: $("policyLimits").value, disclosure: $("policyDisclosure").value, personal_facts: $("personalFacts").value.split("\n").map(x => x.trim()).filter(Boolean), max_attachment_bytes: Math.round(Number($("attachmentBudget").value) * 1048576), max_pdf_pages: Number($("pdfBudget").value)}); $("policyDialog").close(); await loadState(); toast("已保存课程设置；重新生成所选审核包时会应用这些设置。"); } catch (error) { $("policyError").textContent = error.message; $("policyError").hidden = false; } finally { submit.disabled = false; } });
$("quitButton").addEventListener("click", async () => { try { await api("/api/shutdown", "POST", {}); $("notice").textContent = "助手已退出。下次双击快捷方式即可重新打开，任务记录与缓存会保留。"; $("notice").hidden = false; $("refreshButton").disabled = true; $("generateButton").disabled = true; $("quitButton").disabled = true; } catch (error) { toast(error.message); } });
$("todayLabel").textContent = new Intl.DateTimeFormat("zh-CN", {year: "numeric", month: "long", day: "numeric", weekday: "short"}).format(new Date());
if (!token) { $("notice").textContent = "请双击“Classroom 作业助手”快捷方式打开本机界面。此页面没有访问学校账户的本机凭证。"; $("notice").hidden = false; $("refreshButton").disabled = true; }
else { loadState().catch(error => { $("notice").textContent = error.message; $("notice").hidden = false; toast(error.message); }).then(() => { if (!state.monitorOnly) loadModels(); else $("modelStatus").textContent = "只读进度窗口 · 本次实际模型见任务记录"; }); }

$("authorizeDocuments").addEventListener("click", () => action("/api/documents/authorize", {}));
$("fillDocumentButton").addEventListener("click", () => { const {jobId, key} = state.preview; $("reviewDialog").close(); action(`/api/jobs/${jobId}/items/${key}/fill`, {}); });
