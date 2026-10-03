"use strict";
const $ = id => document.getElementById(id);
const state = {documentsAuthorized: false, pending: null, jobs: [], policies: {}, selected: new Set(), active: null, focus: null, view: "pending", busy: false, preview: null, policyCourse: null, images: [], models: null, modelsLoading: false, hideOverdue: localStorage.getItem("classroom-hide-overdue") === "true"};
const activeStates = new Set(["queued", "running", "stopping"]);
const labels = {queued: "等待开始", running: "正在处理", stopping: "正在暂停", waiting: "等待处理", preparing: "读取题目与资料", prepared: "资料已准备", drafting: "Codex 正在分析", ready: "初稿已生成 · 尚未填入", filling_document: "正在填入原文档", document_ready: "已填入 · 打开文档审阅", document_needs_user: "已填入可用答案 · 尚有空栏待确认", needs_document: "初稿已保留 · 填入需处理", needs_user: "需要补充", materials_ready: "资料已整理 · 未调用 AI", paused: "已暂停", interrupted: "上次已中断", failed: "处理失败", completed: "处理已结束", completed_with_issues: "部分处理未完成"};
const keyOf = item => item.course_id + ":" + item.assignment_id;
let token = location.hash.slice(1);
if (token) { sessionStorage.setItem("classroom-access", token); history.replaceState(null, "", location.pathname); }
else token = sessionStorage.getItem("classroom-access") || "";
let polling = null, toastTimer = null;

function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
function icon(name) { const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true"); const use = document.createElementNS(svg.namespaceURI, "use"); use.setAttribute("href", "#i-" + name); svg.append(use); return svg; }
function button(text, className, handler) { const el = node("button", text, className); el.type = "button"; el.addEventListener("click", handler); return el; }
function friendlyError(message) { return ({"A known AI policy requires the user's source evidence.": "请填写教师实际 AI 规定的原文与来源，才能确认允许或禁止使用。", "Limited AI use requires explicit limits.": "选择有限度允许时，请填写允许的具体用途和限制。", "Invalid attachment/page processing budget.": "附件上限需在 1 字节至 20GB 内；PDF 页数上限需在 1–5000 页内。", "Selected assignment is no longer confirmed pending; refresh the list before retrying.": "有选中作业的提交状态已变化，请刷新待办并重新勾选。", "Another local run is active; wait for it to finish.": "另一个本机处理正在运行，请等它结束后重试。", "Course policy changed since preparation; prepare the package again.": "课程规则已变化，请重新生成审核包。"})[message] || message; }
function toast(message) { clearTimeout(toastTimer); $("toast").textContent = message; $("toast").hidden = false; toastTimer = setTimeout(() => $("toast").hidden = true, 7000); }
async function api(path, method = "GET", body) {
  const options = {method, cache: "no-store", credentials: "omit", headers: {Authorization: "Bearer " + token}};
  if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
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
  const busy = !!state.active || state.busy; $("authorizeDocuments").disabled = busy; $("authorizeDocuments").hidden = state.documentsAuthorized; $("documentPermissionNote").textContent = state.documentsAuthorized ? "自动填入已授权：答案写入个人副本，你打开原文档审阅并手动提交。" : "首次填入需 Google Docs 读写授权。Google 授权范围涵盖可编辑文档；本程序只填入本人待完成作业副本。";
  $("refreshButton").disabled = busy; $("refreshButton").classList.toggle("spinner", !!state.active && state.jobs.find(x => x.id === state.active)?.kind === "refresh");
  $("generateButton").disabled = busy || !state.selected.size || ($("aiConfirmed").checked && (!state.models || state.modelsLoading));
  $("aiConfirmed").disabled = busy;
  $("generateButton").querySelector("span").textContent = $("aiConfirmed").checked ? "自动完成并填入" : "准备所选资料";
  $("aiChoiceNote").textContent = $("aiConfirmed").checked ? "已勾选：使用所选模型作答并填入个人 Google 文档；你打开原文档审阅后手动提交。" : "未勾选：只读取和整理资料，不调用 AI。无需填写规则原文。";
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
  $("readyCount").textContent = [...latest.values()].filter(x => ["document_ready", "document_needs_user"].includes(x.item.status)).length;
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
function jobCard(job, full = false) {
  const card = node("section", undefined, full ? "history-card" : ""); const heading = node("div", undefined, "job-heading"), intro = node("div");
  intro.append(node("h3", job.kind === "refresh" ? (activeStates.has(job.status) ? "正在同步真实待办" : "刷新待办 · " + labels[job.status]) : `所选 ${job.items.length} 项作业 · ${labels[job.status] || job.status}`), node("p", friendlyError(job.message)));
  if (full) intro.append(node("span", dateLabel(job.created_at, true), "muted small")); heading.append(intro);
  if (activeStates.has(job.status)) { const pause = button(job.status === "stopping" ? "正在暂停…" : "暂停", "button secondary small-button", () => jobAction(job.id, "pause")); pause.disabled = job.status === "stopping"; heading.append(pause); }
  else if (job.kind === "review" || job.status !== "completed") heading.append(button("重试 / 继续", "button secondary small-button", () => jobAction(job.id, "retry")));
  card.append(heading);
  if (activeStates.has(job.status)) { const progress = node("progress", undefined, "job-progress"); progress.max = Math.max(1, job.items.length * 2); const amount = job.items.reduce((n,x) => n + (x.package ? 1 : 0) + (["ready", "needs_user", "failed"].includes(x.status) ? 1 : 0), 0); if (amount) progress.value = amount; progress.setAttribute("aria-label", "审核进度"); card.append(progress); }
  const items = node("div", undefined, "job-items");
  for (const item of job.items) { const row = node("div", undefined, "job-item"); const title = node("span", item.title || item.assignment_id); if (item.error) title.append(node("p", friendlyError(item.error))); row.append(title, pill(item.status)); if (item.package) row.append(button(item.status === "ready" ? "查看实际初稿" : "查看资料与分析", "text-button", () => openReview(job.id, keyOf(item)))); const g = item.generation; if (g?.thread_id) { const meta = node("div", undefined, "job-model"); meta.append(node("span", `实际模型 ${g.model} · ${g.reasoning_effort || "默认强度"} · ${g.status === "completed" ? "AI 回合完成" : g.status === "running" ? "正在生成" : "尚未成功"} `)); const link = codexLink(g); if (link) meta.append(link); row.append(meta); } else if (item.package) row.append(node("span", "资料已准备 · 没有已核验的 AI 生成记录", "job-model")); for (const d of item.document_fill?.documents || []) { const link = safeLink(d.url, "在原文档审阅 ↗", "button primary small-button"); if (link) row.prepend(link); } if (item.package && g?.status === "completed" && !["document_ready", "document_needs_user"].includes(item.status)) row.append(button("填入已有初稿", "text-button", () => action(`/api/jobs/${job.id}/items/${keyOf(item)}/fill`, {}))); items.append(row); } card.append(items);
  if (job.approval) { const box = node("section", undefined, "approval-card"); box.append(node("h3", "Codex 请求本次操作审批"), node("p", job.approval.reason), node("pre", job.approval.command || job.approval.cwd), node("p", "此请求来自 Codex 的现有权限规则；批准仅适用于本次，不会扩大长期权限。")); const actions = node("div", undefined, "approval-actions"); for (const [choice, label] of [["accept", "批准本次"], ["decline", "拒绝"], ["cancel", "取消操作"]]) actions.append(button(label, "button secondary small-button", async () => { try { await api(`/api/jobs/${job.id}/approval`, "POST", {id: job.approval.id, decision: choice}); await pollJob(); } catch(error) { toast(error.message); } })); box.append(actions); card.append(box); }
  if (job.cache) card.append(node("p", `复用 ${job.cache.reused} 个处理阶段 · 新增处理 ${job.cache.processed} · 失败 ${job.cache.failed}`, "small"));
  if (job.events.length) { const details = node("details"), summary = node("summary", "查看处理记录"), events = node("div", undefined, "event-list"); for (const entry of job.events) { const line = node("div", undefined, "event-line"); line.append(node("time", new Date(entry.at).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit"})), node("span", entry.message)); events.append(line); } details.append(summary, events); card.append(details); }
  return card;
}
function renderJobs() {
  const focus = state.jobs.find(x => x.id === (state.active || state.focus)); $("jobPanel").hidden = !focus || (focus.kind === "refresh" && ["completed", "completed_with_issues"].includes(focus.status)); $("jobPanel").replaceChildren(); if (!$("jobPanel").hidden) $("jobPanel").append(jobCard(focus));
  $("historyList").replaceChildren(); if (!state.jobs.length) $("historyList").append(node("div", "还没有审核记录。刷新待办并勾选作业后，这里会保留处理进度与结果。", "empty-state muted small"));
  for (const job of [...state.jobs].sort((a,b) => b.created_at.localeCompare(a.created_at))) $("historyList").append(jobCard(job, true)); setButtons();
}
async function loadState() {
  const data = await api("/api/bootstrap"); state.documentsAuthorized = !!data.documents_authorized; state.pending = data.pending; state.jobs = data.jobs; state.policies = data.policies; state.active = data.active_job_id; state.timezone = data.timezone; $("accountEmail").textContent = data.account;
  if (state.pending) { const available = new Set(state.pending.assignments.map(keyOf)); state.selected = new Set([...state.selected].filter(x => available.has(x))); }
  if (!state.focus) state.focus = state.jobs[0]?.id;
  renderCourseFilters(); renderPending(); renderJobs(); if (state.active) startPolling();
}
function startPolling() { if (polling) return; polling = setInterval(pollJob, 1400); }
async function pollJob() {
  if (!state.active) { clearInterval(polling); polling = null; return; }
  try { const job = await api("/api/jobs/" + state.active); updateJob(job); renderJobs(); if (!activeStates.has(job.status)) { state.active = null; state.focus = job.id; await loadState(); renderPending(); if (job.kind === "refresh") toast(job.status === "failed" ? job.message : `已刷新 ${state.pending?.assignments.length || 0} 项真实待办`); else toast(job.message); } } catch (error) { toast(error.message); }
}
async function action(path, body) {
  if (state.busy) return; state.busy = true; setButtons();
  try { const job = await api(path, "POST", body); updateJob(job); state.active = activeStates.has(job.status) ? job.id : null; state.focus = job.id; renderJobs(); renderPending(); startPolling(); return job; } catch (error) { toast(error.message); } finally { state.busy = false; setButtons(); }
}
async function jobAction(id, verb) { await action(`/api/jobs/${id}/${verb}`, {}); }
function renderEfforts(preferred) { const model = state.models.models.find(x => x.model === $("modelSelect").value); $("effortSelect").replaceChildren(); const labels = {none: "无", minimal: "最少", low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高", ultra: "极高"}; for (const value of model?.supportedReasoningEfforts || []) { const option = node("option", labels[value.reasoningEffort] || value.reasoningEffort); option.value = value.reasoningEffort; $("effortSelect").append(option); } if (!$("effortSelect").children.length) { const option = node("option", "沿用模型默认"); option.value = ""; $("effortSelect").append(option); } const selected = preferred || model?.defaultReasoningEffort; if ([...$("effortSelect").options].some(x => x.value === selected)) $("effortSelect").value = selected; }
async function loadModels() { state.modelsLoading = true; setButtons(); $("modelStatus").textContent = "正在从本机 Codex 读取模型与当前配置…"; try { const catalog = await api("/api/codex/models"); state.models = catalog; $("modelSelect").replaceChildren(); for (const model of catalog.models) { const option = node("option", model.displayName || model.model); option.value = model.model; $("modelSelect").append(option); } const saved = localStorage.getItem("classroom-model"); const selected = catalog.models.some(x => x.model === saved) ? saved : catalog.default_model; if (catalog.models.some(x => x.model === selected)) $("modelSelect").value = selected; renderEfforts(saved === selected ? localStorage.getItem("classroom-effort") : selected === catalog.default_model ? catalog.default_effort : null); $("modelStatus").textContent = "来自本机 Codex 目录与配置；实际模型、会话和生成记录会保存在每份结果中。"; } catch (error) { state.models = null; $("modelStatus").textContent = error.message; toast(error.message); } finally { state.modelsLoading = false; setButtons(); } }
function setView(view) { state.view = view; $("pendingView").hidden = view !== "pending"; $("historyView").hidden = view !== "history"; $("navPending").classList.toggle("active", view === "pending"); $("navHistory").classList.toggle("active", view === "history"); $("breadcrumb").textContent = "工作台 / " + (view === "pending" ? "待完成作业" : "审核记录"); }
function clearImages() { for (const url of state.images) URL.revokeObjectURL(url); state.images = []; }
async function openReview(jobId, key) {
  try { const data = await api(`/api/jobs/${jobId}/items/${key}`); state.preview = {jobId, key, data}; $("reviewTitle").textContent = data.manifest.assignment.title || "资料与结果"; $("reviewCourse").textContent = data.manifest.course_name || ""; const g = data.generation; $("reviewStatus").textContent = data.policy_changed ? "课程设置已变化，请重新准备" : data.text["draft.md"] && g?.status === "completed" ? `实际初稿已核验 · ${g.model} · ${g.reasoning_effort || "默认强度"} · 尚未提交` : g?.status === "completed" ? `Codex 资料分析已完成 · ${g.model} · 尚无答案初稿` : "资料与待确认包 · 没有已核验的 AI 生成结果"; $("downloadButton").textContent = "下载辅助证据（可选）"; const filled = data.document_fill?.documents?.[0]; const attached = data.requirements.student_submission?.assignmentSubmission?.attachments?.find(x => x.driveFile)?.driveFile; const original = safeLink(filled?.url || attached?.alternateLink, filled ? "打开已填入的文档 ↗" : "打开原作业文档 ↗", "button primary"); $("originalDocumentAction").replaceChildren(); if (original) $("originalDocumentAction").append(original); $("fillDocumentButton").hidden = !data.text["draft.md"] || !!filled; if (filled) $("reviewStatus").textContent = `已自动填入并回读核验 · ${g.model} · 尚未提交`; renderTab("assignment"); $("reviewDialog").showModal(); } catch (error) { toast(error.message); }
}
function renderTab(tab) {
  clearImages(); for (const b of document.querySelectorAll(".review-tabs button")) { b.classList.toggle("active", b.dataset.tab === tab); b.setAttribute("aria-selected", String(b.dataset.tab === tab)); }
  const content = $("reviewContent"); content.replaceChildren(); const {data, jobId, key} = state.preview;
  if (tab === "assignment") { const assignment = data.manifest.assignment; content.append(node("h3", assignment.title), node("pre", assignment.description || "此作业没有文字说明，请检查附件与课堂链接。")); const link = safeLink(assignment.alternateLink, "打开真实 Classroom 作业 ↗"); if (link) content.append(link); for (const material of assignment.materials || []) { const ref = material.driveFile?.driveFile || material.link || material.youtubeVideo || material.form; if (!ref) continue; const materialLink = safeLink(ref.alternateLink || ref.url || ref.formUrl, ref.title || "查看课程附件"); if (materialLink) { const line = node("p"); line.append(materialLink); content.append(line); } } const original = node("details"); original.append(node("summary", "查看要求的原始来源片段")); for (const source of data.requirements.sources) { const box = node("div", undefined, "source-card"); box.append(node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); original.append(box); } content.append(original); }
  if (tab === "draft") content.append(node("pre", data.text["draft.md"] || "尚未生成答案初稿。需要使用 AI 时，在主界面勾选确认，再开始任务。资料包不能代表作业已经完成。"));
  if (tab === "assignment") { const submission = data.requirements.student_submission; if (submission) { const box = node("section", undefined, "source-card"); box.append(node("h3", "你的个人作业文档与具体题目")); for (const attachment of submission.assignmentSubmission?.attachments || []) { const file = attachment.driveFile; if (!file) continue; const link = safeLink(file.alternateLink, "打开个人作业文档 ↗"); if (link) box.append(link); for (const source of data.requirements.sources.filter(x => x.source_id === "file:" + file.id)) box.append(node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); } content.prepend(box); } const index = data.manifest.source_index || []; const materials = node("section", undefined, "source-card"); materials.append(node("h3", "作业附件与同课程授课资料")); for (const source of index.filter(x => x.source_id.startsWith("file:"))) { const sent = source.evidence_ids.some(id => data.generation?.input?.source_ids.includes(id)); const line = node("p", `${source.title} · ${source.locators.length} 个片段 · ${sent ? "已送入本次 AI" : "已读取，保存在本机证据"} `); const link = safeLink(source.url, "原文件 ↗"); if (link) line.append(link); for (const parent of source.parents || []) { const parentLink = safeLink(parent.url, "课堂来源 ↗"); if (parentLink) line.append(node("span", " · "), parentLink); } materials.append(line); } if (index.length) content.append(materials); }
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
else { loadState().catch(error => { $("notice").textContent = error.message; $("notice").hidden = false; toast(error.message); }); loadModels(); }

$("authorizeDocuments").addEventListener("click", () => action("/api/documents/authorize", {}));
$("fillDocumentButton").addEventListener("click", () => { const {jobId, key} = state.preview; $("reviewDialog").close(); action(`/api/jobs/${jobId}/items/${key}/fill`, {}); });
