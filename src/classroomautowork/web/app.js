"use strict";
const $ = id => document.getElementById(id);
const state = {pending: null, jobs: [], policies: {}, selected: new Set(), active: null, focus: null, view: "pending", busy: false, preview: null, policyCourse: null, images: []};
const activeStates = new Set(["queued", "running", "stopping"]);
const labels = {queued: "等待开始", running: "正在处理", stopping: "正在暂停", waiting: "等待处理", preparing: "准备资料", prepared: "资料已准备", drafting: "生成审核包", ready: "初稿待审阅", needs_user: "待你确认", paused: "已暂停", interrupted: "上次已中断", failed: "处理失败", completed: "审核已结束", completed_with_issues: "部分处理未完成"};
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
function latestItems() { const found = new Map(); for (const job of [...state.jobs].sort((a,b) => b.created_at.localeCompare(a.created_at))) if (job.kind === "review") for (const item of job.items) if (!found.has(keyOf(item))) found.set(keyOf(item), {job, item}); return found; }
function filtered() {
  const normalize = value => value.normalize("NFKC").replace(/\s+/g, " ").trim().toLocaleLowerCase();
  const q = normalize($("searchInput").value), course = $("courseFilter").value, date = $("dateFilter").value;
  return (state.pending?.assignments || []).filter(item => (!course || item.course_id === course) && (!q || normalize((item.title || "") + " " + (item.course_name || "")).includes(q)) && (item.due_local ? (!date || localDay(item.due_local) <= date) : $("includeNoDue").checked));
}
function setButtons() {
  const busy = !!state.active || state.busy;
  $("refreshButton").disabled = busy; $("refreshButton").classList.toggle("spinner", !!state.active && state.jobs.find(x => x.id === state.active)?.kind === "refresh");
  $("generateButton").disabled = busy || !state.selected.size;
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
  $("readyCount").textContent = [...latest.values()].filter(x => x.item.status === "ready").length;
  $("visibleCount").textContent = items.length;
  $("refreshTime").textContent = pending ? `上次实时刷新 ${dateLabel(pending.refreshed_at, true)} · 列表保留到下次刷新` : "点击刷新，从 Classroom 读取实时待办";
  $("clearFilters").hidden = !($("courseFilter").value || $("searchInput").value || $("dateFilter").value || !$("includeNoDue").checked);
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
    else side.append(node("span", policy?.can_draft ? "可准备初稿" : "AI 规则待确认", "pill " + (policy?.can_draft ? "success" : "warning")));
    const actions = node("div", undefined, "row-actions"); const policyButton = button("课程规则", "row-action", () => openPolicy(item.course_id, item.course_name)); policyButton.disabled = !!state.active; actions.append(policyButton);
    if (completed?.item.package) actions.append(button("查看审核包", "row-action", () => openReview(completed.job.id, key)));
    side.append(actions); row.append(label, side); list.append(row);
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
  for (const item of job.items) { const row = node("div", undefined, "job-item"); const title = node("span", item.title || item.assignment_id); if (item.error) title.append(node("p", friendlyError(item.error))); row.append(title, pill(item.status)); if (item.package) row.append(button("查看审核包", "text-button", () => openReview(job.id, keyOf(item)))); items.append(row); } card.append(items);
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
  const data = await api("/api/bootstrap"); state.pending = data.pending; state.jobs = data.jobs; state.policies = data.policies; state.active = data.active_job_id; state.timezone = data.timezone; $("accountEmail").textContent = data.account;
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
function setView(view) { state.view = view; $("pendingView").hidden = view !== "pending"; $("historyView").hidden = view !== "history"; $("navPending").classList.toggle("active", view === "pending"); $("navHistory").classList.toggle("active", view === "history"); $("breadcrumb").textContent = "工作台 / " + (view === "pending" ? "待完成作业" : "审核记录"); }
function clearImages() { for (const url of state.images) URL.revokeObjectURL(url); state.images = []; }
async function openReview(jobId, key) {
  try { const data = await api(`/api/jobs/${jobId}/items/${key}`); state.preview = {jobId, key, data}; $("reviewTitle").textContent = data.manifest.assignment.title || "审核包"; $("reviewCourse").textContent = data.manifest.course_name || ""; $("reviewStatus").textContent = data.policy_changed ? "课程规则已变化，请重试重新准备；旧初稿不再作为当前结果显示" : data.receipt?.status === "ready_for_human_review" ? "初稿已核验，等待你审阅 · 尚未提交" : "尚无可提交初稿，先查看待确认问题"; renderTab("assignment"); $("reviewDialog").showModal(); } catch (error) { toast(error.message); }
}
function renderTab(tab) {
  clearImages(); for (const b of document.querySelectorAll(".review-tabs button")) { b.classList.toggle("active", b.dataset.tab === tab); b.setAttribute("aria-selected", String(b.dataset.tab === tab)); }
  const content = $("reviewContent"); content.replaceChildren(); const {data, jobId, key} = state.preview;
  if (tab === "assignment") { const assignment = data.manifest.assignment; content.append(node("h3", assignment.title), node("pre", assignment.description || "此作业没有文字说明，请检查附件与课堂链接。")); const link = safeLink(assignment.alternateLink, "打开真实 Classroom 作业 ↗"); if (link) content.append(link); for (const material of assignment.materials || []) { const ref = material.driveFile?.driveFile || material.link || material.youtubeVideo || material.form; if (!ref) continue; const materialLink = safeLink(ref.alternateLink || ref.url || ref.formUrl, ref.title || "查看课程附件"); if (materialLink) { const line = node("p"); line.append(materialLink); content.append(line); } } const original = node("details"); original.append(node("summary", "查看要求的原始来源片段")); for (const source of data.requirements.sources) { const box = node("div", undefined, "source-card"); box.append(node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); original.append(box); } content.append(original); }
  if (tab === "draft") content.append(node("pre", data.text["draft.md"] || "尚未生成答案初稿。请先查看“待确认”和课程 AI 规则；资料准备不能代表作业已经完成。"));
  if (tab === "questions") { const questions = data.review?.questions; if (questions?.length) for (const q of questions) content.append(node("p", "• " + q)); else content.append(node("pre", data.text["questions.md"] || "暂无审核问题，请先完成审核。")); }
  if (tab === "checklist") { if (data.review?.requirement_checks) for (const c of data.review.requirement_checks) { const box = node("div", undefined, "source-card"); box.append(node("div", ({met: "已满足", partial: "部分满足", unmet: "未满足", needs_user: "待你确认"}[c.status] || c.status) + " · E:" + c.requirement_source_id, "locator"), node("pre", c.requirement), node("p", c.draft_location || "待补充")); content.append(box); } else content.append(node("pre", data.text["checklist.md"] || "审核尚未完成。")); }
  if (tab === "warnings") { if (!data.manifest.warnings.length) content.append(node("p", "本次没有记录资料缺口。引用和内容仍需你审阅。")); for (const warning of data.manifest.warnings) { const box = node("div", undefined, "source-card"); box.append(node("div", warning.source, "locator"), node("pre", warning.error)); content.append(box); } }
  if (tab === "evidence") for (const source of data.evidence.sources) { const box = node("section", undefined, "source-card"), head = node("header"); head.append(node("span", source.title || source.source_kind)); const link = safeLink(source.url, "查看原来源 ↗"); if (link) head.append(link); box.append(head, node("div", source.locator + " · E:" + source.id, "locator"), node("pre", source.text)); if (source.image_path) { const show = button("查看保留的页面图像", "text-button small", async () => { show.disabled = true; try { const blob = await api(`/api/jobs/${jobId}/items/${key}/image/${source.id}`); const url = URL.createObjectURL(blob); state.images.push(url); const image = node("img"); image.src = url; image.alt = source.title + " · " + source.locator; box.append(image); show.remove(); } catch (error) { toast(error.message); show.disabled = false; } }); box.append(show); } content.append(box); }
}
async function openPolicy(courseId, courseName) {
  try { const policy = await api("/api/policies/" + courseId); state.policyCourse = courseId; $("policyTitle").textContent = courseName || "课程 AI 规则"; $("policyUse").value = policy.ai_use; $("policyEvidence").value = policy.policy_evidence; $("policyLimits").value = policy.limitations; $("policyDisclosure").value = policy.disclosure; $("personalFacts").value = policy.personal_facts.join("\n"); $("attachmentBudget").value = policy.max_attachment_bytes / 1048576; $("pdfBudget").value = policy.max_pdf_pages; $("policyError").hidden = true; $("policyDialog").showModal(); } catch (error) { toast(error.message); }
}
$("refreshButton").addEventListener("click", () => action("/api/refresh", {}));
$("generateButton").addEventListener("click", () => action("/api/review", {selected: [...state.selected].map(key => { const [course_id, assignment_id] = key.split(":"); return {course_id, assignment_id}; }), defer_media: !$("processMedia").checked}));
$("searchInput").addEventListener("input", renderPending); for (const id of ["courseFilter", "dateFilter", "includeNoDue"]) $(id).addEventListener("change", renderPending);
$("selectAll").addEventListener("change", () => { for (const item of filtered()) $("selectAll").checked ? state.selected.add(keyOf(item)) : state.selected.delete(keyOf(item)); renderPending(); });
$("clearSelection").addEventListener("click", () => { state.selected.clear(); renderPending(); });
$("clearFilters").addEventListener("click", () => { $("courseFilter").value = ""; $("dateFilter").value = ""; $("searchInput").value = ""; $("includeNoDue").checked = true; renderPending(); });
$("navPending").addEventListener("click", () => setView("pending")); $("navHistory").addEventListener("click", () => setView("history"));
for (const close of document.querySelectorAll(".close-dialog")) close.addEventListener("click", () => close.closest("dialog").close()); $("reviewDialog").addEventListener("close", clearImages);
for (const tab of document.querySelectorAll(".review-tabs button")) tab.addEventListener("click", () => renderTab(tab.dataset.tab));
$("downloadButton").addEventListener("click", async () => { try { const {jobId, key} = state.preview; const blob = await api(`/api/jobs/${jobId}/items/${key}/download`); const url = URL.createObjectURL(blob), link = node("a"); link.href = url; link.download = "Classroom-审核包-" + key.replace(":", "-") + ".zip"; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); } catch (error) { toast(error.message); } });
$("policyForm").addEventListener("submit", async event => { event.preventDefault(); const submit = event.submitter; submit.disabled = true; try { await api("/api/policies/" + state.policyCourse, "PUT", {ai_use: $("policyUse").value, policy_evidence: $("policyEvidence").value, limitations: $("policyLimits").value, disclosure: $("policyDisclosure").value, personal_facts: $("personalFacts").value.split("\n").map(x => x.trim()).filter(Boolean), max_attachment_bytes: Math.round(Number($("attachmentBudget").value) * 1048576), max_pdf_pages: Number($("pdfBudget").value)}); $("policyDialog").close(); await loadState(); toast("已保存课程设置；重新生成所选审核包时会应用这些设置。"); } catch (error) { $("policyError").textContent = error.message; $("policyError").hidden = false; } finally { submit.disabled = false; } });
$("quitButton").addEventListener("click", async () => { try { await api("/api/shutdown", "POST", {}); $("notice").textContent = "助手已退出。下次双击快捷方式即可重新打开，任务记录与缓存会保留。"; $("notice").hidden = false; $("refreshButton").disabled = true; $("generateButton").disabled = true; $("quitButton").disabled = true; } catch (error) { toast(error.message); } });
$("todayLabel").textContent = new Intl.DateTimeFormat("zh-CN", {year: "numeric", month: "long", day: "numeric", weekday: "short"}).format(new Date());
if (!token) { $("notice").textContent = "请双击“Classroom 作业助手”快捷方式打开本机界面。此页面没有访问学校账户的本机凭证。"; $("notice").hidden = false; $("refreshButton").disabled = true; }
else loadState().catch(error => { $("notice").textContent = error.message; $("notice").hidden = false; toast(error.message); });
