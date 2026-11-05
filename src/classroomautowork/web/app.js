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
// Classroom's own submission states, for work left out of the list (pending.py: returned or unknown).
const submissionLabels = {RETURNED: "已退回", SUBMISSION_STATE_UNSPECIFIED: "状态不明"};
// Google Forms question types, in the words of the Chinese Google Forms editor.
const questionTypes = {short_text: "简答题", paragraph: "段落", single_choice: "单选题", checkbox: "复选框", dropdown: "下拉列表", scale: "线性量表",
  grid: "网格题", date: "日期", time: "时间", file_upload: "文件上传", image: "图片"};
const efforts = {low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高"};
const FILTERS = [["all", "全部"], ["week", "7 天内"], ["nodue", "无截止"], ["review", "待审阅"], ["overdue", "已逾期"]];
const STALE_MS = 3 * 60 * 1000;

let token = location.hash.slice(1);
if (token) { sessionStorage.setItem("classroom-access", token); history.replaceState(null, "", location.pathname); }
else token = sessionStorage.getItem("classroom-access") || "";

const state = {pending: null, jobs: [], resultJobs: [], active: null, focus: null, dismissed: localStorage.getItem("classroom-dismissed") || "",
  selected: new Set(), filter: localStorage.getItem("classroom-filter") || "all", busy: false, models: null, apiKey: null, timezone: "Asia/Tokyo",
  panel: null, supplement: null, images: [], error: null};
// True until the first answer from the server: the list shows placeholder rows rather than "nothing yet".
let booting = !!(token || BASE);

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
function inlineRuns(tag, block) { const el = node(tag); for (const run of block.runs || [{text: block.text}]) el.append(run.bold ? node("strong", run.text) : document.createTextNode(run.text || "")); return el; }
function button(text, className, handler) { const b = node("button", text, className); b.type = "button"; b.addEventListener("click", handler); return b; }
function safeLink(url, text, className) {
  try {
    const u = new URL(url);
    if (u.protocol !== "https:" || !(u.hostname.endsWith(".google.com") || ["google.com", "youtube.com", "www.youtube.com", "youtu.be"].includes(u.hostname))) return null;
    const a = node("a", text, className); a.href = u.href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
  } catch { return null; }
}

// The shared outline icons (24px grid, stroke 2), built with createElementNS because the policy forbids images.
const YS_ICONS = {
  "chevron-right": "M9.5 5.5 16 12l-6.5 6.5",
  "chevron-down": "M5.5 9.5 12 16l6.5-6.5",
  refresh: "M18.14 16.3A7.5 7.5 0 1 1 17.3 6.7L20 9.4M20 4.8V9.4H15.4",
  check: "M5 12.5l4.5 4.5L19 7.5",
  download: "M12 4v10.5M7.5 10 12 14.5l4.5-4.5M4.5 15v2.5a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V15",
  external: "M7.5 16.5 16.5 7.5M9 7.5h7.5V15",
  search: "M10.5 4.5a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM15 15l4.5 4.5",
  warning: "M10.27 4.5a2 2 0 0 1 3.46 0l7.36 12.75a2 2 0 0 1-1.73 3H4.64a2 2 0 0 1-1.73-3zM12 9.5v4M12 16.75h.01",
  error: "M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM12 7.75v5M12 16.25h.01",
  info: "M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM12 11v5.25M12 7.75h.01",
  running: "M12 4.5a7.5 7.5 0 1 1-7.5 7.5",
  pending: "M12 4.5a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15z",
  close: "M6.5 6.5l11 11M17.5 6.5l-11 11",
  document: "M13.5 3.5h-6a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-10zM13.5 3.5v5h5M9 12.5h6M9 16h4"
};
function ysIcon(name, extraClass) {
  const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
  const attrs = {class: extraClass ? "ys-icon " + extraClass : "ys-icon", viewBox: "0 0 24 24", width: "16", height: "16", fill: "none",
    stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true"};
  for (const key in attrs) svg.setAttribute(key, attrs[key]);
  const path = document.createElementNS(NS, "path"); path.setAttribute("d", YS_ICONS[name]); svg.append(path);
  return svg;
}
// Titles, course names, 原题, sources and the draft come from Japanese courses: they get lang="ja", so they are set in
// Japanese glyph forms. The page's own words stay Simplified Chinese.
const JAPANESE = /[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ]/;
const ja = text => JAPANESE.test(text || "") ? "ja" : null;
function withLang(el, text) { const lang = ja(text); if (lang) el.lang = lang; else el.removeAttribute("lang"); return el; }
function externalLink(url, text, className) { const a = safeLink(url, text, className); if (a) a.append(ysIcon("external")); return a; }
// Facts side by side, each one wrapping to the next line as a whole (a long name or message wraps inside itself).
// The dot between two facts is drawn by the style sheet, which also hides it at the start of a line.
const CLOSING_BRACKET = /[）」』】〕〉》］｝]$/;
function factRow(parts, className) {
  const row = node("span", undefined, "fact-row" + (className ? " " + className : ""));
  for (const part of parts) {
    if (!part) continue;
    const {text, lang = null, cls = null, wrap = false} = typeof part === "string" ? {text: part} : part;
    if (!text) continue;
    const fact = node("span", undefined, ["fact", wrap && "fact--wrap", cls].filter(Boolean).join(" "));
    if (lang) fact.lang = lang;
    // A full-width closing bracket ends in half an em of blank, which would double the gap before the next dot.
    if (CLOSING_BRACKET.test(text)) fact.append(text.slice(0, -1), node("span", text.slice(-1), "fact-halt")); else fact.textContent = text;
    row.append(fact);
  }
  return row;
}
// A scroller whose content runs on past an edge fades that edge out.
function edgeFade(box, vertical) {
  if (!box) return;
  const more = vertical ? box.scrollHeight - box.clientHeight : box.scrollWidth - box.clientWidth;
  const at = vertical ? box.scrollTop : box.scrollLeft;
  box.classList.toggle("fade-start", more > 1 && at > 1);
  box.classList.toggle("fade-end", more > 1 && at < more - 1);
}
// Scroll a scroller just far enough that one of its children shows whole, clear of the faded edges (its scroll-padding);
// with less than that padding left to go, it goes all the way, so no fade is left over an end with nothing behind it.
function revealSegment(box, el) {
  const max = box && el ? box.scrollWidth - box.clientWidth : 0;
  if (max <= 1) return;
  const style = getComputedStyle(box), outer = box.getBoundingClientRect(), inner = el.getBoundingClientRect();
  const padStart = parseFloat(style.scrollPaddingLeft) || 0, padEnd = parseFloat(style.scrollPaddingRight) || 0;
  let left = box.scrollLeft;
  if (inner.left < outer.left + padStart) { left -= outer.left + padStart - inner.left; if (left < padStart) left = 0; }
  else if (inner.right > outer.right - padEnd) { left += inner.right - (outer.right - padEnd); if (max - left < padEnd) left = max; }
  else return;
  box.scrollLeft = left;
  edgeFade(box);
}
// aria-busy="true" while something loads or runs (an empty value would read as false).
function setBusy(el, busy) { if (busy) el.setAttribute("aria-busy", "true"); else el.removeAttribute("aria-busy"); }
// Rebuilding a region keeps keyboard focus on the same control (matched by data-focus).
function keepFocus(region, render) {
  const active = document.activeElement, key = region.contains(active) ? active.dataset?.focus : null;
  render();
  if (key && !active.isConnected) region.querySelector(`[data-focus="${CSS.escape(key)}"]`)?.focus({preventScroll: true});
}

let toastTimer = null;
// tone: "ok" (a check) or "bad" (an error mark); none for plain notices.
function toast(message, tone) {
  const el = $("toast");
  el.className = "ys-toast toast" + (tone === "bad" ? " ys-toast--bad" : "");
  el.replaceChildren(...(tone ? [ysIcon(tone === "bad" ? "error" : "check")] : []), node("span", message));
  el.hidden = false; placeToast(el);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; placeToast(el); }, 5000);
}
// An open dialog sits in the browser's top layer: a toast left outside it is covered. While a dialog is open the toast
// lives inside it, shown as a popover on top, and goes back to the page when the dialog closes.
function placeToast(el) {
  if (!el.showPopover) return;
  const dialog = el.hidden ? null : [...document.querySelectorAll("dialog:modal")].at(-1);
  const host = dialog || document.body;
  if (el.parentNode !== host) {
    host.append(el);  // moving an open popover closes it
    dialog?.addEventListener("close", () => placeToast(el), {once: true});
  }
  el.popover = "manual";
  if (el.matches(":popover-open")) el.hidePopover();
  if (!el.hidden) el.showPopover();
}
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
  booting = false;
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
  $("subtitle").replaceChildren(pending ? factRow(parts) : booting ? "正在读取…" : "还没有读取待办，点击刷新");
  $("homeLink").hidden = !BASE;
}
// Issues: a dot and the words, in one banner tinted by the worst of them; the header pill counts them.
function renderIssues() {
  const list = $("issues"), found = [];
  const add = (tone, text, action) => {
    const li = node("li"), words = node("span", undefined, "issue-text");
    words.append(...[text].flat());
    li.append(node("span", undefined, "dot " + tone), words);
    if (action) li.append(action);
    found.push([tone, li]);
  };
  if (!token && !BASE) add("critical", "缺少访问凭证。请从 yishuapp.com/classroom/ 打开，或用 SSH 隧道加上 server.json 里的令牌。");
  if (state.error) add("critical", state.error);
  if (aiEnabled() && state.apiKey && !state.apiKey.configured) add("warning", "还没有设置 Claude API 密钥，作答前需要先填写。", button("去设置", "ys-btn ys-btn--sm issue-action link small", () => $("settingsDialog").showModal()));
  const failed = state.pending?.inaccessible_courses || [];
  if (failed.length) add("critical", `${failed.length} 门课程读取失败，待办列表可能不完整。`);
  const unmapped = state.pending?.source?.unmapped_courses || [];
  if (unmapped.length) { const names = node("span", unmapped.join("、")); withLang(names, unmapped.join("")); add("warning", [`资料库没有抓取 ${unmapped.length} 门课：`, names]); }
  if (state.pending?.source && state.pending.source.classroom_ok === false) add("warning", "资料库上次抓取 Classroom 出错，数据可能不是最新。");
  list.replaceChildren(...found.map(([, li]) => li));
  list.hidden = !found.length;
  const worst = found.some(([tone]) => tone === "critical") ? "bad" : "warn";
  const banner = $("issuesBanner");
  banner.hidden = !found.length;
  banner.className = `ys-alert ys-alert--${worst} issues-banner`;
  banner.firstElementChild.replaceWith(ysIcon(worst === "bad" ? "error" : "warning"));
  // The pill is rewritten only when its words or tone change, so it is not announced again on every sync.
  const pill = $("issueCount"), pillClass = `ys-page-status ys-pill ys-pill--lg ys-pill--${worst}`, pillText = `${found.length} 项需要注意`;
  pill.hidden = !found.length;
  if (pill.className !== pillClass) pill.className = pillClass;
  if (pill.textContent !== pillText) pill.textContent = pillText;

  const omitted = state.pending?.needs_confirmation || [];
  $("discoveryDetails").hidden = !omitted.length;
  $("discoverySummary").replaceChildren(node("span", `另有 ${omitted.length} 项已退回或状态不明，没有列入待办`), ysIcon("chevron-right", "ys-disclosure-chevron"));
  const content = $("discoveryContent"); content.replaceChildren();
  for (const item of omitted) {
    const row = node("div", undefined, "discovery-item");
    row.append(withLang(node("span", item.title || "未命名作业", "discovery-title"), item.title),
      factRow([item.course_name && {text: item.course_name, lang: ja(item.course_name), wrap: true},
        submissionLabels[item.submission_state] || labels[item.submission_state] || item.submission_state || "状态不明"], "discovery-meta"));
    const link = externalLink(item.url, "去 Classroom 确认", "ys-link"); if (link) row.append(link);
    content.append(row);
  }
}
// The filter whose segment was last scrolled into view, with or without its count (the counts change the widths).
let revealedFilter = null;
function renderFilters() {
  const latest = latestItems(), now = Date.now(), all = state.pending?.assignments || [];
  const box = $("filters"), known = !(booting && !state.pending);
  keepFocus(box, () => {
    box.replaceChildren();
    for (const [value, label] of FILTERS) {
      const count = all.filter(item => filterMatch(value, item, latest.get(keyOf(item)), now)).length;
      const b = button(known ? `${label} ` : label, "", () => { state.filter = value; localStorage.setItem("classroom-filter", value); renderList(); renderFilters(); renderButtons(); });
      if (known) b.append(node("span", String(count), "ys-seg-count"));
      b.setAttribute("aria-pressed", String(state.filter === value)); b.dataset.focus = "filter-" + value; box.append(b);
    }
  });
  edgeFade(box.parentElement);
  // When the filters overflow, the chosen one is brought into view once per choice, not on every sync.
  if (revealedFilter !== state.filter + known) { revealedFilter = state.filter + known; revealSegment(box.parentElement, box.querySelector('[aria-pressed="true"]')); }
  const select = $("courseFilter"), current = select.value, courses = new Map(all.map(x => [x.course_id, x.course_name || "未命名课程"]));
  select.replaceChildren(node("option", "全部课程")); select.firstChild.value = "";
  for (const [id, name] of courses) { const option = withLang(node("option", name), name); option.value = id; select.append(option); }
  select.value = courses.has(current) ? current : "";
}
// The state of a row: a pill only where something needs attention, a blue dot while it is being worked on, plain words
// otherwise; a chevron where the row opens the answer panel.
function statusMark(status) {
  const box = node("span", undefined, "state"), text = status.text.replace(/\s*›$/, "");
  if (status.tone === "warn" || status.tone === "bad") box.append(node("span", text, `ys-pill ys-pill--${status.tone} ${status.tone}`));
  else if (status.tone === "running") box.append(node("span", text, "ys-status ys-status--info"));
  else box.append(node("span", text, status.tone === "good" ? "state-plain done good" : "state-plain muted"));
  if (status.open) box.append(ysIcon("chevron-right", "ys-row-chevron"));
  return box;
}
function loadingRow() {
  const tr = node("tr", undefined, "loading-row");
  tr.setAttribute("aria-hidden", "true");
  for (const [cls, bars] of [["check", 1], ["due", 2], ["title-cell", 2], ["state", 1]]) {
    const td = node("td", undefined, cls);
    for (let i = 0; i < bars; i++) td.append(node("span", undefined, "ys-skeleton-bar"));
    tr.append(td);
  }
  return tr;
}
function renderList() {
  const latest = latestItems(), items = visibleItems(latest), now = Date.now();
  const eligible = new Set(selectTargets(items, new Set(), latest).map(keyOf));
  state.selected = new Set([...state.selected].filter(key => eligible.has(key)));
  const body = $("assignmentList");
  keepFocus(body, () => {
    body.replaceChildren();
    setBusy(body.parentElement, booting && !state.pending);
    if (booting && !state.pending) { for (let i = 0; i < 6; i++) body.append(loadingRow()); }
    else if (!items.length) {
      const tr = node("tr"), td = node("td", undefined, "empty"), box = node("div", undefined, "list-empty"); td.colSpan = 4;
      box.append(ysIcon(state.pending ? "search" : "document"), node("p", !state.pending ? "还没有读取待办，点击右上角的刷新。" : "当前筛选下没有作业。"));
      td.append(box); tr.append(td); body.append(tr);
    }
    for (const item of items) {
      const key = keyOf(item), record = latest.get(key), status = rowStatus(record), due = dueInfo(item, now);
      const dueTone = due.overdue ? "bad" : due.days !== null && due.text === "今天" ? "warn" : "muted";
      const tr = node("tr"); tr.classList.toggle("selected", state.selected.has(key)); tr.classList.toggle("open", state.panel?.key === key);
      tr.classList.toggle("openable", status.open);
      const check = node("input", undefined, "ys-check"); check.type = "checkbox"; check.checked = state.selected.has(key); check.disabled = !!state.active || !eligible.has(key);
      check.setAttribute("aria-label", "选择 " + (item.title || key)); check.dataset.focus = "check-" + key;
      check.addEventListener("change", () => { check.checked ? state.selected.add(key) : state.selected.delete(key); renderList(); renderButtons(); });
      const checkCell = node("td", undefined, "check"); checkCell.append(check);
      const dueCell = node("td", undefined, "due");
      dueCell.append(node("span", item.due_local ? dateLabel(item.due_local) : "—", "due-date num"), node("span", due.text, "due-rel small " + dueTone));
      const titleCell = node("td", undefined, "title-cell");
      const title = status.open ? button(item.title || key, "link title", () => openPanel(record.job.id, key))
        : safeLink(item.url, item.title || key, "title plain") || node("div", item.title || key, "title");
      withLang(title, item.title); title.dataset.focus = "title-" + key;
      titleCell.append(title, withLang(node("span", item.course_name || "", "meta"), item.course_name));
      const mobileDue = node("div", undefined, "meta mobile-due" + (due.overdue ? " bad" : ""));
      mobileDue.append(item.due_local ? factRow([dateLabel(item.due_local), {text: due.text, cls: dueTone}]) : "无截止");
      mobileDue.hidden = window.innerWidth > 640; titleCell.append(mobileDue);
      if (record?.item?.error && !delivered(record.item)) titleCell.append(node("div", friendlyError(record.item.error), "note " + (status.tone === "bad" ? "bad" : "warn")));
      const stateCell = node("td", undefined, "state-cell"); stateCell.append(statusMark(status));
      tr.append(checkCell, dueCell, titleCell, stateCell);
      // The whole row answers a tap: the checkbox's cell ticks the box, the rest opens the answer when there is one.
      tr.addEventListener("click", event => {
        if (event.target.closest("a, button, input")) return;
        if (event.target.closest("td.check")) { if (!check.disabled) check.click(); return; }
        if (status.open && !String(window.getSelection?.() || "")) openPanel(record.job.id, key);
      });
      body.append(tr);
    }
  });
  const checked = [...eligible].filter(key => state.selected.has(key)).length;
  $("selectAll").checked = !!eligible.size && checked === eligible.size; $("selectAll").indeterminate = checked > 0 && checked < eligible.size;
  $("selectAll").disabled = !eligible.size || !!state.active;
  const skipped = items.length - eligible.size;
  $("listHint").replaceChildren(...(booting && !state.pending ? [] : [factRow([`显示 ${items.length} 项`, skipped ? `${skipped} 项已有答案，完成时会跳过` : null])]));
  $("clearSelection").hidden = !state.selected.size;
}
function renderButtons() {
  const count = targets().length, ai = aiEnabled();
  const g = $("generateButton");
  g.textContent = state.active ? "处理中…" : `${ai ? "完成" : "整理资料"}${state.selected.size ? "所选" : "当前"} ${count} 项`;
  g.disabled = !!state.active || state.busy || !count;
  setBusy(g, !!state.active || state.busy);
  $("refreshButton").disabled = !!state.active || state.busy || (!token && !BASE);
  $("modelSelect").disabled = !ai || !state.models; $("effortSelect").disabled = !ai || !state.models;
}

// The job card: rebuilt when what it says changes; between those, each second only its clock and bar move, so the
// sweep never restarts, a focused button keeps focus and the live region is not re-announced.
let jobView = null;
const STEP_NAMES = ["读资料", "Claude 作答", "核验", "生成答案"];
function progressBar(percent, label, tone, halted) {
  const bar = node("div", undefined, "ys-progress meter" + (percent === null ? " ys-meter--indeterminate indeterminate" : "") + (tone ? " ys-meter--" + tone : "") + (halted ? " is-halted" : ""));
  bar.setAttribute("role", "progressbar"); bar.setAttribute("aria-label", label);
  const fill = node("span", undefined, "ys-meter-fill"); bar.append(fill);
  if (percent === null) bar.style.setProperty("--sweep-delay", -(performance.now() % 1600) + "ms");
  else { bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100"); setPercent(bar, fill, percent); }
  return bar;
}
function setPercent(bar, fill, percent) { fill.style.width = percent.toFixed(1) + "%"; bar.setAttribute("aria-valuenow", String(Math.round(percent))); }
// halted: the worker is gone, so the step it was on is marked stopped rather than running.
function stepList(current, halted) {
  const list = node("ol", undefined, "ys-steps steps");
  STEP_NAMES.forEach((label, i) => {
    const done = i < current, now = i === current, stopped = now && halted;
    const li = node("li", undefined, "ys-step step" + (done ? " ys-step--done done" : stopped ? " ys-step--failed" : now ? " ys-step--current current" : ""));
    if (now) li.setAttribute("aria-current", "step");
    li.append(ysIcon(done ? "check" : stopped ? "close" : now ? "running" : "pending"), label,
      node("span", done ? "（已完成）" : stopped ? "（已停止）" : now ? "（进行中）" : "（未开始）", "ys-sr-only"));
    list.append(li);
  });
  return list;
}
function clock(text) { const span = node("span", undefined, "num"); span.append(document.createTextNode(text)); return span; }
function renderJob() {
  const card = $("jobCard"), job = state.jobs.find(x => x.id === (state.active || state.focus));
  if (!job || (!activeStates.has(job.status) && (job.kind === "refresh" || state.dismissed === job.id))) { card.hidden = true; card.replaceChildren(); jobView = null; return; }
  card.hidden = false;
  const now = Date.now(), s = jobSummary(job, now);
  if (job.kind === "refresh") {
    const key = "refresh " + job.id;
    if (jobView?.key === key) return;
    const top = node("div", undefined, "job-top"), heading = node("div", undefined, "job-heading"), title = node("h2", "正在从资料库读取待办", "job-title");
    title.id = "jobTitle"; heading.append(title); top.append(heading);
    card.replaceChildren(top, progressBar(null, "正在从资料库读取待办"));
    jobView = {key};
    return;
  }
  const current = s.current ? s.current.title || s.current.assignment_id : null;
  const titleText = s.active ? `正在处理 ${Math.min(s.done + 1, s.total)}/${s.total}${current ? " · " + current : ""}` : `${labels[job.status] || job.status} · ${s.total} 项`;
  const message = s.warning || (s.active ? job.items?.find(x => x === s.current)?.generation?.activity_label || job.message : job.message);
  // A job that is pausing, or whose worker is gone, is not heading for a finish: it shows no time left. Pausing says so
  // where 暂停 was; a dead worker will not pause either, so it shows neither.
  const dead = s.active && job.runtime?.worker_alive === false, stopping = s.active && job.status === "stopping";
  const pause = s.active && !stopping, pausing = stopping && !dead, retry = !s.active && ["completed_with_issues", "failed", "paused", "interrupted"].includes(job.status);
  const elapsed = s.elapsed !== null ? "用时 " + durationLabel(s.elapsed) : null;
  const remaining = s.remaining !== null && !stopping && !dead ? "约剩 " + durationLabel(s.remaining * 1000) : null;
  const key = JSON.stringify([job.id, job.status, titleText, message ? friendlyError(message) : "", !!s.warning, dead, s.step, s.active, s.percent === null, !!elapsed, !!remaining, pause, pausing, retry]);
  if (jobView?.key === key) {
    if (jobView.elapsed && elapsed) jobView.elapsed.firstChild.data = elapsed;
    if (jobView.remaining && remaining) jobView.remaining.firstChild.data = remaining;
    if (jobView.fill && s.percent !== null) setPercent(jobView.meter, jobView.fill, s.percent);
    return;
  }
  const top = node("div", undefined, "job-top"), heading = node("div", undefined, "job-heading"), side = node("div", undefined, "job-side small muted");
  const finished = s.active ? null : ({failed: "bad", completed_with_issues: "warn", paused: "warn", interrupted: "warn"})[job.status];
  if (finished) heading.append(ysIcon(finished === "bad" ? "error" : "warning", finished));
  const title = node("h2", undefined, "job-title"); title.id = "jobTitle";
  title.append(s.active ? factRow([`正在处理 ${Math.min(s.done + 1, s.total)}/${s.total}`, current && {text: current, lang: ja(current), wrap: true}])
    : factRow([labels[job.status] || job.status, `${s.total} 项`]));
  heading.append(title);
  const view = {key, elapsed: null, remaining: null, meter: null, fill: null};
  if (elapsed) side.append(view.elapsed = clock(elapsed));
  if (remaining) side.append(view.remaining = clock(remaining));
  const focusable = (b, name) => { b.dataset.focus = "job-" + name; return b; };
  if (pause) side.append(focusable(button("暂停", "ys-btn ys-btn--sm btn small", () => jobAction(job.id, "pause")), "pause"));
  if (pausing) { const note = node("span", "正在暂停…", "job-pausing"); note.tabIndex = -1; side.append(focusable(note, "pause")); }  // keeps the place of a focused 暂停
  if (retry) { const b = button("重试未完成", "ys-btn ys-btn--sm btn small", () => jobAction(job.id, "retry")); b.prepend(ysIcon("refresh")); side.append(focusable(b, "retry")); }
  if (!s.active) side.append(focusable(button("关闭", "ys-btn ys-btn--sm ys-btn--quiet link small", () => { state.dismissed = job.id; localStorage.setItem("classroom-dismissed", job.id); renderJob(); }), "dismiss"));
  top.append(heading, side);
  const parts = [top];
  if (s.active) {
    view.meter = progressBar(s.percent, "处理进度", s.warning ? (dead ? "bad" : "warn") : null, dead);
    view.fill = view.meter.firstChild;
    parts.push(view.meter, stepList(s.step, dead));
  }
  if (message) {
    if (s.warning) { const line = node("p", undefined, "job-warning small warn" + (dead ? " bad" : "")); line.append(ysIcon(dead ? "error" : "warning"), node("span", friendlyError(message))); parts.push(line); }
    else parts.push(node("p", friendlyError(message), "job-message small muted"));
  }
  keepFocus(card, () => card.replaceChildren(...parts));
  jobView = view;
}
// A state in the history: a pill where something did not finish, a blue dot while running, plain words when done.
function jobStateMark(status) {
  const text = labels[status] || status;
  const tone = status === "failed" ? "bad" : ["completed_with_issues", "paused", "interrupted"].includes(status) ? "warn" : null;
  const legacy = status === "completed" ? "good" : status === "failed" ? "bad" : "muted";
  if (tone) return node("span", text, `history-state ys-pill ys-pill--${tone} small ${legacy}`);
  if (activeStates.has(status)) return node("span", text, `history-state ys-status ys-status--info small ${legacy}`);
  return node("span", text, `history-state state-plain small ${legacy}`);
}
function renderHistory() {
  const jobs = state.jobs.filter(x => x.kind !== "refresh" || x.status === "failed");
  $("historyCount").textContent = jobs.length ? `${jobs.length} 次` : "";
  const list = $("historyList"), open = new Set([...list.querySelectorAll("details[open]")].map(x => x.dataset.job));
  keepFocus(list, () => {
    list.replaceChildren();
    for (const job of jobs.slice(0, 20)) {
      const li = node("li"), details = node("details", undefined, "history-item"), summary = node("summary");
      details.dataset.job = job.id; details.open = open.has(job.id);
      summary.dataset.focus = "history-" + job.id;
      const time = node("time", dateLabel(job.created_at, true)); time.dateTime = job.created_at || "";
      summary.append(time, factRow([kindLabels[job.kind] || job.kind, job.items?.length ? `${job.items.length} 项` : null, job.model ? modelName(job.model) : null,
        job.message ? {text: friendlyError(job.message), wrap: true} : null], "history-text"), jobStateMark(job.status), ysIcon("chevron-right"));
      details.append(summary);
      const events = node("ol", undefined, "events");
      for (const event of (job.events || []).slice(-40)) { const row = node("li"), at = node("time", dateLabel(event.at, true)); at.dateTime = event.at || ""; row.append(at, node("span", event.message)); events.append(row); }
      events.addEventListener("scroll", () => edgeFade(events, true), {passive: true});
      details.addEventListener("toggle", () => edgeFade(events, true));
      details.append(events); li.append(details); list.append(li);
    }
  });
}
function renderApiKey() {
  const status = state.apiKey, el = $("apiKeyStatus");
  el.replaceChildren(!status ? "正在读取…" : status.configured ? factRow(["已设置", `末尾 ${status.last4}`, status.updated_at ? "更新于 " + dateLabel(status.updated_at, true) : null]) : "尚未设置。没有密钥时不会调用 Claude。");
  $("apiKeyClear").hidden = !status?.configured;
}

// ---- jobs ----------------------------------------------------------------

let polling = null, pollInFlight = false, clockTimer = null;
function startPolling() {
  if (!polling) polling = setInterval(pollJob, 2000);
  if (!clockTimer) clockTimer = setInterval(renderJob, 1000);
}
async function pollJob() {
  if (!state.active) { clearInterval(polling); clearInterval(clockTimer); polling = clockTimer = null; return; }
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const job = await api("/api/jobs/" + state.active); state.error = null; updateJob(job);
    if (!activeStates.has(job.status)) {
      state.active = null; if (job.kind !== "refresh") state.focus = job.id;
      await loadState();
      const tone = job.status === "failed" ? "bad" : job.status === "completed" ? "ok" : undefined;
      toast(job.kind === "refresh" ? (job.status === "failed" ? friendlyError(job.message) : `已读取 ${state.pending?.assignments.length || 0} 项待办`) : friendlyError(job.message), tone);
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
  } catch (error) { toast(error.message, "bad"); return null; }
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
  } catch (error) { toast(error.message, "bad"); }
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
  try { state.apiKey = await api("/api/settings/api-key", "PUT", {key}); $("apiKeyInput").value = ""; renderApiKey(); renderIssues(); toast(key ? "API 密钥已验证并保存" : "API 密钥已删除", "ok"); }
  catch (error) { toast(error.message, "bad"); }
  finally { buttons.forEach(x => { x.disabled = false; }); }
}

// ---- answer panel --------------------------------------------------------

// Below 900px the panel covers the page (a full-width page of its own on a phone).
const panelCovers = () => matchMedia("(max-width: 900px)").matches;
function panelLoading() {
  const box = node("div", undefined, "panel-loading");
  box.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 4; i++) box.append(node("span", undefined, "ys-skeleton-bar"));
  return box;
}
function panelError(message) { const p = node("p", undefined, "panel-error bad"); p.append(ysIcon("error"), node("span", message)); return p; }
async function openPanel(jobId, key, tab = "answer") {
  const record = latestItems().get(key), job = state.jobs.find(x => x.id === jobId) || record?.job;
  const item = job?.items.find(x => keyOf(x) === key) || record?.item;
  if (!item) return;
  const reopening = state.panel?.key === key;
  state.panel = {jobId, key, item, data: null, tab, showAll: false};
  $("workspace").classList.add("with-panel"); $("panel").hidden = false; renderList();
  withLang($("panelTitle"), item.title).textContent = item.title || key; $("panelMeta").textContent = "正在读取…";
  $("panelActions").replaceChildren(); $("panelNotes").replaceChildren(); $("panelTabs").replaceChildren(); $("panelContent").replaceChildren(panelLoading());
  if (panelCovers() && !reopening) { $("panel").scrollTop = 0; $("panelTitle").focus({preventScroll: true}); }
  if (!item.package) { $("panelMeta").textContent = item.course_name || ""; $("panelContent").replaceChildren(node("p", friendlyError(item.error || "这项作业还没有资料包。"), "content-empty warn")); return; }
  setBusy($("panel"), true);
  try {
    const data = await api(`/api/jobs/${jobId}/items/${key}`);
    if (state.panel?.key !== key) return;
    state.panel.data = data; renderPanel();
  } catch (error) { if (state.panel?.key === key) { $("panelMeta").textContent = ""; $("panelContent").replaceChildren(panelError(error.message)); } }
  finally { if (state.panel?.key === key) setBusy($("panel"), false); }
}
function closePanel() {
  const key = state.panel?.key;
  clearImages(); state.panel = null; $("panel").hidden = true; $("workspace").classList.remove("with-panel"); renderList();
  if (key) $("assignmentList").querySelector(`[data-focus="${CSS.escape("title-" + key)}"]`)?.focus({preventScroll: true});
}
function renderPanel() {
  const {data, jobId, key, item} = state.panel, g = data.generation, manifest = data.manifest, assignment = manifest.assignment;
  withLang($("panelTitle"), assignment.title || item.title).textContent = assignment.title || item.title || key;
  const meta = [manifest.course_name ? {text: manifest.course_name, lang: ja(manifest.course_name), wrap: true} : null];
  if (g?.status === "completed") meta.push(modelName(g.model), `${efforts[g.reasoning_effort] || g.reasoning_effort || "默认"}强度`, Number.isFinite(g.cost_usd) ? `约 ${g.cost_usd.toFixed(3)} 美元` : null);
  meta.push(data.text["draft.md"] ? "未提交" : g?.status === "not_invoked" ? "只整理了资料" : "还没有答案");
  $("panelMeta").replaceChildren(factRow(meta));

  const actions = $("panelActions"); actions.replaceChildren();
  const form = data.form_fill?.forms?.[0], doc = data.document_fill?.documents?.[0];
  const original = externalLink(doc?.url || form?.url, doc ? "打开已填入的文档" : "打开预填表单", "ys-btn ys-btn--sm ys-btn--primary btn small primary");
  if (original) actions.append(original);
  if (data.text["draft.md"]) { const word = button("下载 Word", "ys-btn ys-btn--sm btn small" + (original ? "" : " ys-btn--primary primary"), () => downloadAnswer(jobId, key)); word.prepend(ysIcon("download")); actions.append(word); }
  const classroom = externalLink(assignment.alternateLink, "Classroom", "ys-btn ys-btn--sm btn small"); if (classroom) actions.append(classroom);
  actions.append(button("补充后重做", "ys-btn ys-btn--sm btn small", () => openSupplement(jobId, key)));
  actions.append(button("课程规则", "ys-link link small", () => openPolicy(manifest.course_id, manifest.course_name)));
  actions.append(button("证据包", "ys-link link small", () => downloadEvidence(jobId, key)));

  // Notes: one banner, tinted by the most serious line in it.
  const notes = $("panelNotes"); notes.replaceChildren();
  const lines = [];
  if (data.policy_changed) lines.push(["warn", "补充说明或课程规则已变化，需要重做才会生效。"]);
  if (form) {
    const missing = formContentGaps(data.form_fill), user = formUserInputGaps(data.form_fill, ["user_input_fields"]), native = formUserInputGaps(data.form_fill, ["unsupported_fields"]);
    if (missing.length) lines.push(["warn", `${missing.length} 个必答题没有答案：` + missing.map(x => x.title || x.entry_id).join("；")]);
    if (user.length) lines.push(["warn", `${user.length} 栏需要你本人填写，点“补充后重做”填写后会自动带入表单。`]);
    if (native.length) lines.push(["warn", `${native.length} 栏需要在原表单里操作：` + native.map(x => x.title || x.entry_id).join("；")]);
    lines.push(["info", "预填链接会带入候选答案；在 Google 里改过的草稿请从原表单继续。提交由你操作。"]);
  }
  // The note counts what the 待核对 tab counts: the questions, and any 资料缺口 listed under them.
  const questions = data.review?.questions || [], gaps = (manifest.warnings || []).length;
  if (questions.length) lines.push(["warn", `${questions.length} 条需要你核对${gaps ? `、${gaps} 处资料缺口` : ""}，见“待核对”。`]);
  if (g?.error && g.status !== "completed") lines.push(["bad", friendlyError(g.error)]);
  if (lines.length) {
    const worst = ["bad", "warn", "info"].find(tone => lines.some(([t]) => t === tone));
    const box = node("div", undefined, `note-box ys-alert ys-alert--${worst}`), body = node("div", undefined, "ys-alert-body note-lines");
    for (const [, line] of lines) body.append(node("p", line));
    box.append(ysIcon(worst === "bad" ? "error" : worst === "warn" ? "warning" : "info"), body);
    notes.append(box);
  }

  const tabs = $("panelTabs"); tabs.replaceChildren();
  for (const [id, label, count] of [["answer", "答案"], ["assignment", "原题"], ["checklist", "检查表"], ["review", "待核对", questions.length + gaps], ["sources", "来源"], ["record", "记录"]]) {
    const b = button(count === undefined ? label : label + " ", "", () => { state.panel.tab = id; renderPanelTab(); });
    if (count !== undefined) b.append(node("span", String(count), "ys-seg-count"));
    b.setAttribute("role", "tab"); b.dataset.tab = id; b.id = "panelTab-" + id; b.setAttribute("aria-controls", "panelContent"); tabs.append(b);
  }
  renderPanelTab();
}
// The answer as the exporter lays it out (answer_document.py): body paragraphs indented, the identity line to the right.
const IDENTITY = /学籍番号|学生番号|氏名|学科|クラス|学年/;
const NO_INDENT = /^(?:[①-⑳【〔(（[]|問|Q\d|[A-Za-z]\)|\d+[)）．.])/;
function draftBlocks(blocks) {
  const box = node("div", undefined, "answer draft ys-mincho");
  box.lang = blocks.some(block => JAPANESE.test(block.text || (block.rows || []).flat().join("") || (block.items || []).map(x => x.text).join(""))) ? "ja" : "en";
  const body = blocks[0]?.kind === "heading" && blocks[0].level === 1 ? blocks.slice(1) : blocks;
  for (const block of blocks) {
    if (block.kind === "table") {
      const wrap = node("div", undefined, "draft-table"), table = node("table"), head = node("thead"), rows = node("tbody");
      block.rows.forEach((values, index) => {
        const tr = node("tr");
        for (const value of values) { const cell = node(index === 0 ? "th" : "td", value); if (index === 0) cell.scope = "col"; tr.append(cell); }
        (index === 0 ? head : rows).append(tr);
      });
      table.append(head, rows); wrap.append(table); box.append(wrap);
    }
    else if (block.kind === "list") { const list = node(block.ordered ? "ol" : "ul"); for (const item of block.items || []) { const li = inlineRuns("li", item); if (item.number != null) li.value = item.number; list.append(li); } box.append(list); }
    else if (block.kind === "heading") box.append(node(`h${Math.min(block.level || 2, 3)}`, block.text));
    else {
      const p = inlineRuns("p", block), text = block.text || "";
      if (body.indexOf(block) === 0 && text.length < 120 && IDENTITY.test(text)) p.className = "identity";
      else if (!text.includes("\n") && !NO_INDENT.test(text)) p.className = "indent";
      box.append(p);
    }
  }
  return box;
}
// Classroom's own records (coursework, materials, announcements) arrive as "key: value" lines. The title
// and the teacher's text read as prose; the machine fields (workType, dueDate, materials …) stay verbatim
// in a code block. Dates are left as Classroom sends them (UTC), so nothing is converted or dropped.
const CLASSROOM_KINDS = new Set(["coursework", "material", "announcement"]);
const SOURCE_KINDS = {announcement: "公告", coursework: "作业", material: "资料"};
function classroomSource(source) {
  if (!CLASSROOM_KINDS.has(source.source_kind)) return null;
  const fields = [];
  for (const line of String(source.text).split("\n")) {
    const m = line.match(/^([a-z][A-Za-z0-9_]*): ?(.*)$/);
    if (m) fields.push([m[1], m[2]]);
    else if (fields.length) fields.at(-1)[1] += "\n" + line;
    else return null;
  }
  const parts = [], machine = [];
  for (const [key, value] of fields) {
    if (key === "title" && value.trim() === (source.title || "").trim()) continue;  // the row already names it
    if (key === "title") parts.push(withLang(node("strong", value, "source-title"), value));
    else if (key === "description" || key === "text") parts.push(withLang(node("pre", value.replace(/\s+$/, ""), "content-text"), value));
    else machine.push(`${key}: ${value}`);
  }
  if (machine.length) {
    const code = machine.join("\n");
    parts.push(node("pre", code.length > 1200 ? code.slice(0, 1200) + "…" : code, "content-text ys-mono source-code"));
  }
  return parts.length ? parts : null;
}
// A Google Form reaches the sources as JSON (form_read.py): a question, or the form's own description and sections.
// Each is set out as the form shows it; JSON of any other shape stays readable as code. Null when the text is not JSON.
function formSource(text) {
  let value;
  try { value = JSON.parse(text); } catch { return null; }
  if (!value || typeof value !== "object") return null;
  const parts = [], prose = (words, cls) => withLang(node("p", words, cls), words);
  if (!Array.isArray(value) && "item_id" in value) {
    if (value.title) parts.push(withLang(node("strong", value.title, "source-title"), value.title));
    if (value.description) parts.push(prose(value.description, "muted content-text"));
    const type = questionTypes[value.type], choices = (value.fields || []).flatMap(field => field.choices || []);
    if (type || choices.length)
      parts.push(factRow([type && {text: type, cls: "fact-type"}, ...choices.map(choice => ({text: choice, lang: ja(choice), wrap: true}))], "small source-choices fact-row--slash"));
  } else if (!Array.isArray(value) && Array.isArray(value.sections)) {
    if (value.description) parts.push(prose(value.description, "content-text"));
    for (const section of value.sections) {
      parts.push(factRow([Number.isFinite(section.page) ? `第 ${section.page} 页` : null, section.title && {text: section.title, lang: ja(section.title), wrap: true}], "small source-choices"));
      if (section.description) parts.push(prose(section.description, "muted content-text"));
    }
    for (const warning of value.warnings || []) parts.push(prose(warning, "small muted content-text"));
    if (!parts.length && value.title) parts.push(withLang(node("strong", value.title, "source-title"), value.title));
  } else {
    const code = JSON.stringify(value, null, 2);
    parts.push(node("pre", code.length > 1200 ? code.slice(0, 1200) + "…" : code, "content-text ys-mono source-code"));
  }
  return parts;
}
function renderPanelTab() {
  clearImages();
  const {data, jobId, key, tab} = state.panel, content = $("panelContent"); content.replaceChildren();
  for (const b of $("panelTabs").children) { const on = b.dataset.tab === tab; b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1; }
  content.setAttribute("aria-labelledby", "panelTab-" + tab);
  content.scrollTop = 0;
  // On the full-screen panel a page scrolled past the tabs goes back to them, so the new tab starts at its top.
  // The tabs are sticky, so their resting place is the height of everything above them.
  const panel = $("panel"), tabsBox = $("panelTabs").parentElement;
  if (panelCovers()) {
    const above = [...panel.children].slice(0, [...panel.children].indexOf(tabsBox)).reduce((sum, el) => sum + el.offsetHeight, 0);
    const top = above - panel.querySelector(".panel-bar").offsetHeight;
    if (panel.scrollTop > top) panel.scrollTop = Math.max(0, top);
  }
  const g = data.generation, manifest = data.manifest;
  if (tab === "answer") {
    if (data.answer_document?.blocks?.length) content.append(draftBlocks(data.answer_document.blocks));
    else if (data.text["draft.md"]) { const box = node("div", undefined, "answer draft ys-mincho"); box.lang = ja(data.text["draft.md"]) || "en"; box.append(node("pre", data.text["draft.md"])); content.append(box); }
    else { const box = node("div", undefined, "answer"); box.append(node("p", g?.status === "not_invoked" ? "这次没有调用 AI，只整理了题目和资料。" : "还没有通过核验的答案。", "content-empty muted")); content.append(box); }
  }
  if (tab === "assignment") {
    const assignment = manifest.assignment;
    content.append(withLang(node("pre", assignment.description || "这项作业没有文字说明，请看附件。", "content-text"), assignment.description));
    const links = node("div", undefined, "actions attachments");
    for (const material of assignment.materials || []) { const ref = material.driveFile?.driveFile || material.link || material.youtubeVideo || material.form; const link = ref && externalLink(ref.alternateLink || ref.url || ref.formUrl, ref.title || "附件", "ys-btn ys-btn--sm btn small"); if (link) links.append(withLang(link, ref.title)); }
    if (links.children.length) content.append(links);
    for (const form of data.requirements?.response_forms || []) {
      const heading = node("h3"); heading.append(withLang(node("span", form.title), form.title), `（${form.page_count} 页，${form.question_count} 栏）`); content.append(heading);
      for (const source of (data.requirements.sources || []).filter(x => x.source_id === form.source_id)) {
        try {
          const question = JSON.parse(source.text);
          if (!question.item_id) continue;
          const row = node("div", undefined, "source"); row.append(node("div", `第 ${question.page} 页`, "locator"), withLang(node("strong", question.title, "source-title"), question.title));
          if (question.description) row.append(withLang(node("p", question.description, "muted content-text"), question.description));
          const choices = (question.fields || []).flatMap(field => field.choices || []);
          if (choices.length) { const line = factRow(choices.map(choice => ({text: choice, lang: ja(choice), wrap: true})), "small source-choices fact-row--slash"); row.append(line); }
          content.append(row);
        } catch { content.append(withLang(node("pre", source.text, "content-text"), source.text)); }
      }
    }
    const profile = manifest.student_profile || {}, identity = [["学籍番号", "student_id"], ["氏名", "name"], ["学科", "department"], ["クラス", "class_name"]].filter(([, field]) => profile[field]);
    content.append(node("h3", "作答使用的身份"));
    if (identity.length) { const dl = node("dl", undefined, "facts ys-facts"); dl.lang = "ja"; for (const [label, field] of identity) dl.append(node("dt", label), node("dd", profile[field])); content.append(dl); }
    else content.append(node("p", "没有设置学生资料，身份栏会留空。", "content-empty muted small"));
  }
  if (tab === "checklist") {
    const checks = data.review?.requirement_checks || [];
    if (!checks.length) content.append(node("p", "还没有检查表。", "content-empty muted"));
    // Met requirements stay plain; a pill marks the ones that need you.
    const tone = {met: ["已满足", null], partial: ["部分满足", "warn"], unmet: ["未满足", "bad"], needs_user: ["待你确认", "warn"]};
    for (const check of checks) {
      const row = node("div", undefined, "source check-row"), [text, cls] = tone[check.status] || [check.status, null];
      row.append(node("span", text, cls ? `check-state ys-pill ys-pill--${cls} small ${cls}` : "check-state state-plain small muted"), withLang(node("pre", check.requirement, "content-text"), check.requirement));
      if (check.draft_location) row.append(node("p", check.draft_location, "locator muted small"));
      content.append(row);
    }
  }
  if (tab === "review") {
    const questions = data.review?.questions || [], warnings = manifest.warnings || [];
    if (!questions.length && !warnings.length) content.append(node("p", "没有记录待核对的事项，内容仍需你审阅。", "content-empty muted"));
    for (const question of questions) { const row = node("div", undefined, "source"); row.append(node("p", question, "content-text")); content.append(row); }
    if (warnings.length) content.append(node("h3", "资料缺口"));
    for (const warning of warnings) { const row = node("div", undefined, "source"); row.append(withLang(node("div", warning.title || warning.source, "locator"), warning.title || warning.source), node("p", warning.error, "content-text")); content.append(row); }
  }
  if (tab === "sources") {
    const used = new Set(g?.used_evidence_ids || []), all = data.evidence?.sources || [];
    const ordered = [...all.filter(x => used.has(x.id)), ...all.filter(x => !used.has(x.id))];
    const shown = state.panel.showAll ? ordered : ordered.slice(0, Math.max(used.size, 30));
    content.append(node("p", `${used.size} 段被答案引用，共 ${all.length} 段资料。`, "content-summary muted small"));
    for (const source of shown) {
      const row = node("div", undefined, "source");
      // an announcement has no title of its own (the API names it after its kind); Classroom text spans read in Chinese
      const name = source.title && source.title !== source.source_kind ? source.title : SOURCE_KINDS[source.source_kind] || source.title || source.source_kind;
      const locator = String(source.locator || "").replace(/^Classroom text characters (\d+)-(\d+)$/, "Classroom 原文第 $1–$2 字") || null;
      row.append(factRow([{text: name, lang: ja(name), wrap: true}, locator, used.has(source.id) ? {text: "已引用", cls: "cited"} : null], "locator"),
        ...(formSource(source.text) || classroomSource(source) || [withLang(node("pre", source.text.length > 1200 ? source.text.slice(0, 1200) + "…" : source.text, "content-text"), source.text)]));
      const links = node("div", undefined, "source-links");
      const link = externalLink(source.url, "原来源", "ys-link small"); if (link) links.append(link);
      if (source.image_path) {
        const show = button("查看页面图像", "ys-link link small", async () => {
          show.disabled = true;
          try { const blob = await api(`/api/jobs/${jobId}/items/${key}/image/${source.id}`), url = URL.createObjectURL(blob); state.images.push(url); const img = node("img"); img.src = url; img.alt = `${source.title} ${source.locator}`; row.append(img); show.remove(); }
          catch (error) { toast(error.message, "bad"); show.disabled = false; }
        });
        links.append(show);
      }
      if (links.children.length) row.append(links);
      content.append(row);
    }
    if (shown.length < ordered.length) content.append(button(`显示其余 ${ordered.length - shown.length} 段`, "ys-btn ys-btn--sm show-more btn small", () => { state.panel.showAll = true; renderPanelTab(); }));
  }
  if (tab === "record") {
    if (!g || g.status === "not_invoked") { content.append(node("p", "这次没有调用 AI。", "content-empty muted")); return; }
    const usage = g.token_usage || {}, dl = node("dl", undefined, "facts ys-facts");
    // A record left at "running" by a run that is over (the job ended or was interrupted) was interrupted.
    const live = activeStates.has(state.jobs.find(x => x.id === jobId)?.status) && state.panel.item?.status !== "interrupted";
    const status = g.status === "completed" ? "已完成并通过核验" : g.status === "running" && !live ? labels.interrupted : labels[g.status] || g.status;
    const rows = [["状态", status], ["模型", modelName(g.model || g.requested_model)], ["推理强度", efforts[g.reasoning_effort] || g.reasoning_effort || "默认"],
      ["计费", g.api_key_source === "ANTHROPIC_API_KEY" ? "Claude API 密钥" : g.api_key_source || "—"], ["费用", Number.isFinite(g.cost_usd) ? `${g.cost_usd.toFixed(4)} 美元` : "—"],
      ["用时", Number.isFinite(g.duration_ms) ? durationLabel(g.duration_ms) : "—"], ["输入 / 输出", usage.input_tokens !== undefined ? `${usage.input_tokens + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0)} / ${usage.output_tokens} tokens` : "—"],
      ["送入资料", `${g.input?.source_ids?.length || 0} 段文字 · ${g.input?.images?.length || 0} 张页图`], ["开始", dateLabel(g.started_at, true)], ["结束", g.finished_at ? dateLabel(g.finished_at, true) : "—"]];
    for (const [label, value] of rows) dl.append(node("dt", label), node("dd", value));
    content.append(dl);
    if (g.error) { const p = panelError(friendlyError(g.error)); p.classList.add("record-error"); content.append(p); }
    if (data.text["summary.md"]) content.append(node("h3", "模型说明"), node("pre", data.text["summary.md"], "content-text"));
  }
}
function clearImages() { for (const url of state.images) URL.revokeObjectURL(url); state.images = []; }
async function downloadAnswer(jobId, key) {
  try {
    const job = await api(`/api/jobs/${jobId}/items/${key}/export-answer`, "POST", {}); updateJob(job);
    const blob = await api(`/api/jobs/${jobId}/items/${key}/answer-docx`), item = job.items.find(x => keyOf(x) === key);
    saveBlob(blob, (item?.title || "答案").replace(/[\\/:*?"<>|]/g, "_") + "-审阅稿.docx");
    toast("Word 已下载；核对修改后再手动提交。", "ok");
  } catch (error) { toast(error.message, "bad"); }
}
async function downloadEvidence(jobId, key) {
  try { saveBlob(await api(`/api/jobs/${jobId}/items/${key}/download`), `证据包-${key.replace(":", "-")}.zip`); } catch (error) { toast(error.message, "bad"); }
}

// ---- course rules --------------------------------------------------------

async function openPolicy(courseId, courseName) {
  try {
    const policy = await api("/api/policies/" + courseId); state.policyCourse = courseId;
    $("policyTitle").replaceChildren(withLang(node("span", courseName || "课程"), courseName), " · 课程规则");
    $("policyUse").value = policy.ai_use; $("policyEvidence").value = policy.policy_evidence; $("policyLimits").value = policy.limitations;
    $("policyDisclosure").value = policy.disclosure; $("personalFacts").value = policy.personal_facts.join("\n"); $("policyError").hidden = true;
    $("policyDialog").showModal();
  } catch (error) { toast(error.message, "bad"); }
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
    const id = `answer-input-${state.supplement.editors.length}`, checkboxes = question.type === "checkbox" && field.choices.length;
    // 第 N 页 · the question (in Japanese) · 必填 or 选填
    const caption = [`第 ${question.page} 页 · `, withLang(node("span", question.title), question.title), field.required ? "（必填）" : "（选填）"];
    const group = node(checkboxes ? "fieldset" : "div", undefined, checkboxes ? "choice-set answer-field" : "ys-field answer-field");
    if (checkboxes) { const legend = node("legend"); legend.append(...caption); group.append(legend); }
    else { const label = node("label", undefined, "ys-field-label"); label.append(...caption); label.htmlFor = id; group.append(label); }
    if (question.description) group.append(withLang(node("p", question.description, "ys-field-help small muted"), question.description));
    let control, read;
    if (["single_choice", "dropdown", "scale"].includes(question.type) && field.choices.length) {
      control = node("select"); const empty = node("option", "请选择"); empty.value = ""; control.append(empty);
      for (const value of field.choices) { const option = withLang(node("option", value), value); option.value = value; control.append(option); }
      control.value = supplied?.values?.[0] || ""; read = () => control.value ? [control.value] : [];
      const wrap = node("span", undefined, "ys-select"); wrap.append(control, ysIcon("chevron-down")); control.id = id; group.append(wrap);
    } else if (["short_text", "paragraph"].includes(question.type)) {
      control = node(question.type === "paragraph" ? "textarea" : "input", undefined, "ys-input"); if (question.type === "paragraph") control.rows = 3; else control.type = "text";
      control.maxLength = 20000; control.value = supplied?.values?.[0] || ""; read = () => control.value.trim() ? [control.value.trim()] : [];
      control.id = id; group.append(control);
    } else if (checkboxes) {
      const options = []; group.id = id;
      for (const value of field.choices) { const option = node("label", undefined, "ys-check-label inline"), input = node("input", undefined, "ys-check"); input.type = "checkbox"; input.value = value; input.checked = supplied?.values?.includes(value) || false; option.append(input, withLang(node("span", value), value)); group.append(option); options.push(input); }
      read = () => options.filter(x => x.checked).map(x => x.value);
    } else { group.append(node("p", "这一题需要在原表单里填写。", "answer-native small muted")); content.append(group); continue; }
    content.append(group);
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
    withLang($("supplementTitle"), item.title).textContent = item.title || key;
    $("supplementText").value = supplement.text || ""; $("supplementFacts").value = (supplement.personal_facts || []).join("\n");
    renderAnswerInputs(data); updateSupplementActions();
    const notes = $("supplementQuestions"); notes.replaceChildren();
    if (data?.review?.questions?.length) { const list = node("ul"); for (const question of data.review.questions) list.append(node("li", question)); notes.append(node("h3", "上次留下的待核对事项"), list); }
    $("supplementAi").checked = job.ai_confirmed !== false; $("supplementError").hidden = true;
    const urls = [...new Set([...(data?.requirements?.response_forms || []).map(x => x.url), ...(data?.manifest?.warnings || []).map(x => x.url)].filter(url => /^https:\/\/docs\.google\.com\/forms\//.test(url || "")))];
    const links = $("restrictedFormLinks"); links.replaceChildren();
    for (const url of urls) { const link = externalLink(url, "打开原表单", "ys-btn ys-btn--sm btn small"); if (link) links.append(link); }
    $("captureUrl").value = urls[0] || ""; $("captureScript").value = ""; $("browserQuestionSection").open = false;
    $("supplementDialog").showModal();
  } catch (error) { toast(error.message, "bad"); }
}
async function supplementAction(control, operation) {
  control.disabled = true; $("supplementError").hidden = true;
  try { await operation(); } catch (error) { $("supplementError").textContent = error.message; $("supplementError").hidden = false; $("supplementError").parentElement.scrollIntoView({block: "nearest"}); }
  finally { control.disabled = false; }
}
async function saveFormInputs(apply, close = true) {
  const {jobId, key, editors} = state.supplement;
  const answers = editors.map(({read, ...mapping}) => ({...mapping, values: read()})).filter(x => x.values.length);
  const result = await api(`/api/jobs/${jobId}/items/${key}/answers`, "POST", {answers, apply});
  if (!close) return result;
  $("supplementDialog").close(); await loadState();
  toast(result.apply_queued ? "回答已保存，当前任务结束后自动带入表单" : apply ? "回答已保存，正在带入表单" : "回答已保存", "ok");
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
  toast(continueRun ? "已保存，正在重做这项作业" : "补充已保存，下次处理时使用", "ok");
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
    $("policyDialog").close(); toast("课程规则已保存；重做这门课的作业时生效。", "ok");
    if (state.panel?.data) openPanel(state.panel.jobId, state.panel.key, state.panel.tab);
  } catch (error) { $("policyError").textContent = error.message; $("policyError").hidden = false; $("policyError").parentElement.scrollIntoView({block: "nearest"}); }
  finally { submit.disabled = false; }
});
for (const id of ["supplementText", "supplementFacts"]) $(id).addEventListener("input", updateSupplementActions);
$("saveSupplement").addEventListener("click", event => supplementAction(event.currentTarget, () => saveSupplement(false)));
$("supplementForm").addEventListener("submit", event => { event.preventDefault(); supplementAction(event.submitter, () => saveSupplement(true)); });
$("importQuestions").addEventListener("click", event => supplementAction(event.currentTarget, async () => {
  const {jobId, key} = state.supplement;
  const result = await api(`/api/jobs/${jobId}/items/${key}/questions`, "POST", {url: $("captureUrl").value, question_script: $("captureScript").value});
  $("captureScript").value = ""; toast(`已导入 ${result.page_count} 页、${result.question_count} 栏题目，点“保存并重做”继续。`, "ok");
}));
// Tabs follow the arrow keys, as tabs do.
$("panelTabs").addEventListener("keydown", event => {
  const tabs = [...$("panelTabs").children], at = tabs.indexOf(document.activeElement);
  const next = {ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: tabs.length - 1}[event.key];
  if (at < 0 || next === undefined || !state.panel?.data) return;
  event.preventDefault();
  const tab = tabs[(next + tabs.length) % tabs.length];
  state.panel.tab = tab.dataset.tab; renderPanelTab(); tab.focus();
});
$("filters").parentElement.addEventListener("scroll", event => edgeFade(event.currentTarget), {passive: true});
window.addEventListener("resize", () => {
  for (const el of document.querySelectorAll(".mobile-due")) el.hidden = window.innerWidth > 640;
  edgeFade($("filters").parentElement); revealSegment($("filters").parentElement, $("filters").querySelector('[aria-pressed="true"]'));
});

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
if (token || BASE) loadState().then(() => { loadModels(); syncWhenIdle(); }).catch(error => { booting = false; state.error = error.message; renderHeader(); renderIssues(); renderList(); });
