"use strict";
const $ = id => document.getElementById(id);
// On yishuapp.com the page lives under /classroom/ and Cloudflare Access authenticates requests.
const BASE = location.pathname.startsWith("/classroom/") ? "/classroom" : "";
const activeStates = new Set(["queued", "running", "stopping"]);
const keyOf = item => item.course_id + ":" + item.assignment_id;
const labels = {waiting: "等待中", preparing: "读取资料", prepared: "资料就绪", drafting: "作答中", filling_form: "准备表单", filling_document: "填写文档",
  ready: "答案", form_opened: "答案", document_ready: "答案", document_needs_user: "答案", materials_ready: "资料已整理", needs_user: "待补充",
  needs_form: "待补充", needs_document: "待补充", failed: "失败", paused: "已暂停", interrupted: "已中断", queued: "等待开始", running: "处理中",
  stopping: "正在暂停", completed: "已完成", completed_with_issues: "部分未完成"};
const kindLabels = {refresh: "刷新待办", review: "作答", fill: "填写"};
const efforts = {low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高"};
const FILTERS = [["all", "全部"], ["week", "7 天内"], ["nodue", "无截止"], ["review", "待审阅"], ["overdue", "已逾期"]];
const STALE_MS = 3 * 60 * 1000;

let token = location.hash.slice(1);
if (token) { sessionStorage.setItem("classroom-access", token); history.replaceState(null, "", location.pathname); }
else token = sessionStorage.getItem("classroom-access") || "";

const state = {pending: null, jobs: [], resultJobs: [], active: null, focus: null, dismissed: localStorage.getItem("classroom-dismissed") || "",
  selected: new Set(), filter: localStorage.getItem("classroom-filter") || "all", busy: false, models: null, apiKey: null, timezone: "Asia/Tokyo",
  panel: null, supplement: null, images: [], error: null};

// Pure presentation; exercised by tests/progress_view.js.
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
function formGenerationIncomplete(item) {
  const mapping = Object.hasOwn(item?.form_fill || {}, "form_mapping_complete") ? item.form_fill.form_mapping_complete : item?.generation?.form_mapping_complete;
  return !!((item?.form_requires_regeneration && item?.form_fill?.form_mapping_complete !== true) || mapping === false || formContentGaps(item?.form_fill).length);
}
function answersIncomplete(item) {
  return documentAnswersIncomplete(item) || formGenerationIncomplete(item) || !!formUserInputGaps(item?.form_fill).length;
}
function hasAnswer(item) {
  return !!(item?.answer_document?.files?.["answer.docx"] || item?.document_fill?.documents?.length || item?.form_fill?.forms?.length);
}
function delivered(item) {
  return !!item && !answersIncomplete(item) && hasAnswer(item) && ["ready", "document_ready", "document_needs_user", "form_opened"].includes(item.status);
}
function dueInfo(item, now) {
  if (!item.due_utc && !item.due_local) return {days: null, overdue: false, text: "无截止"};
  const due = Date.parse(item.due_utc || item.due_local), days = Math.floor((due - now) / 86400000);
  if (due < now) return {days, overdue: true, text: "已逾期"};
  return {days, overdue: false, text: days < 1 ? "今天" : `${days} 天后`};
}
function filterMatch(filter, item, record, now) {
  const due = dueInfo(item, now);
  return filter === "week" ? !due.overdue && due.days !== null && due.days < 7
    : filter === "nodue" ? due.days === null
    : filter === "review" ? hasAnswer(record?.item)
    : filter === "overdue" ? due.overdue
    : true;
}
// A checked assignment hidden by a filter is never processed; delivered answers are skipped.
function selectTargets(visible, selected, latest) {
  return visible.filter(item => !delivered(latest.get(keyOf(item))?.item) && (!selected.size || selected.has(keyOf(item))));
}
function rowStatus(record) {
  const item = record?.item;
  if (!item) return {text: "未开始", tone: "muted", open: false};
  if (activeStates.has(record.job.status) && ["waiting", "preparing", "prepared", "drafting", "filling_form", "filling_document"].includes(item.status))
    return {text: labels[item.status], tone: item.status === "waiting" ? "muted" : "running", open: false};
  if (delivered(item)) return {text: "答案 ›", tone: "good", open: true};
  if (hasAnswer(item)) return {text: "待补充 ›", tone: "warn", open: true};
  const text = labels[item.status] || item.status;
  if (["failed", "paused", "interrupted", "needs_user", "needs_form", "needs_document"].includes(item.status))
    return {text: item.package ? text + " ›" : text, tone: item.status === "failed" ? "bad" : "warn", open: !!item.package};
  return {text: item.package ? text + " ›" : text, tone: "muted", open: !!item.package};
}
// Steps of the item being worked on: read material, answer, check, deliver.
function stepIndex(job, item) {
  const stage = job.progress?.stage;
  if (stage === "validate") return 2;
  if (["form_fill", "fill", "document_inspect"].includes(stage) || ["filling_form", "filling_document"].includes(item?.status)) return 3;
  if (stage === "ai" || item?.status === "drafting") return 1;
  return 0;
}
function lastActivity(job, item) {
  const times = [item?.generation?.activity_at, job.progress?.activity_at, job.events?.at(-1)?.at, job.updated_at, job.started_at].map(x => Date.parse(x || "")).filter(Number.isFinite);
  return times.length ? Math.max(...times) : null;
}
function jobSummary(job, now) {
  const items = job.items || [], total = items.length;
  const done = items.filter(x => !["waiting", "preparing", "prepared", "drafting", "filling_form", "filling_document"].includes(x.status)).length;
  const current = items.find(x => ["preparing", "prepared", "drafting", "filling_form", "filling_document"].includes(x.status)) || null;
  const active = activeStates.has(job.status);
  const estimate = job.estimate?.overall;
  const percent = active && job.estimate?.state === "estimating" && Number.isFinite(estimate?.percent) ? Math.min(99, Math.max(0, estimate.percent))
    : total > 1 && done ? 100 * done / total : null;
  const remaining = active && Number.isFinite(estimate?.remaining_seconds) && job.estimate?.state === "estimating" ? estimate.remaining_seconds : null;
  const activity = lastActivity(job, current), started = Date.parse(job.started_at || job.created_at || "");
  let warning = null;
  if (active && job.runtime && job.runtime.worker_alive === false) warning = "后台进程不在运行，这个任务不会继续。可以刷新页面后重试。";
  else if (active && job.status !== "stopping" && activity && now - activity > STALE_MS) warning = `已有 ${Math.round((now - activity) / 60000)} 分钟没有新进展，可能卡住了；可以暂停后重试。`;
  return {total, done, current, active, percent, remaining, warning, step: stepIndex(job, current),
    elapsed: Number.isFinite(started) ? Math.max(0, (Date.parse(job.finished_at || "") || now) - started) : null};
}
function durationLabel(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`;
}
// End pure presentation.

function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined && text !== null) el.textContent = text; if (className) el.className = className; return el; }
function button(text, className, handler) { const b = node("button", text, className); b.type = "button"; b.addEventListener("click", handler); return b; }
function safeLink(url, text, className) {
  try {
    const u = new URL(url);
    if (u.protocol !== "https:" || !(u.hostname.endsWith(".google.com") || ["google.com", "youtube.com", "www.youtube.com", "youtu.be"].includes(u.hostname))) return null;
    const a = node("a", text, className); a.href = u.href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
  } catch { return null; }
}
let toastTimer = null;
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000); }
function dateLabel(value, time = false) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {timeZone: state.timezone, month: "2-digit", day: "2-digit", ...(time ? {hour: "2-digit", minute: "2-digit", hour12: false} : {})}).format(new Date(value));
}
function friendlyError(message) {
  return ({"Selected assignment is no longer confirmed pending; refresh the list before retrying.": "有选中作业的提交状态已变化，请刷新后重新勾选。",
    "Another local run is active; wait for it to finish.": "另一个处理正在运行，请等它结束后重试。",
    "A known AI policy requires the user's source evidence.": "选择允许或禁止时，请填写规定原文与来源。",
    "Limited AI use requires explicit limits.": "选择有限度允许时，请填写允许的具体用途。"})[message] || message;
}
async function api(path, method = "GET", body) {
  const options = {method, cache: "no-store", credentials: BASE ? "same-origin" : "omit", headers: token ? {Authorization: "Bearer " + token} : {}};
  if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), method === "GET" ? 20000 : 60000);
  options.signal = controller.signal;
  let response;
  try { response = await fetch(BASE + path, options); }
  catch (error) { throw new Error(error.name === "AbortError" ? "连接超时，稍后会自动重试。" : "连不上作业助手，请检查网络。"); }
  finally { clearTimeout(timer); }
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(friendlyError(data.error || `请求失败（${response.status}）`)); }
  return (response.headers.get("Content-Type") || "").includes("application/json") ? response.json() : response.blob();
}
function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob), link = node("a"); link.href = url; link.download = name;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- state ---------------------------------------------------------------

function latestItems() {
  const found = new Map();
  for (const job of [...state.jobs, ...state.resultJobs].sort((a, b) => b.created_at.localeCompare(a.created_at)))
    if (["review", "fill"].includes(job.kind)) for (const item of job.items) if (!found.has(keyOf(item))) found.set(keyOf(item), {job, item});
  return found;
}
function visibleItems(latest = latestItems()) {
  const normalize = value => value.normalize("NFKC").replace(/\s+/g, " ").trim().toLocaleLowerCase();
  const q = normalize($("searchInput").value), course = $("courseFilter").value, now = Date.now();
  return (state.pending?.assignments || []).filter(item => (!course || item.course_id === course)
    && (!q || normalize(`${item.title || ""} ${item.course_name || ""}`).includes(q))
    && filterMatch(state.filter, item, latest.get(keyOf(item)), now))
    .sort((a, b) => (a.due_utc || "9999").localeCompare(b.due_utc || "9999"));
}
function targets(latest = latestItems()) { return selectTargets(visibleItems(latest), state.selected, latest); }
function aiEnabled() { return $("aiConfirmed").checked; }

async function loadState() {
  const data = await api("/api/bootstrap");
  state.error = null; state.pending = data.pending; state.jobs = data.jobs; state.resultJobs = data.result_jobs || [];
  state.active = data.active_job_id; state.timezone = data.timezone || "Asia/Tokyo"; state.apiKey = data.api_key || null;
  if (state.pending) { const keys = new Set(state.pending.assignments.map(keyOf)); state.selected = new Set([...state.selected].filter(x => keys.has(x))); }
  if (!state.focus) state.focus = state.jobs.find(x => x.kind !== "refresh")?.id || null;
  renderAll();
  if (state.active) startPolling();
}
function updateJob(job) {
  const index = state.jobs.findIndex(x => x.id === job.id);
  if (index >= 0) state.jobs[index] = job; else state.jobs.unshift(job);
}

// ---- rendering -----------------------------------------------------------

function renderAll() { renderHeader(); renderIssues(); renderFilters(); renderList(); renderJob(); renderHistory(); renderButtons(); renderApiKey(); }

function renderHeader() {
  const pending = state.pending, source = pending?.source || {}, latest = latestItems();
  const answered = (pending?.assignments || []).filter(x => hasAnswer(latest.get(keyOf(x))?.item)).length;
  const parts = [];
  if (source.classroom_fetched) parts.push(`资料库 ${source.classroom_fetched.slice(5, 16)} 抓取`);
  if (pending) parts.push(`${pending.assignments.length} 项待办`, `${answered} 项已有答案`);
  $("subtitle").textContent = pending ? parts.join(" · ") : "还没有读取待办，点击刷新";
  $("homeLink").hidden = !BASE;
}
function renderIssues() {
  const list = $("issues"); list.replaceChildren();
  const add = (tone, text, action) => { const li = node("li"); li.append(node("span", undefined, "dot " + tone), node("span", text)); if (action) li.append(action); list.append(li); };
  if (!token && !BASE) add("critical", "缺少访问凭证。请从 yishuapp.com/classroom/ 打开，或用 SSH 隧道加上 server.json 里的令牌。");
  if (state.error) add("critical", state.error);
  if (aiEnabled() && state.apiKey && !state.apiKey.configured) add("warning", "还没有设置 Claude API 密钥，作答前需要先填写。", button("去设置", "link small", () => $("settingsDialog").showModal()));
  const failed = state.pending?.inaccessible_courses || [];
  if (failed.length) add("critical", `${failed.length} 门课程读取失败，待办列表可能不完整。`);
  const unmapped = state.pending?.source?.unmapped_courses || [];
  if (unmapped.length) add("warning", `资料库没有抓取 ${unmapped.length} 门课：${unmapped.join("、")}`);
  if (state.pending?.source && state.pending.source.classroom_ok === false) add("warning", "资料库上次抓取 Classroom 出错，数据可能不是最新。");
  list.hidden = !list.children.length;
  const omitted = state.pending?.needs_confirmation || [];
  $("discoveryDetails").hidden = !omitted.length;
  $("discoverySummary").textContent = `另有 ${omitted.length} 项已退回或状态不明，没有列入待办`;
  $("discoveryContent").replaceChildren();
  for (const item of omitted) {
    const p = node("p", `${item.course_name || ""} · ${item.title || "未命名作业"}（${labels[item.submission_state] || item.submission_state || "状态不明"}） `, "muted");
    const link = safeLink(item.url, "去 Classroom 确认 ↗"); if (link) p.append(link); $("discoveryContent").append(p);
  }
}
function renderFilters() {
  const latest = latestItems(), now = Date.now(), all = state.pending?.assignments || [];
  const box = $("filters"); box.replaceChildren();
  for (const [value, label] of FILTERS) {
    const count = all.filter(item => filterMatch(value, item, latest.get(keyOf(item)), now)).length;
    const b = button(`${label} ${count}`, "", () => { state.filter = value; localStorage.setItem("classroom-filter", value); renderList(); renderFilters(); renderButtons(); });
    b.setAttribute("aria-pressed", String(state.filter === value)); box.append(b);
  }
  const select = $("courseFilter"), current = select.value, courses = new Map(all.map(x => [x.course_id, x.course_name || "未命名课程"]));
  select.replaceChildren(node("option", "全部课程")); select.firstChild.value = "";
  for (const [id, name] of courses) { const option = node("option", name); option.value = id; select.append(option); }
  select.value = courses.has(current) ? current : "";
}
function renderList() {
  const latest = latestItems(), items = visibleItems(latest), now = Date.now();
  const eligible = new Set(selectTargets(items, new Set(), latest).map(keyOf));
  state.selected = new Set([...state.selected].filter(key => eligible.has(key)));
  const body = $("assignmentList"); body.replaceChildren();
  if (!items.length) {
    const tr = node("tr"), td = node("td", !state.pending ? "还没有读取待办，点击右上角的刷新。" : "当前筛选下没有作业。", "empty"); td.colSpan = 4; tr.append(td); body.append(tr);
  }
  for (const item of items) {
    const key = keyOf(item), record = latest.get(key), status = rowStatus(record), due = dueInfo(item, now);
    const tr = node("tr"); tr.classList.toggle("selected", state.selected.has(key)); tr.classList.toggle("open", state.panel?.key === key);
    const check = node("input"); check.type = "checkbox"; check.checked = state.selected.has(key); check.disabled = !!state.active || !eligible.has(key);
    check.setAttribute("aria-label", "选择 " + (item.title || key));
    check.addEventListener("change", () => { check.checked ? state.selected.add(key) : state.selected.delete(key); renderList(); renderButtons(); });
    const checkCell = node("td", undefined, "check"); checkCell.append(check);
    const dueCell = node("td", undefined, "due"); dueCell.append(node("div", item.due_local ? dateLabel(item.due_local) : "—", "due-date num"), node("div", due.text, "small " + (due.overdue ? "bad" : "muted")));
    const titleCell = node("td", undefined, "title-cell");
    const title = status.open ? button(item.title || key, "link title", () => openPanel(record.job.id, key))
      : safeLink(item.url, item.title || key, "title plain") || node("div", item.title || key, "title");
    titleCell.append(title, node("div", item.course_name || "", "meta"));
    const mobileDue = node("div", item.due_local ? `${dateLabel(item.due_local)} · ${due.text}` : "无截止", "meta mobile-due " + (due.overdue ? "bad" : "")); mobileDue.hidden = window.innerWidth > 640; titleCell.append(mobileDue);
    if (record?.item?.error && !delivered(record.item)) titleCell.append(node("div", friendlyError(record.item.error), "note warn"));
    const stateCell = node("td", undefined, "state-cell");
    if (status.open) stateCell.append(button(status.text, "link " + status.tone, () => openPanel(record.job.id, key)));
    else stateCell.append(node("span", status.text, status.tone === "running" ? "" : status.tone));
    tr.append(checkCell, dueCell, titleCell, stateCell); body.append(tr);
  }
  const checked = [...eligible].filter(key => state.selected.has(key)).length;
  $("selectAll").checked = !!eligible.size && checked === eligible.size; $("selectAll").indeterminate = checked > 0 && checked < eligible.size;
  $("selectAll").disabled = !eligible.size || !!state.active;
  const skipped = items.length - eligible.size;
  $("listHint").textContent = `显示 ${items.length} 项${skipped ? ` · ${skipped} 项已有答案，完成时会跳过` : ""}`;
  $("clearSelection").hidden = !state.selected.size;
}
function renderButtons() {
  const count = targets().length, ai = aiEnabled();
  const g = $("generateButton");
  g.textContent = state.active ? "处理中…" : `${ai ? "完成" : "整理资料"}${state.selected.size ? "所选" : "当前"} ${count} 项`;
  g.disabled = !!state.active || state.busy || !count;
  $("refreshButton").disabled = !!state.active || state.busy || (!token && !BASE);
  $("modelSelect").disabled = !ai || !state.models; $("effortSelect").disabled = !ai || !state.models;
}
function renderJob() {
  const card = $("jobCard"), job = state.jobs.find(x => x.id === (state.active || state.focus));
  card.replaceChildren();
  if (!job || (!activeStates.has(job.status) && (job.kind === "refresh" || state.dismissed === job.id))) { card.hidden = true; return; }
  card.hidden = false;
  const now = Date.now(), s = jobSummary(job, now), top = node("div", undefined, "job-top"), side = node("div", undefined, "job-side small muted");
  if (job.kind === "refresh") {
    top.append(node("span", "正在从资料库读取待办", "job-title"));
    const meter = node("div", undefined, "meter indeterminate"); meter.append(node("span"));
    card.append(top, meter);
    return;
  }
  const title = s.active ? `正在处理 ${Math.min(s.done + 1, s.total)}/${s.total}${s.current ? " · " + (s.current.title || s.current.assignment_id) : ""}`
    : `${labels[job.status] || job.status} · ${s.total} 项`;
  top.append(node("span", title, "job-title"));
  if (s.elapsed !== null) side.append(node("span", "用时 " + durationLabel(s.elapsed), "num"));
  if (s.remaining !== null) side.append(node("span", "约剩 " + durationLabel(s.remaining * 1000), "num"));
  if (s.active && job.status !== "stopping") side.append(button("暂停", "btn small", () => jobAction(job.id, "pause")));
  if (!s.active && ["completed_with_issues", "failed", "paused", "interrupted"].includes(job.status)) side.append(button("重试未完成", "btn small", () => jobAction(job.id, "retry")));
  if (!s.active) side.append(button("关闭", "link small", () => { state.dismissed = job.id; localStorage.setItem("classroom-dismissed", job.id); renderJob(); }));
  top.append(side); card.append(top);
  if (s.active) {
    const meter = node("div", undefined, "meter" + (s.percent === null ? " indeterminate" : "")), fill = node("span");
    if (s.percent !== null) fill.style.width = s.percent.toFixed(1) + "%";
    meter.append(fill); card.append(meter);
    const steps = node("div", undefined, "steps");
    ["读资料", "Claude 作答", "核验", "生成答案"].forEach((label, i) => { const step = node("span", undefined, "step" + (i < s.step ? " done" : i === s.step ? " current" : "")); step.append(node("span", undefined, "dot"), node("span", label)); steps.append(step); });
    card.append(steps);
  }
  const message = s.warning || (s.active ? job.items?.find(x => x === s.current)?.generation?.activity_label || job.message : job.message);
  if (message) card.append(node("p", friendlyError(message), "small " + (s.warning ? "warn" : "muted")));
}
function renderHistory() {
  const jobs = state.jobs.filter(x => x.kind !== "refresh" || x.status === "failed");
  $("historyCount").textContent = jobs.length ? `${jobs.length} 次` : "";
  const list = $("historyList"); list.replaceChildren();
  for (const job of jobs.slice(0, 20)) {
    const li = node("li"), details = node("details"), summary = node("summary");
    summary.append(node("time", dateLabel(job.created_at, true)), node("span", [kindLabels[job.kind] || job.kind, job.items?.length ? `${job.items.length} 项` : "", job.model ? modelName(job.model) : "", friendlyError(job.message || "")].filter(Boolean).join(" · ")), node("span", labels[job.status] || job.status, "small " + (job.status === "completed" ? "good" : job.status === "failed" ? "bad" : "muted")));
    details.append(summary);
    const events = node("ol", undefined, "events");
    for (const event of (job.events || []).slice(-40)) { const row = node("li"); row.append(node("time", dateLabel(event.at, true)), node("span", event.message)); events.append(row); }
    details.append(events); li.append(details); list.append(li);
  }
}
function renderApiKey() {
  const status = state.apiKey;
  $("apiKeyStatus").textContent = !status ? "正在读取…" : status.configured ? `已设置 · 末尾 ${status.last4}${status.updated_at ? " · 更新于 " + dateLabel(status.updated_at, true) : ""}` : "尚未设置。没有密钥时不会调用 Claude。";
  $("apiKeyClear").hidden = !status?.configured;
}

// ---- jobs ----------------------------------------------------------------

let polling = null, pollInFlight = false, clock = null;
function startPolling() {
  if (!polling) polling = setInterval(pollJob, 2000);
  if (!clock) clock = setInterval(renderJob, 1000);
}
async function pollJob() {
  if (!state.active) { clearInterval(polling); clearInterval(clock); polling = clock = null; return; }
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const job = await api("/api/jobs/" + state.active); state.error = null; updateJob(job);
    if (!activeStates.has(job.status)) {
      state.active = null; if (job.kind !== "refresh") state.focus = job.id;
      await loadState();
      toast(job.kind === "refresh" ? (job.status === "failed" ? friendlyError(job.message) : `已读取 ${state.pending?.assignments.length || 0} 项待办`) : friendlyError(job.message));
      if (state.panel && job.items?.some(x => keyOf(x) === state.panel.key)) openPanel(job.id, state.panel.key, state.panel.tab);
    } else { renderList(); renderJob(); renderButtons(); }
  } catch (error) { state.error = error.message; renderIssues(); }
  finally { pollInFlight = false; }
}
async function action(path, body) {
  if (state.busy) return null;
  state.busy = true; renderButtons();
  try {
    const job = await api(path, "POST", body); updateJob(job);
    state.active = activeStates.has(job.status) ? job.id : null;
    if (job.kind !== "refresh") { state.focus = job.id; state.dismissed = ""; }
    renderAll(); startPolling(); return job;
  } catch (error) { toast(error.message); return null; }
  finally { state.busy = false; renderButtons(); }
}
function jobAction(id, verb) { return action(`/api/jobs/${id}/${verb}`, {}); }
function startSelected() {
  const items = targets();
  if (!items.length) return;
  if (aiEnabled() && state.apiKey && !state.apiKey.configured) { toast("请先在设置里填写 Claude API 密钥。"); $("settingsDialog").showModal(); return; }
  const body = {selected: items.map(({course_id, assignment_id}) => ({course_id, assignment_id})), ai_confirmed: aiEnabled()};
  if (aiEnabled()) Object.assign(body, {model: $("modelSelect").value, effort: $("effortSelect").value || null});
  state.selected.clear();
  action("/api/review", body);
}

// ---- models and settings -------------------------------------------------

async function loadModels() {
  try {
    const catalog = await api("/api/models"); state.models = catalog;
    const select = $("modelSelect"); select.replaceChildren();
    for (const model of catalog.models) { const option = node("option", model.displayName || model.model); option.value = model.model; select.append(option); }
    const saved = localStorage.getItem("classroom-model");
    select.value = catalog.models.some(x => x.model === saved) ? saved : catalog.default_model;
    renderEfforts(localStorage.getItem("classroom-effort"));
  } catch (error) { toast(error.message); }
  renderButtons();
}
function renderEfforts(preferred) {
  const model = state.models?.models.find(x => x.model === $("modelSelect").value), select = $("effortSelect"); select.replaceChildren();
  for (const {reasoningEffort} of model?.supportedReasoningEfforts || []) { const option = node("option", "强度 " + (efforts[reasoningEffort] || reasoningEffort)); option.value = reasoningEffort; select.append(option); }
  const values = [...select.options].map(x => x.value);
  select.value = values.includes(preferred) ? preferred : values.includes(model?.defaultReasoningEffort) ? model.defaultReasoningEffort : values[0] || "";
}
function modelName(id) { return state.models?.models.find(x => x.model === id)?.displayName || id; }
function saveModelChoice() { localStorage.setItem("classroom-model", $("modelSelect").value); localStorage.setItem("classroom-effort", $("effortSelect").value); }
async function saveApiKey(key) {
  const buttons = $("apiKeyForm").querySelectorAll("button"); buttons.forEach(x => { x.disabled = true; });
  try { state.apiKey = await api("/api/settings/api-key", "PUT", {key}); $("apiKeyInput").value = ""; renderApiKey(); renderIssues(); toast(key ? "API 密钥已验证并保存" : "API 密钥已删除"); }
  catch (error) { toast(error.message); }
  finally { buttons.forEach(x => { x.disabled = false; }); }
}

// ---- answer panel --------------------------------------------------------

async function openPanel(jobId, key, tab = "answer") {
  const record = latestItems().get(key), job = state.jobs.find(x => x.id === jobId) || record?.job;
  const item = job?.items.find(x => keyOf(x) === key) || record?.item;
  if (!item) return;
  state.panel = {jobId, key, item, data: null, tab, showAll: false};
  $("workspace").classList.add("with-panel"); $("panel").hidden = false; renderList();
  $("panelTitle").textContent = item.title || key; $("panelMeta").textContent = "正在读取…";
  $("panelActions").replaceChildren(); $("panelNotes").replaceChildren(); $("panelTabs").replaceChildren(); $("panelContent").replaceChildren();
  if (!item.package) { $("panelMeta").textContent = item.course_name || ""; $("panelContent").append(node("p", friendlyError(item.error || "这项作业还没有资料包。"), "warn")); return; }
  try {
    const data = await api(`/api/jobs/${jobId}/items/${key}`);
    if (state.panel?.key !== key) return;
    state.panel.data = data; renderPanel();
  } catch (error) { $("panelMeta").textContent = ""; $("panelContent").append(node("p", error.message, "bad")); }
}
function closePanel() {
  clearImages(); state.panel = null; $("panel").hidden = true; $("workspace").classList.remove("with-panel"); renderList();
}
function renderPanel() {
  const {data, jobId, key, item} = state.panel, g = data.generation, manifest = data.manifest, assignment = manifest.assignment;
  $("panelTitle").textContent = assignment.title || item.title || key;
  const meta = [manifest.course_name];
  if (g?.status === "completed") meta.push(`${modelName(g.model)} · ${efforts[g.reasoning_effort] || g.reasoning_effort || "默认"}强度`, Number.isFinite(g.cost_usd) ? `约 ${g.cost_usd.toFixed(3)} 美元` : null);
  meta.push(data.text["draft.md"] ? "未提交" : g?.status === "not_invoked" ? "只整理了资料" : "还没有答案");
  $("panelMeta").textContent = meta.filter(Boolean).join(" · ");

  const actions = $("panelActions"); actions.replaceChildren();
  const form = data.form_fill?.forms?.[0], doc = data.document_fill?.documents?.[0];
  const original = safeLink(doc?.url || form?.url, doc ? "打开已填入的文档 ↗" : "打开预填表单 ↗", "btn small primary");
  if (original) actions.append(original);
  if (data.text["draft.md"]) actions.append(button("下载 Word", "btn small" + (original ? "" : " primary"), () => downloadAnswer(jobId, key)));
  const classroom = safeLink(assignment.alternateLink, "Classroom ↗", "btn small"); if (classroom) actions.append(classroom);
  actions.append(button("补充后重做", "btn small", () => openSupplement(jobId, key)));
  actions.append(button("课程规则", "link small", () => openPolicy(manifest.course_id, manifest.course_name)));
  actions.append(button("证据包", "link small", () => downloadEvidence(jobId, key)));

  const notes = $("panelNotes"); notes.replaceChildren();
  const lines = [];
  if (data.policy_changed) lines.push("补充说明或课程规则已变化，需要重做才会生效。");
  if (form) {
    const missing = formContentGaps(data.form_fill), user = formUserInputGaps(data.form_fill, ["user_input_fields"]), native = formUserInputGaps(data.form_fill, ["unsupported_fields"]);
    if (missing.length) lines.push(`${missing.length} 个必答题没有答案：` + missing.map(x => x.title || x.entry_id).join("；"));
    if (user.length) lines.push(`${user.length} 栏需要你本人填写，点“补充后重做”填写后会自动带入表单。`);
    if (native.length) lines.push(`${native.length} 栏需要在原表单里操作：` + native.map(x => x.title || x.entry_id).join("；"));
    lines.push("预填链接会带入候选答案；在 Google 里改过的草稿请从原表单继续。提交由你操作。");
  }
  const questions = data.review?.questions || [];
  if (questions.length) lines.push(`${questions.length} 条需要你核对，见“待核对”。`);
  if (g?.error && g.status !== "completed") lines.push(friendlyError(g.error));
  if (lines.length) { const box = node("div", undefined, "note-box"); for (const line of lines) box.append(node("span", line)); notes.append(box); }

  const tabs = $("panelTabs"); tabs.replaceChildren();
  const warnings = (manifest.warnings || []).length;
  for (const [id, label] of [["answer", "答案"], ["assignment", "原题"], ["checklist", "检查表"], ["review", `待核对 ${questions.length + warnings}`], ["sources", "来源"], ["record", "记录"]]) {
    const b = button(label, "", () => { state.panel.tab = id; renderPanelTab(); });
    b.setAttribute("role", "tab"); b.dataset.tab = id; tabs.append(b);
  }
  renderPanelTab();
}
function renderPanelTab() {
  clearImages();
  const {data, jobId, key, tab} = state.panel, content = $("panelContent"); content.replaceChildren();
  for (const b of $("panelTabs").children) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  const g = data.generation, manifest = data.manifest;
  if (tab === "answer") {
    const box = node("div", undefined, "answer");
    if (data.answer_document?.blocks?.length) for (const block of data.answer_document.blocks) {
      if (block.kind === "table") { const table = node("table"); for (const values of block.rows) { const tr = node("tr"); for (const value of values) tr.append(node("td", value)); table.append(tr); } box.append(table); }
      else box.append(node(block.kind === "heading" ? `h${Math.min(block.level || 2, 3)}` : "p", block.text));
    }
    else if (data.text["draft.md"]) box.append(node("pre", data.text["draft.md"]));
    else box.append(node("p", g?.status === "not_invoked" ? "这次没有调用 AI，只整理了题目和资料。" : "还没有通过核验的答案。", "muted"));
    content.append(box);
  }
  if (tab === "assignment") {
    const assignment = manifest.assignment;
    content.append(node("pre", assignment.description || "这项作业没有文字说明，请看附件。"));
    const links = node("div", undefined, "actions"); links.style.marginTop = "8px";
    for (const material of assignment.materials || []) { const ref = material.driveFile?.driveFile || material.link || material.youtubeVideo || material.form; const link = ref && safeLink(ref.alternateLink || ref.url || ref.formUrl, (ref.title || "附件") + " ↗", "btn small"); if (link) links.append(link); }
    if (links.children.length) content.append(links);
    for (const form of data.requirements?.response_forms || []) {
      content.append(node("h3", `${form.title}（${form.page_count} 页，${form.question_count} 栏）`));
      for (const source of (data.requirements.sources || []).filter(x => x.source_id === form.source_id)) {
        try {
          const question = JSON.parse(source.text);
          if (!question.item_id) continue;
          const row = node("div", undefined, "source"); row.append(node("div", `第 ${question.page} 页`, "locator"), node("strong", question.title));
          if (question.description) row.append(node("p", question.description, "muted"));
          const choices = (question.fields || []).flatMap(field => field.choices || []); if (choices.length) row.append(node("p", choices.join(" ／ "), "small"));
          content.append(row);
        } catch { content.append(node("pre", source.text)); }
      }
    }
    const profile = manifest.student_profile || {}, identity = [["学籍番号", "student_id"], ["氏名", "name"], ["学科", "department"], ["クラス", "class_name"]].filter(([, field]) => profile[field]);
    content.append(node("h3", "作答使用的身份"));
    if (identity.length) { const dl = node("dl", undefined, "facts"); for (const [label, field] of identity) dl.append(node("dt", label), node("dd", profile[field])); content.append(dl); }
    else content.append(node("p", "没有设置学生资料，身份栏会留空。", "muted small"));
  }
  if (tab === "checklist") {
    const checks = data.review?.requirement_checks || [];
    if (!checks.length) content.append(node("p", "还没有检查表。", "muted"));
    const tone = {met: ["已满足", "good"], partial: ["部分满足", "warn"], unmet: ["未满足", "bad"], needs_user: ["待你确认", "warn"]};
    for (const check of checks) { const row = node("div", undefined, "source"), [text, cls] = tone[check.status] || [check.status, "muted"]; row.append(node("div", text, "small " + cls), node("pre", check.requirement)); if (check.draft_location) row.append(node("p", check.draft_location, "muted small")); content.append(row); }
  }
  if (tab === "review") {
    const questions = data.review?.questions || [], warnings = manifest.warnings || [];
    if (!questions.length && !warnings.length) content.append(node("p", "没有记录待核对的事项，内容仍需你审阅。", "muted"));
    for (const question of questions) { const row = node("div", undefined, "source"); row.append(node("p", question)); content.append(row); }
    if (warnings.length) content.append(node("h3", "资料缺口"));
    for (const warning of warnings) { const row = node("div", undefined, "source"); row.append(node("div", warning.title || warning.source, "locator"), node("p", warning.error)); content.append(row); }
  }
  if (tab === "sources") {
    const used = new Set(g?.used_evidence_ids || []), all = data.evidence?.sources || [];
    const ordered = [...all.filter(x => used.has(x.id)), ...all.filter(x => !used.has(x.id))];
    const shown = state.panel.showAll ? ordered : ordered.slice(0, Math.max(used.size, 30));
    content.append(node("p", `${used.size} 段被答案引用，共 ${all.length} 段资料。`, "muted small"));
    for (const source of shown) {
      const row = node("div", undefined, "source"), head = node("div", `${source.title || source.source_kind} · ${source.locator}${used.has(source.id) ? " · 已引用" : ""}`, "locator");
      row.append(head, node("pre", source.text.length > 1200 ? source.text.slice(0, 1200) + "…" : source.text));
      const link = safeLink(source.url, "原来源 ↗", "small"); if (link) row.append(link);
      if (source.image_path) row.append(button("查看页面图像", "link small", async event => {
        event.currentTarget.disabled = true;
        try { const blob = await api(`/api/jobs/${jobId}/items/${key}/image/${source.id}`), url = URL.createObjectURL(blob); state.images.push(url); const img = node("img"); img.src = url; img.alt = `${source.title} ${source.locator}`; row.append(img); event.target.remove(); }
        catch (error) { toast(error.message); event.target.disabled = false; }
      }));
      content.append(row);
    }
    if (shown.length < ordered.length) content.append(button(`显示其余 ${ordered.length - shown.length} 段`, "btn small", () => { state.panel.showAll = true; renderPanelTab(); }));
  }
  if (tab === "record") {
    if (!g || g.status === "not_invoked") { content.append(node("p", "这次没有调用 AI。", "muted")); return; }
    const usage = g.token_usage || {}, dl = node("dl", undefined, "facts");
    const rows = [["状态", g.status === "completed" ? "已完成并通过核验" : g.status], ["模型", modelName(g.model || g.requested_model)], ["推理强度", efforts[g.reasoning_effort] || g.reasoning_effort || "默认"],
      ["计费", g.api_key_source === "ANTHROPIC_API_KEY" ? "Claude API 密钥" : g.api_key_source || "—"], ["费用", Number.isFinite(g.cost_usd) ? `${g.cost_usd.toFixed(4)} 美元` : "—"],
      ["用时", Number.isFinite(g.duration_ms) ? durationLabel(g.duration_ms) : "—"], ["输入 / 输出", usage.input_tokens !== undefined ? `${usage.input_tokens + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0)} / ${usage.output_tokens} tokens` : "—"],
      ["送入资料", `${g.input?.source_ids?.length || 0} 段文字 · ${g.input?.images?.length || 0} 张页图`], ["开始", dateLabel(g.started_at, true)], ["结束", g.finished_at ? dateLabel(g.finished_at, true) : "—"]];
    for (const [label, value] of rows) dl.append(node("dt", label), node("dd", value));
    content.append(dl);
    if (g.error) content.append(node("p", friendlyError(g.error), "bad"));
    if (data.text["summary.md"]) content.append(node("h3", "模型说明"), node("pre", data.text["summary.md"]));
  }
}
function clearImages() { for (const url of state.images) URL.revokeObjectURL(url); state.images = []; }
async function downloadAnswer(jobId, key) {
  try {
    const job = await api(`/api/jobs/${jobId}/items/${key}/export-answer`, "POST", {}); updateJob(job);
    const blob = await api(`/api/jobs/${jobId}/items/${key}/answer-docx`), item = job.items.find(x => keyOf(x) === key);
    saveBlob(blob, (item?.title || "答案").replace(/[\\/:*?"<>|]/g, "_") + "-审阅稿.docx");
    toast("Word 已下载；核对修改后再手动提交。");
  } catch (error) { toast(error.message); }
}
async function downloadEvidence(jobId, key) {
  try { saveBlob(await api(`/api/jobs/${jobId}/items/${key}/download`), `证据包-${key.replace(":", "-")}.zip`); } catch (error) { toast(error.message); }
}

// ---- course rules --------------------------------------------------------

async function openPolicy(courseId, courseName) {
  try {
    const policy = await api("/api/policies/" + courseId); state.policyCourse = courseId;
    $("policyTitle").textContent = (courseName || "课程") + " · 课程规则";
    $("policyUse").value = policy.ai_use; $("policyEvidence").value = policy.policy_evidence; $("policyLimits").value = policy.limitations;
    $("policyDisclosure").value = policy.disclosure; $("personalFacts").value = policy.personal_facts.join("\n"); $("policyError").hidden = true;
    $("policyDialog").showModal();
  } catch (error) { toast(error.message); }
}

// ---- supplement ------------------------------------------------------------

function lines(id) { return $(id).value.split("\n").map(x => x.trim()).filter(Boolean); }
// Original native fields and choices are data; none can change destinations or permissions.
function supplementFields(data) {
  const known = new Map((data?.model_form_answers || []).filter(x => x.values?.length).map(x => [`${x.form_url}:${x.entry_id}`, x]));
  const supplied = new Map((data?.user_form_answers?.answers || []).map(x => [`${x.form_url}:${x.entry_id}`, x]));
  return (data?.response_fields || []).flatMap(form => form.questions.flatMap(question => question.fields.map(field => ({form, question, field, supplied: supplied.get(`${form.url}:${field.entry_id}`), generated: known.has(`${form.url}:${field.entry_id}`)}))))
    .filter(x => !x.generated || x.supplied).sort((a, b) => a.question.page - b.question.page || a.question.title.localeCompare(b.question.title, "ja", {numeric: true}));
}
function renderAnswerInputs(data) {
  const content = $("answerInputs"); content.replaceChildren();
  const fields = supplementFields(data);
  $("answerInputsSection").hidden = !fields.length && !data?.response_fields_error;
  $("answerInputNote").textContent = data?.response_fields_error || "按原题填写或选择。回答只保存在小主机上，会自动带入预填表单。";
  $("extraSupplement").open = !fields.length;
  for (const {form, question, field, supplied} of fields) {
    const group = node("div", undefined, "answer-field"), id = `answer-input-${state.supplement.editors.length}`;
    const label = node("label", `第 ${question.page} 页 · ${question.title}${field.required ? "（必填）" : "（选填）"}`); label.htmlFor = id; group.append(label);
    if (question.description) group.append(node("p", question.description, "small muted"));
    let control, read;
    if (["single_choice", "dropdown", "scale"].includes(question.type) && field.choices.length) {
      control = node("select"); const empty = node("option", "请选择"); empty.value = ""; control.append(empty);
      for (const value of field.choices) { const option = node("option", value); option.value = value; control.append(option); }
      control.value = supplied?.values?.[0] || ""; read = () => control.value ? [control.value] : [];
    } else if (["short_text", "paragraph"].includes(question.type)) {
      control = node(question.type === "paragraph" ? "textarea" : "input"); if (question.type === "paragraph") control.rows = 3; else control.type = "text";
      control.maxLength = 20000; control.value = supplied?.values?.[0] || ""; read = () => control.value.trim() ? [control.value.trim()] : [];
    } else if (question.type === "checkbox" && field.choices.length) {
      control = node("fieldset"); const options = [];
      for (const value of field.choices) { const option = node("label", undefined, "inline"), input = node("input"); input.type = "checkbox"; input.value = value; input.checked = supplied?.values?.includes(value) || false; option.append(input, node("span", value)); control.append(option); options.push(input); }
      read = () => options.filter(x => x.checked).map(x => x.value);
    } else { group.append(node("p", "这一题需要在原表单里填写。", "small muted")); content.append(group); continue; }
    control.id = id; group.append(control); content.append(group);
    state.supplement.editors.push({form_url: form.url, entry_id: field.entry_id, context_sha256: form.context_sha256, read});
  }
}
function supplementNotesChanged() { return $("supplementText").value !== state.supplement.savedText || JSON.stringify(lines("supplementFacts")) !== JSON.stringify(state.supplement.savedFacts); }
function updateSupplementActions() {
  const answersOnly = state.supplement.editors.length && !supplementNotesChanged();
  $("saveSupplement").textContent = answersOnly ? "只保存回答" : "只保存";
  $("continueSupplement").textContent = answersOnly ? "保存并带入表单" : "保存并重做";
  $("supplementAiRow").hidden = !!answersOnly;
  const s = state.supplement;
  $("supplementModel").textContent = answersOnly ? "只合并你的回答，不重新调用 AI。" : `重做会使用 ${s.model ? modelName(s.model) : "所选模型"}。`;
}
async function openSupplement(jobId, key) {
  try {
    const job = state.jobs.find(x => x.id === jobId) || await api(`/api/jobs/${jobId}`), item = job.items.find(x => keyOf(x) === key);
    const data = state.panel?.key === key && state.panel.data ? state.panel.data : item.package ? await api(`/api/jobs/${jobId}/items/${key}`) : null;
    const supplement = data?.supplement || await api(`/api/jobs/${jobId}/items/${key}/supplement`);
    state.supplement = {jobId, key, model: $("modelSelect").value || job.model || null, effort: $("effortSelect").value || job.effort || null, editors: [], savedText: supplement.text || "", savedFacts: supplement.personal_facts || []};
    $("supplementTitle").textContent = item.title || key;
    $("supplementText").value = supplement.text || ""; $("supplementFacts").value = (supplement.personal_facts || []).join("\n");
    renderAnswerInputs(data); updateSupplementActions();
    const notes = $("supplementQuestions"); notes.replaceChildren();
    if (data?.review?.questions?.length) { notes.append(node("strong", "上次留下的待核对事项")); for (const question of data.review.questions) notes.append(node("p", "· " + question, "muted")); }
    $("supplementAi").checked = job.ai_confirmed !== false; $("supplementError").hidden = true;
    const urls = [...new Set([...(data?.requirements?.response_forms || []).map(x => x.url), ...(data?.manifest?.warnings || []).map(x => x.url)].filter(url => /^https:\/\/docs\.google\.com\/forms\//.test(url || "")))];
    const links = $("restrictedFormLinks"); links.replaceChildren();
    for (const url of urls) { const link = safeLink(url, "打开原表单 ↗", "btn small"); if (link) links.append(link); }
    $("captureUrl").value = urls[0] || ""; $("captureScript").value = ""; $("browserQuestionSection").open = false;
    $("supplementDialog").showModal();
  } catch (error) { toast(error.message); }
}
async function supplementAction(control, operation) {
  control.disabled = true; $("supplementError").hidden = true;
  try { await operation(); } catch (error) { $("supplementError").textContent = error.message; $("supplementError").hidden = false; }
  finally { control.disabled = false; }
}
async function saveFormInputs(apply, close = true) {
  const {jobId, key, editors} = state.supplement;
  const answers = editors.map(({read, ...mapping}) => ({...mapping, values: read()})).filter(x => x.values.length);
  const result = await api(`/api/jobs/${jobId}/items/${key}/answers`, "POST", {answers, apply});
  if (!close) return result;
  $("supplementDialog").close(); await loadState();
  toast(result.apply_queued ? "回答已保存，当前任务结束后自动带入表单" : apply ? "回答已保存，正在带入表单" : "回答已保存");
  return result;
}
async function saveSupplement(continueRun) {
  const {jobId, key, model, effort} = state.supplement;
  if (state.supplement.editors.length && !supplementNotesChanged()) return saveFormInputs(continueRun);
  if (state.supplement.editors.length) await saveFormInputs(false, false);
  const payload = {text: $("supplementText").value, personal_facts: lines("supplementFacts")};
  if (continueRun) Object.assign(payload, {ai_confirmed: $("supplementAi").checked, model, effort});
  const job = await api(`/api/jobs/${jobId}/items/${key}/${continueRun ? "continue" : "supplement"}`, "POST", payload);
  $("supplementDialog").close(); await loadState();
  if (continueRun && job?.id) { state.focus = job.id; state.dismissed = ""; renderJob(); }
  toast(continueRun ? "已保存，正在重做这项作业" : "补充已保存，下次处理时使用");
}

// ---- wiring --------------------------------------------------------------

$("refreshButton").addEventListener("click", () => action("/api/refresh", {}));
$("generateButton").addEventListener("click", startSelected);
$("openSettings").addEventListener("click", () => $("settingsDialog").showModal());
$("closePanel").addEventListener("click", closePanel);
document.addEventListener("keydown", event => { if (event.key === "Escape" && state.panel && !document.querySelector("dialog[open]")) closePanel(); });
for (const close of document.querySelectorAll(".close-dialog")) close.addEventListener("click", () => close.closest("dialog").close());
$("aiConfirmed").checked = localStorage.getItem("classroom-ai-enabled") !== "false";
$("aiConfirmed").addEventListener("change", () => { localStorage.setItem("classroom-ai-enabled", String(aiEnabled())); renderButtons(); renderIssues(); });
$("modelSelect").addEventListener("change", () => { renderEfforts(); saveModelChoice(); });
$("effortSelect").addEventListener("change", saveModelChoice);
$("searchInput").addEventListener("input", () => { renderList(); renderButtons(); });
$("courseFilter").addEventListener("change", () => { renderList(); renderButtons(); });
$("selectAll").addEventListener("change", () => { const latest = latestItems(); for (const item of selectTargets(visibleItems(latest), new Set(), latest)) $("selectAll").checked ? state.selected.add(keyOf(item)) : state.selected.delete(keyOf(item)); renderList(); renderButtons(); });
$("clearSelection").addEventListener("click", () => { state.selected.clear(); renderList(); renderButtons(); });
$("apiKeyForm").addEventListener("submit", event => { event.preventDefault(); saveApiKey($("apiKeyInput").value.trim()); });
$("apiKeyClear").addEventListener("click", () => { if (confirm("删除已保存的 Claude API 密钥？删除后无法作答，直到重新填写。")) saveApiKey(""); });
$("policyForm").addEventListener("submit", async event => {
  event.preventDefault(); const submit = event.submitter; submit.disabled = true;
  try {
    await api("/api/policies/" + state.policyCourse, "PUT", {ai_use: $("policyUse").value, policy_evidence: $("policyEvidence").value, limitations: $("policyLimits").value, disclosure: $("policyDisclosure").value, personal_facts: lines("personalFacts")});
    $("policyDialog").close(); toast("课程规则已保存；重做这门课的作业时生效。");
    if (state.panel?.data) openPanel(state.panel.jobId, state.panel.key, state.panel.tab);
  } catch (error) { $("policyError").textContent = error.message; $("policyError").hidden = false; }
  finally { submit.disabled = false; }
});
for (const id of ["supplementText", "supplementFacts"]) $(id).addEventListener("input", updateSupplementActions);
$("saveSupplement").addEventListener("click", event => supplementAction(event.currentTarget, () => saveSupplement(false)));
$("supplementForm").addEventListener("submit", event => { event.preventDefault(); supplementAction(event.submitter, () => saveSupplement(true)); });
$("importQuestions").addEventListener("click", event => supplementAction(event.currentTarget, async () => {
  const {jobId, key} = state.supplement;
  const result = await api(`/api/jobs/${jobId}/items/${key}/questions`, "POST", {url: $("captureUrl").value, question_script: $("captureScript").value});
  $("captureScript").value = ""; toast(`已导入 ${result.page_count} 页、${result.question_count} 栏题目，点“保存并重做”继续。`);
}));
window.addEventListener("resize", () => { for (const el of document.querySelectorAll(".mobile-due")) el.hidden = window.innerWidth > 640; });

// Pick up work started in another window, and refresh a stale to-do list while idle.
let lastRefreshAttempt = 0;
async function syncWhenIdle() {
  if ((!token && !BASE) || state.active || state.busy || document.hidden || document.querySelector("dialog[open]")) return;
  try {
    await loadState();
    const age = Date.now() - Date.parse(state.pending?.refreshed_at || 0);
    if (!state.active && !(age < 15 * 60 * 1000) && Date.now() - lastRefreshAttempt > 60000) { lastRefreshAttempt = Date.now(); await action("/api/refresh", {}); }
  } catch (error) { state.error = error.message; renderIssues(); }
}
setInterval(syncWhenIdle, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) syncWhenIdle(); });

renderAll();
if (token || BASE) loadState().then(() => { loadModels(); syncWhenIdle(); }).catch(error => { state.error = error.message; renderIssues(); });
