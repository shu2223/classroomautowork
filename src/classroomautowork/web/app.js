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
  grid: "单选题网格", checkbox_grid: "复选框网格", date: "日期", time: "时间", file_upload: "文件上传", image: "图片"};
// The label of a form's "Other" option in the question data (form_read.OTHER_CHOICE); an answer that picks it is this
// label followed by the words written into it.
const OTHER_CHOICE = "[その他の自由記入]";
const efforts = {low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高"};
// How an OpenAI-compatible server was asked for its answer (generation.json structured_output_mode).
const OUTPUT_MODES = {json_schema: "JSON Schema（严格）", json_object: "JSON 对象（接口不支持 JSON Schema）", none: "只靠提示词（接口不支持 JSON 格式）"};
const FILTERS = [["all", "全部"], ["week", "7 天内"], ["nodue", "无截止"], ["review", "待审阅"], ["overdue", "已逾期"]];
const STALE_MS = 3 * 60 * 1000;

let token = location.hash.slice(1);
if (token) { sessionStorage.setItem("classroom-access", token); history.replaceState(null, "", location.pathname); }
else token = sessionStorage.getItem("classroom-access") || "";

const state = {pending: null, jobs: [], resultJobs: [], active: null, focus: null, dismissed: localStorage.getItem("classroom-dismissed") || "",
  selected: new Set(), filter: localStorage.getItem("classroom-filter") || "all", busy: false, models: null, apiKey: null, timezone: "Asia/Tokyo",
  panel: null, supplement: null, images: [], error: null,
  // provider: {active, anthropic, anthropic_compatible, openai, tests} from the server; providerView: the provider the
  // settings dialog shows, when it is not (yet) the active one; suggestions: the model ids each endpoint listed when saved.
  provider: null, providerView: null, suggestions: {openai: [], anthropic_compatible: []},
  // Connection tests: the provider being tested now, and this page's latest result per provider (shown in its group).
  testing: null, tests: {}, settingsBusy: false,
  // 设置 → 学校账号: {state: signed_out|signed_in|expired|mismatch, email, verified_at, …} from the server.
  schoolLogin: null};
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
// A required file upload is no gap: it can never be prefilled, so uploading it is the student's own step in the form (completion.py).
function formUserInputGaps(fill, keys = ["user_input_fields", "unsupported_fields"]) {
  return keys.flatMap(key => (Object.hasOwn(fill || {}, key) ? fill[key] || [] : (fill?.forms || []).flatMap(form => form[key] || []))
    .filter(field => !(key === "unsupported_fields" && field.type === "file_upload"))).filter(field => field.required);
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

// Provider presentation; exercised by tests/provider_view.js.
// The provider a job answered with: recorded since providers exist; earlier AI review jobs were Claude's. A fill job
// answers nothing itself, so it has none.
function jobProvider(job) { return job?.provider || (job?.ai_confirmed && job.kind !== "fill" ? "anthropic" : null); }
// How the page names each provider.
const PROVIDER_LABELS = {anthropic: "Claude API", anthropic_compatible: "Anthropic 兼容接口", openai: "OpenAI 兼容接口"};
// The provider of a generation record (generation.json names the endpoints "openai_compatible" and "anthropic_compatible").
function recordProvider(g) { return g?.provider === "openai_compatible" ? "openai" : g?.provider === "anthropic_compatible" ? "anthropic_compatible" : "anthropic"; }
// A model as the page names it: an OpenAI-compatible model by its own id; a Claude model (the Claude API's, or one an
// Anthropic-compatible endpoint serves) by its display name, whichever provider the toolbar lists now (the catalog carries
// Claude's names in claude_names).
function modelLabel(id, provider, catalog) {
  if (!id || provider === "openai") return id;
  const own = (catalog?.provider || "anthropic") === (provider || "anthropic") ? catalog?.models?.find(x => x.model === id) : null;
  return own?.displayName || catalog?.claude_names?.[id] || id;
}
// The job card's answering step names the service: the job's own; for a job that answers nothing, the active one.
function stepNames(job, active) { return ["读资料", (jobProvider(job) || active) === "openai" ? "模型作答" : "Claude 作答", "核验", "生成答案"]; }
// What 补充后重做 sends. The toolbar's choice when the toolbar lists the active provider's models (默认 is "" and sends no
// effort); without that list, the source job's own model and effort, only if that job used the active provider; else
// nothing, so the server takes the active provider's defaults.
function redoChoice(toolbar, job, active) {
  if (toolbar.provider === active) return {model: toolbar.model || null, effort: toolbar.effort || null};
  return jobProvider(job) === active ? {model: job?.model || null, effort: job?.effort || null} : {model: null, effort: null};
}
// The line above 保存并重做: the model the redo uses, and the provider too when the answer came from the other one.
function redoLine(name, active, from) {
  if (!from || from === active) return `重做会使用 ${name}。`;
  const label = PROVIDER_LABELS[active] || PROVIDER_LABELS.anthropic;
  return `重做会改用 ${label}${/[A-Za-z]$/.test(label) ? " " : ""}的 ${name}。`;
}
// Why page images were left out of an endpoint's request (generation.json degradations).
function imagesDroppedWhy(g) { return (g?.degradations || []).includes("images_size") ? "请求超出了接口的上限" : "接口不接受图片"; }
// The answer panel's note when the endpoint never saw some page images: those pages rest on their extracted text alone.
// (Claude Code drops the images an endpoint refuses by itself and carries on; its record says so in degradations.)
function imagesNote(g) {
  if (recordProvider(g) !== "openai" && !Number.isFinite(g?.images_dropped) && (g?.degradations || []).includes("images"))
    return "接口不接受页面图片，Claude Code 去掉图片后作答，这些页只按提取的文字作答；图表、截图和扫描内容请对照原资料核对。";
  const count = g?.images_dropped;
  if (!Number.isFinite(count) || count <= 0) return null;
  return `${count} 张页面图片没有发给模型（${imagesDroppedWhy(g)}），这些页只按提取的文字作答；图表、截图和扫描内容请对照原资料核对。`;
}
// The 记录 tab's 模型 row: the model the server reported, and beside it the chosen one when the server named another
// (an endpoint may serve another model than the one asked for; Claude Code records the one its replies named).
function recordModels(g, label) {
  const shown = label(g.model || g.requested_model);
  return g.model && g.requested_model && g.model !== g.requested_model ? [shown, "所选 " + label(g.requested_model)] : [shown];
}
// The 记录 tab's 推理强度 row: the chosen effort, and what the server took of it (generation.json effort, degradations).
function effortRecord(g, names) {
  const name = x => names[x] || x, chosen = g?.reasoning_effort;
  if (!chosen) return "默认";
  const relaxed = new Set(g.degradations || []), sent = g.effort?.sent;
  if (relaxed.has("reasoning_effort_lowered") && sent && sent !== chosen) return `${name(chosen)} → ${name(sent)}（接口不支持${name(chosen)}）`;
  if (relaxed.has("reasoning_effort")) return name(chosen) + "（接口不支持，未发送）";
  return name(chosen);
}
// The 记录 tab's 输出上限 row (generation.json output_limit {requested, sent, param}); null when the record has none.
function limitRecord(g) {
  const limit = g?.output_limit;
  if (!limit) return null;
  // No limit was set, but the endpoint refused Claude Code's own default and named its maximum.
  if (!limit.requested && limit.sent) return `默认 → ${limit.sent} tokens（接口给出的最大值）`;
  if (!limit.requested) return "未设置（由接口决定）";
  if (!limit.sent) return `${limit.requested} tokens（接口不接受，未发送）`;
  const param = limit.param === "max_completion_tokens" ? "以 max_completion_tokens 发送" : "";
  if (limit.sent !== limit.requested) return `${limit.requested} → ${limit.sent} tokens（接口给出的最大值${param ? "，" + param : ""}）`;
  return `${limit.requested} tokens` + (param ? `（${param}）` : "");
}
// The 记录 tab's 计费 and 费用 rows. Claude Code prices an Anthropic-compatible endpoint's turn at Claude's list price,
// which the endpoint need not charge; an OpenAI-compatible endpoint reports no cost.
function billingRecord(g) {
  const provider = recordProvider(g);
  if (provider !== "anthropic") return `${PROVIDER_LABELS[provider]}（${g.host || "—"}）`;
  return g.api_key_source === "ANTHROPIC_API_KEY" ? "Claude API 密钥" : g.api_key_source || "—";
}
// An Anthropic-compatible record's cost is Claude Code's estimate at Claude's prices: [the figure, the note under it].
function costRecord(g) {
  if (!Number.isFinite(g?.cost_usd)) return "—";
  return recordProvider(g) === "anthropic_compatible" ? [`约 ${g.cost_usd.toFixed(4)} 美元`, "按 Claude 官方价估算，实际以接口计费为准"] : `${g.cost_usd.toFixed(4)} 美元`;
}
// The answer panel's 强度 fact: the effort the endpoint took (generation.json effort.sent), not only the one chosen;
// none when the endpoint took none.
function effortMeta(g, names) {
  const chosen = g?.reasoning_effort, sent = g?.effort && "sent" in g.effort ? g.effort.sent : chosen;
  if (chosen && !sent) return null;
  return `${names[sent] || sent || "默认"}强度`;
}
// The status row's 上次测试 line from the saved {at, ok, model, summary} ("正常 · 2.1 秒", or the failure's first line):
// the facts, and for a failure its words on a line of their own. time is the test's time as the page writes it.
function lastTestLine(test, time) {
  if (!test?.at) return null;
  if (test.ok) { const [word, ...rest] = String(test.summary || "正常").split(" · "); return {facts: ["上次测试：" + word, ...rest, time], detail: null}; }
  return {facts: ["上次测试：失败", time], detail: test.summary || null};
}
// What 保存并验证 found, for its toast.
function savedMessage(check, switched) {
  const then = switched ? "，作答改用这个接口" : "";
  if (!check?.models_listed) return `已保存${then}。这个接口没有提供模型列表，密钥和模型要到作答时才能确认。`;
  const unlisted = check.unlisted_models?.length ? `接口的模型列表里没有 ${check.unlisted_models.join("、")}；列表常常不全，已照样保存。` : "";
  if (check.key_checked === false) return `已保存${then}。这个接口不用密钥也能读取模型列表，密钥要到作答时才能确认。${unlisted}`;
  return `已验证并保存${then}` + (unlisted ? "。" + unlisted : "");
}
// End provider presentation.

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
// tone: "ok" (a check) or "bad" (an error mark); none for plain notices. An error's second line (the server's own
// words, see errorParts) goes under it as a quiet detail and keeps the toast up longer, so there is time to read it.
// A short name for the copy button: a leading 【…】 label when the question has one, else its first words.
function pasteLabel(field) {
  const title = (field.title || field.entry_id || "").trim(), mark = title.match(/^【[^】]{1,12}】/);
  return mark ? mark[0] : "「" + (title.length > 14 ? title.slice(0, 13) + "…" : title) + "」";
}
async function copyAnswer(field) {
  try { await navigator.clipboard.writeText(field.text || ""); toast(`已复制${pasteLabel(field)}的答案，去表单里粘贴`, "ok"); }
  catch { toast("没能复制：请在“答案”里选中这一栏的文字手动复制。", "bad"); }
}
function toast(message, tone) {
  const el = $("toast"), [main, detail] = errorParts(message);
  el.className = "ys-toast toast" + (tone === "bad" ? " ys-toast--bad" : "") + (detail ? " has-detail" : "");
  const body = node("span", detail ? undefined : main, detail ? "toast-body" : undefined);
  if (detail) body.append(node("span", main), detailLine(detail));
  el.replaceChildren(...(tone ? [ysIcon(tone === "bad" ? "error" : "check")] : []), body);
  el.hidden = false; placeToast(el);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; placeToast(el); }, detail ? 9000 : 5000);
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
// The server's errors name the problem and the fix on their first line; a second line, when there is one, is the remote
// service's own message (already trimmed and without any key), shown underneath as a quiet detail.
function errorParts(message) {
  const text = String(message ?? ""), at = text.indexOf("\n");
  return at < 0 ? [friendlyError(text), ""] : [friendlyError(text.slice(0, at)), text.slice(at + 1).trim()];
}
function detailLine(text) { const el = node("span", text, "error-detail"); if (!/[぀-ヿ㐀-鿿]/.test(text)) el.lang = "en"; return el; }
function errorNodes(message) { const [main, detail] = errorParts(message); return detail ? [main, detailLine(detail)] : [main]; }
async function api(path, method = "GET", body, timeout) {
  const options = {method, cache: "no-store", credentials: BASE ? "same-origin" : "omit", headers: token ? {Authorization: "Bearer " + token} : {}};
  if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout || (method === "GET" ? 20000 : 60000));
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
  state.active = data.active_job_id; state.timezone = data.timezone || "Asia/Tokyo";
  state.provider = data.provider || null; state.apiKey = data.provider?.anthropic || data.api_key || null;
  if (data.school_login) state.schoolLogin = {...state.schoolLogin, ...data.school_login};
  // The provider was switched elsewhere (another tab or device): the toolbar lists its models again.
  if (state.models && state.provider?.active && state.models.provider !== state.provider.active) loadModels();
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

function renderAll() { renderHeader(); renderIssues(); renderFilters(); renderList(); renderJob(); renderHistory(); renderButtons(); renderApiKey(); renderProvider(); renderSchoolLogin(); }

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
  if (state.error) add("critical", errorNodes(state.error));
  const settingsLink = () => button("去设置", "ys-btn ys-btn--sm issue-action link small", openSettings);
  if (aiEnabled() && activeProvider() === "anthropic" && state.apiKey && !state.apiKey.configured) add("warning", "还没有设置 Claude API 密钥，作答前需要先填写。", settingsLink());
  if (aiEnabled() && ENDPOINTS[activeProvider()] && state.provider && !state.provider[activeProvider()]?.configured)
    add("warning", `${ENDPOINTS[activeProvider()].label}还没有设置好，作答前请在设置里填写，或改用 Claude API。`, settingsLink());
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
      // Only the main line here: on a phone the server's words would crowd every failed row; the panel shows them.
      if (record?.item?.error && !delivered(record.item)) titleCell.append(node("div", errorParts(record.item.error)[0], "note " + (status.tone === "bad" ? "bad" : "warn")));
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
function stepList(current, halted, job) {
  const list = node("ol", undefined, "ys-steps steps");
  stepNames(job, activeProvider()).forEach((label, i) => {
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
    parts.push(view.meter, stepList(s.step, dead, job));
  }
  if (message) {
    if (s.warning) { const line = node("p", undefined, "job-warning small warn" + (dead ? " bad" : "")); line.append(ysIcon(dead ? "error" : "warning"), node("span", friendlyError(message))); parts.push(line); }
    else { const line = node("p", undefined, "job-message small muted"); line.append(...errorNodes(message)); parts.push(line); }
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
      summary.append(time, factRow([kindLabels[job.kind] || job.kind, job.items?.length ? `${job.items.length} 项` : null, job.model ? modelLabel(job.model, jobProvider(job), state.models) : null,
        job.message ? {text: errorParts(job.message)[0], wrap: true} : null], "history-text"), jobStateMark(job.status), ysIcon("chevron-right"));
      details.append(summary);
      const events = node("ol", undefined, "events");
      for (const event of (job.events || []).slice(-40)) { const row = node("li"), at = node("time", dateLabel(event.at, true)); at.dateTime = event.at || ""; row.append(at, node("span", event.message)); events.append(row); }
      events.addEventListener("scroll", () => edgeFade(events, true), {passive: true});
      details.addEventListener("toggle", () => edgeFade(events, true));
      details.append(events); li.append(details); list.append(li);
    }
  });
}
// ---- settings: the Claude API -----------------------------------------------
function renderApiKey() {
  const status = state.apiKey, el = $("apiKeyStatus");
  $("apiKeyClear").hidden = !status?.configured; $("apiKeyReuse").hidden = !status?.configured;
  const lines = !status ? ["正在读取…"] : !status.configured ? ["尚未设置。没有密钥时不会调用 Claude。"]
    : [factRow(["已设置", `末尾 ${status.last4}`, status.max_output_tokens ? `输出上限 ${status.max_output_tokens}` : null,
      status.updated_at ? "更新于 " + dateLabel(status.updated_at, true) : null]), ...lastTestNodes("anthropic")];
  const key = JSON.stringify([status, state.timezone, state.provider?.tests?.anthropic, !!state.tests.anthropic]);
  if (el.dataset.key !== key) { el.dataset.key = key; el.replaceChildren(...lines); }
}
// The form shows the saved limit when the dialog opens and after a save; the key field always starts empty.
function fillClaudeForm() { $("apiKeyInput").value = ""; $("claudeMaxTokens").value = state.apiKey?.max_output_tokens ?? ""; showClaudeError(null); }
// A refused save says why in its group, and the field the problem is in is marked; any edit clears the mark.
function markFields(el, message, fields) {
  el.replaceChildren(...(message ? errorNodes(message) : [])); el.hidden = !message;
  const field = message ? fields.find(([pattern]) => pattern.test(message))?.[1] : null;
  for (const [, input] of fields) { if (input === field) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid"); }
  if (message) el.parentElement.scrollIntoView({block: "nearest"});
}
function showClaudeError(message) {
  markFields($("claudeError"), message, [[/^输出上限/, $("claudeMaxTokens")], [/(密钥|sk-ant-)/, $("apiKeyInput")]]);
}
// 保存 sends the limit every time (an empty field clears it) and the key only when one is typed: with a key saved, the
// limit alone can be changed. A new key is checked with Anthropic first; a refused one changes nothing.
async function saveClaude() {
  const save = $("apiKeySave"), key = $("apiKeyInput").value.trim();
  if (!key && !state.apiKey?.configured) { showClaudeError("请填写 Claude API 密钥（以 sk-ant- 开头）。"); $("apiKeyInput").focus(); return; }
  const body = {max_output_tokens: $("claudeMaxTokens").value.trim() || null}; if (key) body.key = key;
  settingsBusy(true); setBusy(save, true); save.textContent = key ? "正在验证…" : "正在保存…"; showClaudeError(null);
  try {
    state.apiKey = await api("/api/settings/api-key", "PUT", body); if (state.provider) state.provider.anthropic = state.apiKey;
    delete state.tests.anthropic; fillClaudeForm();
    await api("/api/settings/openai").then(applySettings).catch(() => {});
    toast(key ? "API 密钥已验证并保存" : "输出上限已保存", "ok");
  } catch (error) { showClaudeError(error.message); }
  finally {
    settingsBusy(false); setBusy(save, false); save.textContent = "保存"; renderProvider(); renderIssues(); renderButtons();
    if ($("settingsDialog").open && (!document.activeElement || document.activeElement === document.body)) save.focus({preventScroll: true});
  }
}
async function clearClaude() {
  if (!confirm("删除已保存的 Claude API 密钥？删除后无法用 Claude API 作答，直到重新填写。")) return;
  settingsBusy(true);
  try {
    state.apiKey = await api("/api/settings/api-key", "PUT", {key: ""}); if (state.provider) state.provider.anthropic = state.apiKey;
    delete state.tests.anthropic; fillClaudeForm(); await api("/api/settings/openai").then(applySettings).catch(() => {});
    toast("API 密钥已删除", "ok");
  } catch (error) { toast(error.message, "bad"); }
  finally { settingsBusy(false); renderProvider(); renderIssues(); renderButtons(); }
}

// ---- settings: the provider and the two endpoints ------------------------------
// The model provider: the active one answers new jobs; the settings dialog can show another one before it is saved.
function activeProvider() { return state.provider?.active || "anthropic"; }
function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
// The two endpoints share one form layout; each names its own elements (and its save route and suggestions).
const ENDPOINTS = {
  anthropic_compatible: {label: "Anthropic 兼容接口", route: "/api/settings/anthropic-compatible", example: "claude-sonnet-5-5",
    el: () => ({settings: $("compatSettings"), form: $("compatForm"), status: $("compatStatus"), base: $("compatBaseUrl"), key: $("compatKey"),
      reuse: $("compatKeyReuse"), models: $("compatModels"), list: $("compatModelList"), hint: $("compatModelsHint"), limit: $("compatMaxTokens"),
      save: $("compatSave"), clear: $("compatClear"), error: $("compatError")})},
  openai: {label: "OpenAI 兼容接口", route: "/api/settings/openai", example: "deepseek-chat",
    el: () => ({settings: $("openaiSettings"), form: $("openaiForm"), status: $("openaiStatus"), base: $("openaiBaseUrl"), key: $("openaiKey"),
      reuse: $("openaiKeyReuse"), models: $("openaiModels"), list: $("openaiModelList"), hint: $("openaiModelsHint"), limit: $("openaiMaxTokens"),
      save: $("openaiSave"), clear: $("openaiClear"), error: $("openaiError")})}
};
function renderProvider() {
  const active = activeProvider(), view = state.providerView || active;
  for (const b of $("providerSeg").children) b.setAttribute("aria-pressed", String(b.dataset.provider === view));
  $("anthropicSettings").hidden = view !== "anthropic";
  for (const name in ENDPOINTS) ENDPOINTS[name].el().settings.hidden = view !== name;
  // An endpoint is chosen but not saved yet: the active provider still answers until 保存并验证 succeeds. (A saved one is
  // being switched to.)
  const pending = !!ENDPOINTS[view] && active !== view && !state.provider?.[view]?.configured;
  $("providerNotice").hidden = !pending; $("providerNote").hidden = pending;
  if (pending) setText($("providerNoticeText"), `${ENDPOINTS[view].label}还没有保存，作答仍使用 ${PROVIDER_LABELS[active]}。填好下面的接口地址、密钥和模型，点“保存并验证”后会切换过去。`);
  renderApiKey(); for (const name in ENDPOINTS) renderEndpointStatus(name);
  renderTests();
}
function renderEndpointStatus(name) {
  const status = state.provider?.[name], el = ENDPOINTS[name].el(), shown = !!(status?.configured || status?.damaged);
  const suggestions = state.suggestions[name] || [];
  // A damaged file is said so and can be deleted; with nothing saved there is nothing to show or delete.
  el.status.hidden = !shown; el.clear.hidden = !shown;
  // The saved key can be kept only when something is saved.
  el.reuse.hidden = !status?.configured;
  setText(el.hint, suggestions.length ? `输入时会提示接口列出的 ${suggestions.length} 个模型。` : "");
  if (!shown) { el.status.replaceChildren(); delete el.status.dataset.key; return; }
  const lines = [];
  if (!status.configured) lines.push(node("span", "保存的设置已损坏，读不出来。请重新填写并保存，或删除。", "status-sub warn"));
  else {
    lines.push(factRow([status.host, {text: status.models.map(id => name === "openai" ? id : modelLabel(id, name, state.models)).join("、"), wrap: true},
      status.last4 ? `密钥 …${status.last4}` : "已保存密钥", status.max_output_tokens ? `输出上限 ${status.max_output_tokens}` : null,
      status.updated_at ? "更新于 " + dateLabel(status.updated_at, true) : null]));
    if (!status.models_listed) lines.push(node("span", "这个接口没有提供模型列表：密钥和模型要到作答时才能确认。", "status-sub"));
    else if (status.key_checked === false) lines.push(node("span", "这个接口不用密钥也能读取模型列表：密钥要到作答时才能确认。", "status-sub"));
    if (status.models_listed && status.unlisted_models?.length) lines.push(node("span", `接口的模型列表里没有 ${status.unlisted_models.join("、")}；列表常常不全，已照样保存。`, "status-sub"));
    if (status.plain_http) lines.push(node("span", "这个地址用 http，密钥会以明文发送；能用 https 时请改用 https。", "status-sub warn"));
    lines.push(...lastTestNodes(name));
  }
  const key = JSON.stringify([status, state.timezone, state.provider?.tests?.[name], !!state.tests[name], state.models?.provider]);
  if (el.status.dataset.key !== key) { el.status.dataset.key = key; el.status.replaceChildren(...lines); }
}
// The form shows the saved address and models when the dialog opens and after a save, never while it is being edited.
function fillEndpointForm(name) {
  const status = state.provider?.[name], el = ENDPOINTS[name].el();
  el.base.value = status?.base_url || ""; el.key.value = ""; el.models.value = status?.models?.join(", ") || "";
  el.limit.value = status?.max_output_tokens ?? "";
  showEndpointError(name, null); renderModelSuggestions(name);
}
function showEndpointError(name, message) {
  const el = ENDPOINTS[name].el();
  markFields(el.error, message, [[/^(请填写接口地址|接口地址|连不上|这个地址返回的不是模型列表|验证时接口返回错误)/, el.base],
    [/^(请至少填写一个模型|最多填写|模型名称)/, el.models], [/^(请填写接口的密钥|换了接口地址|密钥|接口拒绝了这个密钥)/, el.key], [/^输出上限/, el.limit]]);
}
// Suggestions for the model field: the endpoint's own ids, completing the name after the last comma, so picking one
// keeps the names already typed.
function renderModelSuggestions(name) {
  const el = ENDPOINTS[name].el(), value = el.models.value, cut = Math.max(...[",", "，", "、"].map(x => value.lastIndexOf(x)));
  const head = cut >= 0 ? value.slice(0, cut + 1).replace(/\s*$/, " ") : "", last = value.slice(cut + 1).trim().toLowerCase();
  const chosen = new Set(head.split(/[,，、]/).map(x => x.trim()).filter(Boolean));
  const options = (state.suggestions[name] || []).filter(id => !chosen.has(id) && (!last || id.toLowerCase().includes(last))).slice(0, 60);
  el.list.replaceChildren(...options.map(id => { const option = node("option"); option.value = head + id; return option; }));
}
function applySettings(view) {
  // Every provider block the server sends (and its last connection tests), whatever providers it knows.
  const {suggestions, anthropic_compatible_suggestions: compatSuggestions, check: _check, ...provider} = view;
  state.provider = provider; state.apiKey = view.anthropic;
  if (Array.isArray(suggestions)) state.suggestions.openai = suggestions;
  if (Array.isArray(compatSuggestions)) state.suggestions.anthropic_compatible = compatSuggestions;
}
function openSettings(section) {
  state.providerView = null; fillClaudeForm(); for (const name in ENDPOINTS) fillEndpointForm(name); renderProvider(); renderSchoolLogin();
  $("settingsDialog").showModal();
  if (section === "school") requestAnimationFrame(() => $("schoolSettings").scrollIntoView({block: "start"}));
  api("/api/settings/openai").then(view => { applySettings(view); renderProvider(); for (const name in ENDPOINTS) renderModelSuggestions(name); }).catch(() => {});
  loadSchoolLogin(); renderDrive(); loadDrive();
}
async function chooseProvider(provider) {
  if (provider === (state.providerView || activeProvider()) || $("providerSeg").getAttribute("aria-busy")) return;
  state.providerView = provider === activeProvider() ? null : provider; renderProvider();
  // Choosing an endpoint before it is saved only shows its form: the switch happens once 保存并验证 succeeds.
  if (provider === activeProvider() || (ENDPOINTS[provider] && !state.provider?.[provider]?.configured)) return;
  await switchProvider(provider);
}
// The server's view of the provider settings, re-read after a refusal: another tab may have changed them meanwhile.
function reloadSettings() { return api("/api/settings/openai").then(view => { applySettings(view); renderProvider(); renderIssues(); renderButtons(); }).catch(() => {}); }
async function switchProvider(provider) {
  setBusy($("providerSeg"), true);
  try {
    applySettings(await api("/api/settings/provider", "PUT", {provider})); state.providerView = null;
    toast(ENDPOINTS[provider] ? `作答改用 ${ENDPOINTS[provider].label}（${state.provider[provider].host}）` : "作答改用 Claude API", "ok");
    await loadModels();
  } catch (error) { state.providerView = null; toast(error.message, "bad"); await reloadSettings(); }
  finally { setBusy($("providerSeg"), false); renderProvider(); renderIssues(); renderButtons(); }
}
// While one settings request runs, the other buttons wait: a 删除 during 保存并验证 would be undone by the save.
function settingsBusy(busy) {
  state.settingsBusy = busy;
  for (const b of [$("apiKeySave"), $("apiKeyClear"), ...Object.keys(ENDPOINTS).flatMap(name => [ENDPOINTS[name].el().save, ENDPOINTS[name].el().clear])]) b.disabled = busy;
  renderTests();
}
async function saveEndpoint(name) {
  // The limit always goes: an empty field clears a saved one; the server checks the number.
  const el = ENDPOINTS[name].el(), save = el.save;
  const body = {base_url: el.base.value.trim(), models: el.models.value, max_output_tokens: el.limit.value.trim() || null};
  const key = el.key.value.trim(); if (key) body.api_key = key;
  settingsBusy(true); setBusy(save, true); save.textContent = "正在验证…"; showEndpointError(name, null);
  try {
    const view = await api(ENDPOINTS[name].route, "PUT", body); applySettings(view);
    let switched = false;
    if (state.providerView === name && activeProvider() !== name) {
      applySettings(await api("/api/settings/provider", "PUT", {provider: name})); switched = true;
    }
    state.providerView = null; delete state.tests[name]; fillEndpointForm(name);
    if (activeProvider() === name) await loadModels();
    toast(savedMessage(view.check, switched), "ok");
  } catch (error) { showEndpointError(name, error.message); }
  finally {
    settingsBusy(false); setBusy(save, false); save.textContent = "保存并验证"; renderProvider(); renderIssues(); renderButtons();
    // The button was disabled while it worked, which dropped keyboard focus to the page: it goes back to the button.
    if ($("settingsDialog").open && (!document.activeElement || document.activeElement === document.body)) save.focus({preventScroll: true});
  }
}
async function clearEndpoint(name) {
  const label = ENDPOINTS[name].label, answering = activeProvider() === name;
  if (!confirm(`删除已保存的 ${label}设置？` + (answering ? "删除后作答改用 Claude API。" : ""))) return;
  settingsBusy(true);
  try {
    applySettings(await api(ENDPOINTS[name].route, "PUT", {clear: true})); state.providerView = null; state.suggestions[name] = []; delete state.tests[name];
    fillEndpointForm(name); if (answering) await loadModels();
    toast(`已删除 ${label}的设置` + (answering ? "，作答改用 Claude API" : ""), "ok");
  } catch (error) { toast(error.message, "bad"); }
  finally { settingsBusy(false); renderProvider(); renderIssues(); renderButtons(); }
}

// ---- connection test ------------------------------------------------------
// Each provider group has a 测试连接 button (data-test-provider) and a result row (data-test-result); a provider is
// testable once it is configured. One test at a time, page-wide, as on the server.
const testConfigured = {anthropic: () => !!state.provider?.anthropic?.configured, openai: () => !!state.provider?.openai?.configured,
  anthropic_compatible: () => !!state.provider?.anthropic_compatible?.configured};
function renderTests() {
  for (const b of document.querySelectorAll("[data-test-provider]")) {
    const provider = b.dataset.testProvider, mine = state.testing === provider;
    b.disabled = !!state.testing || state.settingsBusy || !testConfigured[provider]?.();
    setBusy(b, mine); setText(b, mine ? "测试中…" : "测试连接");
  }
  for (const box of document.querySelectorAll("[data-test-result]")) renderTestResult(box, state.tests[box.dataset.testResult]);
}
// The saved last test, under a group's status facts; hidden while this page shows a fresh result in the group.
function lastTestNodes(provider) {
  const line = state.tests[provider] ? null : lastTestLine(state.provider?.tests?.[provider], state.provider?.tests?.[provider]?.at ? clockLabel(state.provider.tests[provider].at) : "");
  if (!line) return [];
  const nodes = [factRow(line.facts, "status-sub")];
  if (line.detail) nodes.push(node("span", line.detail, "status-sub warn"));
  return nodes;
}
function clockLabel(value) { return new Intl.DateTimeFormat("zh-CN", {timeZone: state.timezone, hour: "2-digit", minute: "2-digit", hour12: false}).format(new Date(value)); }
// The result of the test just run, in the group: plain when it worked, with what the endpoint did not take; the error
// (and the server's own words underneath) when it did not.
function renderTestResult(box, result) {
  const key = JSON.stringify(result || null);
  if (box.dataset.key === key) return;
  box.dataset.key = key; box.hidden = !result;
  if (!result) { box.replaceChildren(); return; }
  if (!result.ok) {
    const head = node("span", undefined, "test-head bad"); head.append(ysIcon("error"), node("span", "连接测试没有通过"));
    const words = node("span", undefined, "test-error"); words.append(...errorNodes(result.error || result.message));
    box.replaceChildren(head, words); return;
  }
  const usage = result.usage || {}, tokens = usage.input_tokens !== undefined ? `${usage.input_tokens + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0)} / ${usage.output_tokens} tokens` : null;
  const head = node("span", undefined, "test-head"); head.append(ysIcon("check"), node("span", "连接正常"));
  const seconds = result.duration_ms < 60000 ? `${((result.duration_ms || 0) / 1000).toFixed(1)} 秒` : durationLabel(result.duration_ms);
  // The model the server reported, and the chosen one beside it when it named another (as the 记录 tab does).
  const label = id => result.provider === "openai" ? id : modelLabel(id, result.provider, state.models);
  const models = recordModels({provider: "openai_compatible", model: result.reported_model, requested_model: result.requested_model}, label);
  const lines = [head, factRow([...models.map((text, i) => ({text, wrap: true, cls: i ? "ys-muted" : null})), seconds, tokens,
    Number.isFinite(result.cost_usd) ? `约 ${result.cost_usd.toFixed(4)} 美元` + (result.provider === "anthropic_compatible" ? "（按官方价估算）" : "") : null])];
  // What the endpoint did not take: the way an answer will be sent too.
  for (const note of result.notes || []) lines.push(node("span", note, "status-sub warn"));
  box.replaceChildren(...lines);
}
async function testConnection(provider) {
  if (state.testing || !testConfigured[provider]?.()) return;
  state.testing = provider; delete state.tests[provider]; renderTests(); renderProvider();
  // The toolbar's model when the toolbar lists this provider's models; else the provider's default.
  const model = state.models?.provider === provider ? $("modelSelect").value || null : null;
  try { state.tests[provider] = await api("/api/settings/test", "POST", model ? {provider, model} : {provider}, 135000); }
  catch (error) { state.tests[provider] = {ok: false, provider, error: error.message}; }
  finally {
    state.testing = null;
    await api("/api/settings/openai").then(applySettings).catch(() => {});
    renderProvider(); renderTests();
  }
}

// ---- settings: Google 文档 (google_doc.py) -------------------------------------------------
// One grant, Drive's "files this app created" only, so the answer can go into a new Doc in the student's own Drive. The
// mini PC cannot receive Google's redirect: the student pastes the address of the page that did not open, once.
const drive = {status: null, then: null};
function renderDrive() {
  const s = drive.status, el = $("driveStatus");
  if (!s) el.replaceChildren("正在读取…");
  else if (!s.client) el.replaceChildren(node("span", "小主机上还没有 Google 授权用的客户端配置，暂时不能授权。", "status-warn"));
  else if (s.mismatch) el.replaceChildren(node("span", "授权账号与学校账号不符", "status-warn"), node("span", `已授权 ${s.email}，学校账号是 ${s.account}。请重新授权，在 Google 页面选择学校账号。`, "status-sub warn"));
  else if (s.authorized) el.replaceChildren(factRow(["已授权", {text: s.email, wrap: true}, s.granted_at ? "授权于 " + dateLabel(s.granted_at, true) : null]));
  else el.replaceChildren("未授权");
  setText($("driveAuthorize"), s?.authorized ? "重新授权" : "授权");
  $("driveAuthorize").classList.toggle("ys-btn--primary", !s?.authorized || !!s?.mismatch);
  $("driveAuthorize").disabled = !s?.client;
  $("driveForget").hidden = !s?.authorized;
}
async function loadDrive() {
  try { drive.status = await api("/api/google-docs/status"); } catch (error) { /* keep what was shown */ }
  renderDrive();
}
async function openDriveAuth(then) {
  drive.then = then || null; $("driveError").hidden = true; $("driveUrl").value = "";
  try {
    const started = await api("/api/google-docs/authorize", "POST", {});
    $("driveOpen").href = started.url; setText($("driveAccount"), started.account ? `（${started.account}）` : "");
    $("driveDialog").showModal();
  } catch (error) { toast(error.message, "bad"); }
}
async function createGoogleDoc(jobId, key, control) {
  if (!drive.status) await loadDrive();
  if (!drive.status?.authorized || drive.status?.mismatch) {
    if (!drive.status?.client) { toast("小主机上还没有 Google 授权用的客户端配置，暂时不能新建文档。", "bad"); return; }
    openDriveAuth(() => createGoogleDoc(jobId, key)); return;
  }
  if (control) { control.disabled = true; setText(control, "正在新建…"); }
  try {
    const result = await api(`/api/jobs/${jobId}/items/${key}/google-doc`, "POST", {}, 180000);
    updateJob(result.job);
    toast(result.reused ? "这份答案已经有 Google 文档了。" : "已在你的云端硬盘新建 Google 文档，内容已读回核对。", "ok");
    if (state.panel?.key === key) openPanel(jobId, key, state.panel.tab);
  } catch (error) {
    toast(error.message, "bad");
    if (error.message.includes("授权")) loadDrive();
    if (control) { control.disabled = false; setText(control, "新建 Google 文档"); }
  }
}

// ---- settings: the school account (school_browser.py) ------------------------------------
// The login lives only in the mini PC's school-login browser. This page shows who is signed in and opens a remote view
// of that browser to sign in: a live picture of the page, taps, the text typed below it and a few keys. Nothing typed
// here is kept by the page: the field is emptied as soon as its text is sent.
const school = {open: false, started: false, ended: false, finishing: false, busy: false, seq: 0, frame: null, url: null,
  host: "", hint: "", password: false, editable: false, reveal: false, chain: Promise.resolve(), drag: null, scroll: null,
  viewport: null, resize: null, typing: false, field: null, refused: false, elsewhere: false};
function schoolStatusLines(status) {
  if (!status) return ["正在读取…"];
  const when = status.verified_at ? "确认于 " + dateLabel(status.verified_at, true) : null;
  if (status.state === "signed_in") {
    const lines = [factRow(["已登录", {text: status.email, wrap: true}, when])];
    if (status.uncertain) lines.push(node("span", "账号是从页面文字里找到的（Google 的账号列表格式可能变了），请确认这是你的学校账号。", "status-sub warn"));
    return lines;
  }
  if (status.state === "expired") return [node("span", "登录已过期，需要重新登录", "status-warn"), factRow([{text: status.email, wrap: true}, when], "status-sub")];
  if (status.state === "mismatch")
    return [node("span", "账号与设置不符", "status-warn"), node("span", `已登录 ${status.email}，设置里的学校邮箱是 ${status.school_email}。请退出登录后用学校邮箱登录。`, "status-sub warn")];
  return ["未登录"];
}
function renderSchoolLogin() {
  const status = state.schoolLogin, el = $("schoolLoginStatus"), key = JSON.stringify([status?.state, status?.email, status?.verified_at, status?.uncertain, status?.school_email, state.timezone]);
  if (el.dataset.key !== key) { el.dataset.key = key; el.replaceChildren(...schoolStatusLines(status)); }
  const signed = !!status && status.state !== "signed_out", mismatch = status?.state === "mismatch";
  setText($("schoolSignIn"), signed ? "重新登录" : "登录");
  // A working login needs no action: 重新登录 is then an ordinary button, 登录 the main one. Another account than the
  // settings' email is fixed by logging out first, so 退出登录 is the main action then.
  $("schoolSignIn").classList.toggle("ys-btn--primary", !["signed_in", "mismatch"].includes(status?.state));
  $("schoolSignIn").disabled = !status || school.busy || school.open;
  const logout = $("schoolLogout");
  logout.hidden = !(signed || status?.profile); logout.disabled = school.busy || school.open;
  // Without a login, what is left is a sign-in that was never finished: the button clears that browser's data.
  setText(logout, signed ? "退出登录" : "清除登录资料");
  logout.className = mismatch ? "ys-btn ys-btn--primary btn small" : "ys-btn ys-btn--danger link small";
}
async function loadSchoolLogin() {
  try { state.schoolLogin = await api("/api/school-login/status"); } catch (error) { if (!state.schoolLogin) state.schoolLogin = {state: "signed_out"}; }
  renderSchoolLogin();
}
async function schoolLogout() {
  const signed = state.schoolLogin?.state && state.schoolLogin.state !== "signed_out";
  if (!confirm(signed ? "退出学校账号？会删除小主机上这个浏览器的全部登录资料；之后需要登录的表单要重新登录才能读取。"
    : "清除小主机上这个浏览器留下的登录资料？（之前的登录没有完成，资料没有在用。）")) return;
  school.busy = true; renderSchoolLogin();
  try { state.schoolLogin = await api("/api/school-login/logout", "POST", {}); toast(signed ? "已退出学校账号，浏览器的登录资料已删除。" : "浏览器的登录资料已清除。", "ok"); }
  catch (error) { toast(error.message, "bad"); await loadSchoolLogin(); }
  finally { school.busy = false; renderSchoolLogin(); if (state.panel?.data) renderPanel(); }
}
const schoolHints = {
  not_secure: "Google 拒绝了这个浏览器（“此浏览器或应用可能不安全”）。点“返回”后再试一次，或在画面里选择其他登录方式；仍然不行时，可以在作业的“补充后重做”里导入题目。",
  passkey: "Google 要求用通行密钥验证，远程画面里用不了。请点画面里的“尝试其他方式”（別の方法を試す），改用密码或验证码。"};
function signedInView() { return school.host === "docs.google.com"; }
function schoolStatusText() {
  // While the page opens, the view itself says so (once).
  if (school.ended || !school.frame) return "";
  if (school.finishing) return "正在确认登录的是哪个账号…";
  if (school.refused) return "这个账号没有保存。请在画面里重新登录学校账号，再点“完成”。";
  if (signedInView()) return "已经登录 Google。点“完成”确认账号。";
  if (school.editable) return school.password ? `密码栏已选中：在上面输入${school.reveal ? "" : "（不会显示）"}，按键盘上的回车发送并确认。` : "输入栏已选中：在上面输入，按键盘上的回车发送并确认。";
  return "点画面里的输入框或按钮来操作；输入的文字只送到登录页。";
}
function renderSchoolDialog() {
  const ended = school.ended, live = school.started && !ended;
  // The host line shows where the view is, with the lock; nothing while the page opens, and no lock once it is closed.
  setText($("schoolHost"), ended ? "登录窗口已关闭" : school.host || "\u00a0");
  // (Its line keeps its place while empty, so the view under it does not change size when the host appears.)
  $("schoolHostLine").classList.toggle("is-empty", !ended && !school.host); $("schoolHostLine").classList.toggle("is-closed", ended);
  $("schoolDialog").classList.toggle("is-ended", ended);
  $("schoolTakeover").hidden = !(ended && school.elsewhere);
  const hint = schoolHints[school.hint] && !ended ? schoolHints[school.hint] : "";
  $("schoolHint").hidden = !hint; setText($("schoolHintText"), hint);
  setText($("schoolStatusLine"), schoolStatusText());
  for (const el of [$("schoolText"), $("schoolSend"), $("schoolReveal"), $("schoolBack"), ...document.querySelectorAll("[data-school-key]")]) el.disabled = !live || school.finishing;
  $("schoolFinish").hidden = ended; $("schoolFinish").disabled = !live || school.finishing; setBusy($("schoolFinish"), school.finishing);
  // 完成 becomes the main action once Google has sent the view on to its continue page (docs.google.com).
  $("schoolFinish").classList.toggle("ys-btn--primary", (signedInView() && !school.refused) || school.finishing);
  setText($("schoolFinish"), school.finishing ? "正在确认…" : "完成"); setText($("schoolCancel"), ended ? "关闭" : "取消");
  const input = $("schoolText"), type = school.password && !school.reveal ? "password" : "text";
  if (input.type !== type) input.type = type;
  input.placeholder = school.password ? "在这里输入密码，发送到登录页" : "在这里输入，发送到登录页";
  $("schoolReveal").hidden = !school.password; setText($("schoolReveal"), school.reveal ? "隐藏" : "显示");
  $("schoolReveal").setAttribute("aria-pressed", String(school.reveal));
}
// A sign-in error is red; a window that closed in the ordinary way (cancelled, idle, opened elsewhere) is said calmly.
function showSchoolError(message, quiet = false) {
  const el = $("schoolError"); el.replaceChildren(...(message ? errorNodes(message) : [])); el.hidden = !message;
  el.parentElement.classList.toggle("is-quiet", !!message && quiet);
}
// The picture keeps its proportions and fits the view, at the size the sign-in page was laid out for. While a touch
// screen's keyboard is up (the text field has the focus) it keeps the view's width instead, near one to one, and shows
// the band of the remote page around its focused field: the rest is a swipe away, and nothing shrinks to a stamp.
function fitSchoolFrame() {
  const frame = school.frame, img = $("schoolFrame"), view = $("schoolView");
  if (!frame || !frame.width || !frame.height) return;
  const typing = school.typing, viewH = view.clientHeight;
  const fit = Math.min(1, view.clientWidth / frame.width, typing ? Infinity : viewH / frame.height);
  const height = Math.floor(frame.height * fit);
  img.style.width = Math.floor(frame.width * fit) + "px"; img.style.height = height + "px";
  let shift = 0;
  if (typing && height > viewH) {
    // The field a third of the way down, so what Google shows under it (an error, the next button) stays in sight.
    const anchor = school.field ? school.field.top * fit - viewH / 3 : 0;
    shift = Math.round(Math.max(0, Math.min(height - viewH, anchor)));
  }
  img.style.transform = shift ? `translateY(${-shift}px)` : "";
}
function setSchoolTyping(on) {
  on = on && matchMedia("(pointer: coarse)").matches;
  if (school.typing === on) return;
  school.typing = on; $("schoolDialog").classList.toggle("is-typing", on); fitSchoolFrame();
}
function applySchoolFrame(f) {
  school.seq = f.seq;
  if (f.image) {
    const bytes = Uint8Array.from(atob(f.image), c => c.charCodeAt(0)), url = URL.createObjectURL(new Blob([bytes], {type: "image/jpeg"}));
    const img = $("schoolFrame"), old = school.url;
    school.url = url; img.src = url; img.hidden = false; $("schoolViewNote").hidden = true;
    if (old) URL.revokeObjectURL(old);
    school.frame = {image_width: f.image_width, image_height: f.image_height, width: f.width, height: f.height};
    fitSchoolFrame();
  }
  if (f.password !== school.password) { school.password = !!f.password; school.reveal = false; }
  const focused = !!f.editable && !school.editable;
  school.editable = !!f.editable; school.host = f.host || school.host; school.hint = f.hint || "";
  // Back on Google's sign-in after an account 完成 refused: the usual guidance again.
  if (school.refused && school.host && school.host !== "docs.google.com") school.refused = false;
  const field = f.editable && f.top >= 0 ? {top: f.top, bottom: f.bottom} : null;
  if (JSON.stringify(field) !== JSON.stringify(school.field)) { school.field = field; fitSchoolFrame(); }
  // Signed in: a phone's keyboard goes down, so 完成 is in sight.
  if (signedInView() && !school.refused && school.typing) $("schoolText").blur();
  renderSchoolDialog();
  // On a computer the keyboard goes on typing: a field chosen in the picture sends the focus to the text field here.
  if (focused && !matchMedia("(pointer: coarse)").matches && document.activeElement !== $("schoolText")) $("schoolText").focus({preventScroll: true});
}
function schoolEnded(message, quiet = false) {
  if (school.ended) return;
  school.ended = true; school.editable = false; school.password = false;
  if (document.activeElement === $("schoolText")) $("schoolText").blur();
  showSchoolError(message || "登录窗口已经关闭。需要时请在 设置 → 学校账号 重新开始登录。", quiet);
  renderSchoolDialog();
}
async function pollSchool() {
  let failures = 0;
  while (school.open && !school.ended) {
    let f;
    try { f = await api(`/api/school-login/frame?after=${school.seq}&wait=20`, "GET", undefined, 35000); failures = 0; }
    catch (error) {
      if (!school.open) return;
      if (++failures >= 4) { schoolEnded(error.message); return; }
      await new Promise(resolve => setTimeout(resolve, 1500)); continue;
    }
    if (!school.open) return;
    if (f.ended) { schoolEnded(f.message, true); return; }
    applySchoolFrame(f);
  }
}
// Input goes out one action at a time, in order (the text, then 回车).
function schoolInput(action) {
  school.chain = school.chain.then(() => school.started && !school.ended ? api("/api/school-login/input", "POST", action) : null)
    .catch(error => { showSchoolError(error.message); });
  return school.chain;
}
function sendSchoolText(enter) {
  const input = $("schoolText"), text = input.value;
  showSchoolError(null);
  if (text) { input.value = ""; schoolInput({type: "text", text}); }
  if (enter) schoolInput({type: "key", key: "Enter"});
  if (!text && !enter) input.focus();
}
// A point of the picture, in the frame image's pixels (the server maps it to the page).
function schoolPoint(event) {
  const rect = $("schoolFrame").getBoundingClientRect(), frame = school.frame;
  const x = Math.min(Math.max(event.clientX - rect.left, 0), rect.width), y = Math.min(Math.max(event.clientY - rect.top, 0), rect.height);
  return {x: Math.round(x / rect.width * frame.image_width), y: Math.round(y / rect.height * frame.image_height)};
}
// A swipe (touch) or the wheel scrolls the page, sent at most every 80 ms; the distance is in the page's own pixels.
function queueSchoolScroll(event, dx, dy) {
  const rect = $("schoolFrame").getBoundingClientRect(), per = school.frame.width / rect.width;
  const pending = school.scroll || (school.scroll = {...schoolPoint(event), dx: 0, dy: 0, timer: null});
  pending.dx += dx * per; pending.dy += dy * per;
  if (!pending.timer) pending.timer = setTimeout(flushSchoolScroll, 80);
}
function flushSchoolScroll() {
  const pending = school.scroll; school.scroll = null;
  if (!pending) return;
  clearTimeout(pending.timer);
  if (Math.abs(pending.dx) + Math.abs(pending.dy) >= 1) schoolInput({type: "scroll", x: pending.x, y: pending.y, dx: Math.round(pending.dx), dy: Math.round(pending.dy)});
}
// While a touch screen's keyboard is up (phone or iPad: the visible part is clearly shorter than the page), the sheet
// follows the visible part of the screen, so the picture, the field and the keys stay above the keyboard.
function trackSchoolViewport(on) {
  const vv = window.visualViewport, dialog = $("schoolDialog");
  if (on && vv && !school.viewport) {
    school.viewport = () => {
      const keyboard = matchMedia("(pointer: coarse)").matches && vv.scale < 1.05 && vv.height < window.innerHeight - 80;
      dialog.classList.toggle("is-keyboard", keyboard);
      if (keyboard) { dialog.style.setProperty("--school-vh", Math.round(vv.height) + "px"); dialog.style.setProperty("--school-top", Math.round(vv.offsetTop) + "px"); }
      else { dialog.style.removeProperty("--school-vh"); dialog.style.removeProperty("--school-top"); }
      fitSchoolFrame();
    };
    vv.addEventListener("resize", school.viewport); vv.addEventListener("scroll", school.viewport); school.viewport();
  } else if (!on && vv && school.viewport) {
    vv.removeEventListener("resize", school.viewport); vv.removeEventListener("scroll", school.viewport); school.viewport = null;
    dialog.style.removeProperty("--school-vh"); dialog.style.removeProperty("--school-top"); dialog.classList.remove("is-keyboard");
  }
}
function resetSchool() {
  Object.assign(school, {started: false, ended: false, finishing: false, seq: 0, frame: null, host: "", hint: "", password: false, editable: false, reveal: false, drag: null, scroll: null, field: null, refused: false, elsewhere: false, chain: Promise.resolve()});
  const img = $("schoolFrame"); img.hidden = true; img.removeAttribute("src"); img.style.transform = "";
  setText($("schoolViewNote"), "正在打开 Google 登录页…"); $("schoolViewNote").hidden = false; $("schoolText").value = "";
  showSchoolError(null); renderSchoolDialog();
}
async function openSchoolLogin() {
  if (school.open) return;
  school.open = true; resetSchool(); renderSchoolLogin();
  $("schoolDialog").showModal(); trackSchoolViewport(true);
  school.resize = school.resize || new ResizeObserver(() => fitSchoolFrame());
  school.resize.observe($("schoolView"));
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  await startSchool(false);
}
// `takeover`: a sign-in window open elsewhere (another device, a closed tab) is closed and opened here instead.
async function startSchool(takeover) {
  // The sign-in page is laid out for the size of the view here, so the picture shows it at one to one.
  const view = $("schoolView"), touch = matchMedia("(pointer: coarse)").matches;
  const body = {width: Math.round(view.clientWidth), height: Math.round(view.clientHeight), touch, scale: (window.devicePixelRatio || 1) >= 1.5 ? 2 : 1, takeover};
  try {
    const status = await api("/api/school-login/start", "POST", body, 90000);
    if (!school.open) { api("/api/school-login/cancel", "POST", {}).catch(() => {}); return; }
    school.started = true; state.schoolLogin = {...state.schoolLogin, ...status}; renderSchoolDialog();
    pollSchool();
  } catch (error) {
    // Refused because a window is open elsewhere: say so, and offer to open it here.
    const status = await api("/api/school-login/status").catch(() => null);
    if (!school.open) return;
    school.elsewhere = !!status?.session?.active;
    schoolEnded(error.message, school.elsewhere); setText($("schoolViewNote"), "登录页没有打开。");
  }
}
function takeoverSchool() {
  if (!school.open || !school.ended) return;
  resetSchool(); startSchool(true);
}
async function finishSchool() {
  if (!school.started || school.ended || school.finishing) return;
  school.finishing = true; showSchoolError(null); renderSchoolDialog();
  try {
    await school.chain;
    const status = await api("/api/school-login/finish", "POST", {}, 60000);
    state.schoolLogin = status; school.ended = true;
    $("schoolDialog").close();
    toast(`已登录学校账号 ${status.email}。之前因表单停下的作业，点“重试未完成”即可读取。`, "ok");
    if (state.panel?.data) renderPanel();
  } catch (error) { school.refused = true; showSchoolError(error.message); }
  finally { school.finishing = false; renderSchoolDialog(); renderSchoolLogin(); }
}
function cancelSchool() {
  const live = school.started && !school.ended;
  school.ended = true;
  if (live) api("/api/school-login/cancel", "POST", {}).catch(() => {});
  if ($("schoolDialog").open) $("schoolDialog").close();
}
function schoolClosed() {
  if (school.started && !school.ended) api("/api/school-login/cancel", "POST", {}).catch(() => {});
  school.open = false; school.ended = true; school.started = false; school.frame = null; $("schoolText").value = "";
  setSchoolTyping(false); $("schoolDialog").classList.remove("is-ended");
  if (school.url) { URL.revokeObjectURL(school.url); school.url = null; }
  const img = $("schoolFrame"); img.removeAttribute("src"); img.hidden = true;
  trackSchoolViewport(false); school.resize?.disconnect();
  renderSchoolLogin(); loadSchoolLogin();
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
  if (aiEnabled() && activeProvider() === "anthropic" && state.apiKey && !state.apiKey.configured) { toast("请先在设置里填写 Claude API 密钥。"); openSettings(); return; }
  if (aiEnabled() && ENDPOINTS[activeProvider()] && state.provider && !state.provider[activeProvider()]?.configured) { toast(`请先在设置里填写 ${ENDPOINTS[activeProvider()].label}。`); openSettings(); return; }
  const body = {selected: items.map(({course_id, assignment_id}) => ({course_id, assignment_id})), ai_confirmed: aiEnabled()};
  // The provider whose models the toolbar lists: the server refuses the start if another one was made active meanwhile.
  if (aiEnabled()) Object.assign(body, {model: $("modelSelect").value, effort: $("effortSelect").value || null, provider: state.models?.provider || null});
  // A refused start (the provider switched elsewhere, say) keeps the ticks, so 完成 does not fall back to every item.
  const chosen = new Set(state.selected);
  state.selected.clear();
  action("/api/review", body).then(job => {
    if (job) return;
    state.selected = chosen; renderList(); renderButtons();
    loadState().catch(() => {});
  });
}

// ---- models and settings -------------------------------------------------

// The toolbar lists the active provider's models. Each provider remembers its own choice.
const choiceKeys = () => ({openai: ["classroom-openai-model", "classroom-openai-effort"],
  anthropic_compatible: ["classroom-compat-model", "classroom-compat-effort"]})[state.models?.provider] || ["classroom-model", "classroom-effort"];
async function loadModels() {
  const select = $("modelSelect");
  try {
    const catalog = await api("/api/models"); state.models = catalog;
    select.replaceChildren();
    for (const model of catalog.models) { const option = node("option", model.displayName || model.model); option.value = model.model; select.append(option); }
    const [modelKey, effortKey] = choiceKeys(), saved = localStorage.getItem(modelKey);
    select.value = catalog.models.some(x => x.model === saved) ? saved : catalog.default_model;
    renderEfforts(localStorage.getItem(effortKey));
  } catch (error) {
    state.models = null; select.replaceChildren(node("option", "没有模型")); $("effortSelect").replaceChildren(node("option", "—"));
    toast(error.message, "bad");
  }
  renderButtons();
}
// An OpenAI-compatible server gets no reasoning_effort unless one is chosen: 默认 sends nothing.
function renderEfforts(preferred) {
  const model = state.models?.models.find(x => x.model === $("modelSelect").value), select = $("effortSelect"); select.replaceChildren();
  if (state.models?.provider === "openai") { const option = node("option", "强度 默认"); option.value = ""; select.append(option); }
  for (const {reasoningEffort} of model?.supportedReasoningEfforts || []) { const option = node("option", "强度 " + (efforts[reasoningEffort] || reasoningEffort)); option.value = reasoningEffort; select.append(option); }
  const values = [...select.options].map(x => x.value);
  select.value = values.includes(preferred) ? preferred : values.includes(model?.defaultReasoningEffort) ? model.defaultReasoningEffort : values[0] || "";
}
function saveModelChoice() { const [modelKey, effortKey] = choiceKeys(); localStorage.setItem(modelKey, $("modelSelect").value); localStorage.setItem(effortKey, $("effortSelect").value); }
// ---- answer panel --------------------------------------------------------

// Below 900px the panel covers the page (a full-width page of its own on a phone).
const panelCovers = () => matchMedia("(max-width: 900px)").matches;
function panelLoading() {
  const box = node("div", undefined, "panel-loading");
  box.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 4; i++) box.append(node("span", undefined, "ys-skeleton-bar"));
  return box;
}
function panelError(message) { const p = node("p", undefined, "panel-error bad"), words = node("span"); words.append(...errorNodes(message)); p.append(ysIcon("error"), words); return p; }
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
  if (!item.package) {
    const empty = node("p", undefined, "content-empty warn"); empty.append(...errorNodes(item.error || "这项作业还没有资料包。"));
    $("panelMeta").textContent = item.course_name || ""; $("panelContent").replaceChildren(empty); return;
  }
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
  if (g?.status === "completed") meta.push(modelLabel(g.model, recordProvider(g), state.models), recordProvider(g) !== "anthropic" ? PROVIDER_LABELS[recordProvider(g)] : null,
    effortMeta(g, efforts),
    Number.isFinite(g.cost_usd) ? `约 ${g.cost_usd.toFixed(3)} 美元` + (recordProvider(g) === "anthropic_compatible" ? "（估算）" : "") : null);
  meta.push(data.text["draft.md"] ? "未提交" : g?.status === "not_invoked" ? "只整理了资料" : "还没有答案");
  $("panelMeta").replaceChildren(factRow(meta));

  const actions = $("panelActions"); actions.replaceChildren();
  const form = data.form_fill?.forms?.[0], doc = data.document_fill?.documents?.[0];
  const original = externalLink(doc?.url || form?.url, doc ? "打开已填入的文档" : "打开预填表单", "ys-btn ys-btn--sm ys-btn--primary btn small primary");
  if (original) actions.append(original);
  // No form or copy of the teacher's file to write in: the answer goes into a new Doc in the student's own Drive.
  const own = !!data.text["draft.md"] && !original && !!(data.google_doc || data.own_document_suggested);
  if (data.text["draft.md"]) {
    const look = "ys-btn ys-btn--sm btn small" + (own ? " ys-btn--primary primary" : "");
    actions.append(data.google_doc ? externalLink(data.google_doc.url, "打开 Google 文档", look)
      : button("新建 Google 文档", look, event => createGoogleDoc(jobId, key, event.currentTarget)));
  }
  if (data.text["draft.md"]) { const word = button("下载 Word", "ys-btn ys-btn--sm btn small" + (original || own ? "" : " ys-btn--primary primary"), () => downloadAnswer(jobId, key)); word.prepend(ysIcon("download")); actions.append(word); }
  const classroom = externalLink(assignment.alternateLink, "Classroom", "ys-btn ys-btn--sm btn small"); if (classroom) actions.append(classroom);
  actions.append(button("补充后重做", "ys-btn ys-btn--sm btn small", () => openSupplement(jobId, key)));
  actions.append(button("课程规则", "ys-link link small", () => openPolicy(manifest.course_id, manifest.course_name)));
  actions.append(button("证据包", "ys-link link small", () => downloadEvidence(jobId, key)));

  // Notes: one banner, tinted by the most serious line in it.
  const notes = $("panelNotes"); notes.replaceChildren();
  const lines = [];
  if (data.policy_changed) lines.push(["warn", "补充说明或课程规则已变化，需要重做才会生效。"]);
  if (data.text["draft.md"] && !original && data.google_doc)
    lines.push(["info", `答案已写进你云端硬盘里的「${data.google_doc.name}」。在 Classroom 作业页点「添加或创建 → Google 云端硬盘」选中它，核对后点「提出」。`]);
  else if (own)
    lines.push(["info", "这项作业没有你自己的答题文档（表单或老师给每人的副本）。可以点「新建 Google 文档」，在你的云端硬盘新建一份写好答案，再在 Classroom 里附上。"]);
  // The item stopped without an answer (its own form was not read, or the model named what it lacked): why, and what to do.
  const unread = (manifest.warnings || []).filter(w => w.form_unread && (w.origins || []).some(o => o.assignment_id === manifest.assignment_id && o.role === "coursework"));
  if (item.error && !data.text["draft.md"] && item.error !== g?.error && !delivered(item)) {
    const words = node("span"), login = unread.some(w => ["login_required", "login_expired", "account_mismatch"].includes(w.form_unread));
    const job = [...state.jobs, ...state.resultJobs].find(x => x.id === jobId), retry = job && !activeStates.has(job.status) && ["completed_with_issues", "failed", "paused", "interrupted"].includes(job.status);
    if (login && state.schoolLogin?.state === "signed_in") {
      // Signed in since it stopped: the next step is the retry, right here (on a phone the job card is behind this panel).
      words.append(`已登录学校账号 ${state.schoolLogin.email}，这一项可以重试了：重试时会用学校账号读取表单。`);
      lines.push(["info", retry ? [words, button("重试未完成", "ys-btn ys-btn--sm btn small note-action", () => { closePanel(); jobAction(jobId, "retry"); })] : [words]]);
    } else {
      words.append(...errorNodes(item.error));
      lines.push([item.status === "failed" ? "bad" : "warn", login ? [words, button("登录学校账号", "ys-btn ys-btn--sm btn small note-action", openSchoolLogin)] : [words]]);
    }
  }
  if (form) {
    const missing = formContentGaps(data.form_fill), user = formUserInputGaps(data.form_fill, ["user_input_fields"]);
    // A file upload cannot be prefilled: listed, required or not, for the student to upload in the form.
    const unsupported = (data.form_fill.forms || []).flatMap(x => x.unsupported_fields || []);
    const uploads = unsupported.filter(x => x.type === "file_upload"), native = unsupported.filter(x => x.type !== "file_upload" && x.required);
    if (missing.length) lines.push(["warn", `${missing.length} 个必答题没有答案：` + missing.map(x => x.title || x.entry_id).join("；")]);
    if (user.length) lines.push(["warn", `${user.length} 栏需要你本人填写，点“补充后重做”填写后会自动带入表单。`]);
    if (uploads.length) lines.push(["warn", "需要你在表单里自己上传：" + uploads.map(x => (x.title || x.entry_id) + (x.required ? "" : "（选填）")).join("；")]);
    if (native.length) lines.push(["warn", `${native.length} 栏需要在原表单里操作：` + native.map(x => x.title || x.entry_id).join("；")]);
    // Answers too long for Google's prefill link (about 8000 characters): copied here, pasted into the form.
    const paste = (data.form_fill.forms || []).flatMap(x => x.paste_fields || []);
    if (paste.length) lines.push(["warn", [`${paste.length} 栏答案太长，预填链接放不下，没有带入表单。复制后粘贴到表单里对应的栏：`,
      ...paste.map(x => { const b = button("复制" + pasteLabel(x), "ys-btn ys-btn--sm btn small note-action", () => copyAnswer(x)); b.title = x.title || ""; return b; })]]);
    lines.push(["info", "打开预填表单 → 在 Google 表单里逐项核对，确认无误后由你自己点「送信」。作业助手不会、也无法提交。"]);
  }
  // The note counts what the 待核对 tab counts: the questions, and any 资料缺口 listed under them.
  const questions = data.review?.questions || [], gaps = (manifest.warnings || []).length;
  if (questions.length) lines.push(["warn", `${questions.length} 条需要你核对${gaps ? `、${gaps} 处资料缺口` : ""}，见“待核对”。`]);
  const unseen = g?.status === "completed" ? imagesNote(g) : null;
  if (unseen) lines.push(["warn", unseen]);
  if (g?.error && g.status !== "completed") lines.push(["bad", errorNodes(g.error)]);
  if (lines.length) {
    const worst = ["bad", "warn", "info"].find(tone => lines.some(([t]) => t === tone));
    const box = node("div", undefined, `note-box ys-alert ys-alert--${worst}`), body = node("div", undefined, "ys-alert-body note-lines");
    for (const [, line] of lines) { const p = node("p"); p.append(...[line].flat()); body.append(p); }
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
// A form question's answer options as the form shows them: its choices ("Other" in words), a grid's columns once and
// its rows, a date's or time's format.
function questionFacts(question, withType) {
  const fields = question.fields || [], grid = ["grid", "checkbox_grid"].includes(question.type), type = questionTypes[question.type];
  const listed = grid ? fields[0]?.choices || [] : fields.flatMap(field => field.choices || []);
  // "Other" goes last, as the form shows it.
  const choices = [...listed.filter(choice => choice !== OTHER_CHOICE), ...(listed.includes(OTHER_CHOICE) ? ["其他（自由填写）"] : [])];
  const format = ["date", "time"].includes(question.type) ? fields[0]?.format : null;
  const lines = [];
  if ((withType && type) || choices.length || format)
    lines.push(factRow([withType && type && {text: type, cls: "fact-type"}, format && {text: format, cls: "num"}, ...choices.map(choice => ({text: choice, lang: ja(choice), wrap: true}))], "small source-choices fact-row--slash"));
  if (grid && fields.length) lines.push(factRow([{text: "行", cls: "fact-type"}, ...fields.map(field => ({text: field.row || field.entry_id, lang: ja(field.row), wrap: true}))], "small source-choices fact-row--slash"));
  return lines;
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
    parts.push(...questionFacts(value, true));
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
          row.append(...questionFacts(question, ["grid", "checkbox_grid", "date", "time", "file_upload"].includes(question.type)));
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
    // An OpenAI-compatible record names its host, the reply format the server took and what it could not take.
    const openai = g.provider === "openai_compatible";
    const images = openai && Number.isFinite(g.images_sent) ? g.images_sent : g.input?.images?.length || 0;
    const models = factRow(recordModels(g, id => modelLabel(id, recordProvider(g), state.models)).map((text, i) => ({text, cls: i ? "ys-muted" : null, wrap: text.length > 28})));
    const rows = [["状态", status], ["模型", models],
      ["推理强度", effortRecord(g, efforts)], ...(limitRecord(g) ? [["输出上限", limitRecord(g)]] : []),
      ["计费", billingRecord(g)], ["费用", costRecord(g)],
      ...(openai ? [["结构化输出", OUTPUT_MODES[g.structured_output_mode] || "—"]] : []),
      ["用时", Number.isFinite(g.duration_ms) ? durationLabel(g.duration_ms) : "—"], ["输入 / 输出", usage.input_tokens !== undefined ? `${usage.input_tokens + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0)} / ${usage.output_tokens} tokens` : "—"],
      ["送入资料", `${g.input?.source_ids?.length || 0} 段文字 · ${images} 张页图` + (g.images_dropped ? `（${g.images_dropped} 张没有发送：${imagesDroppedWhy(g)}）`
        : !openai && images && (g.degradations || []).includes("images") ? "被接口拒收（Claude Code 去掉了图片）" : "")],
      ["开始", dateLabel(g.started_at, true)], ["结束", g.finished_at ? dateLabel(g.finished_at, true) : "—"]];
    for (const [label, value] of rows) {
      const dd = node("dd", typeof value === "string" ? value : undefined);
      if (Array.isArray(value)) dd.append(value[0], node("span", value[1], "record-note"));
      else if (typeof value !== "string") dd.append(value);
      dl.append(node("dt", label), dd);
    }
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
// A date or time field's own control: the browser's picker where one fits the format, else text in that format.
const DATE_INPUTS = {"YYYY-MM-DD": "date", "YYYY-MM-DD HH:MM": "datetime-local", "HH:MM": "time"};
function formatControl(id, format, value) {
  const control = node("input", undefined, "ys-input format-input"), kind = DATE_INPUTS[format];
  control.type = kind || "text"; control.id = id;
  if (kind) control.value = kind === "datetime-local" ? (value || "").replace(" ", "T") : value || "";
  else { control.value = value || ""; control.placeholder = format; control.inputMode = "numeric"; control.autocomplete = "off"; }
  if (kind === "time" || kind === "datetime-local") control.step = "60";
  return [control, () => { const text = control.value.trim().replace("T", " "); return text ? [text] : []; }];
}
// An "Other" option: a choice of its own and the words that go with it.
function otherWords(id, supplied) {
  const input = node("input", undefined, "ys-input other-input"); input.type = "text"; input.id = id + "-other"; input.maxLength = 2000;
  input.placeholder = "其他：在这里填写"; input.setAttribute("aria-label", "其他选项的内容");
  const found = (supplied?.values || []).find(x => x.startsWith(OTHER_CHOICE)); input.value = found ? found.slice(OTHER_CHOICE.length) : "";
  return input;
}
function renderAnswerInputs(data) {
  const content = $("answerInputs"); content.replaceChildren();
  const fields = supplementFields(data);
  $("answerInputsSection").hidden = !fields.length && !data?.response_fields_error;
  $("answerInputNote").textContent = data?.response_fields_error || "按原题填写或选择。回答只保存在小主机上，会自动带入预填表单。";
  $("extraSupplement").open = !fields.length;
  for (const {form, question, field, supplied} of fields) {
    const id = `answer-input-${state.supplement.editors.length}`, type = question.type;
    const several = (type === "checkbox" || type === "checkbox_grid") && field.choices.length;
    // 第 N 页 · the question (in Japanese) · a grid's row · 必填 or 选填
    const caption = [`第 ${question.page} 页 · `, withLang(node("span", question.title), question.title)];
    if (field.row) caption.push(" · ", withLang(node("span", field.row), field.row));
    caption.push(field.required ? "（必填）" : "（选填）");
    const group = node(several ? "fieldset" : "div", undefined, several ? "choice-set answer-field" : "ys-field answer-field");
    if (several) { const legend = node("legend"); legend.append(...caption); group.append(legend); }
    else { const label = node("label", undefined, "ys-field-label"); label.append(...caption); label.htmlFor = id; group.append(label); }
    if (question.description) group.append(withLang(node("p", question.description, "ys-field-help small muted"), question.description));
    const choices = field.choices.filter(x => x !== OTHER_CHOICE);
    let read;
    if (["single_choice", "dropdown", "scale", "grid"].includes(type) && field.choices.length) {
      const control = node("select"); const empty = node("option", "请选择"); empty.value = ""; control.append(empty);
      for (const value of choices) { const option = withLang(node("option", value), value); option.value = value; control.append(option); }
      const other = field.other ? otherWords(id, supplied) : null;
      if (other) { const option = node("option", "其他…"); option.value = OTHER_CHOICE; control.append(option); }
      const current = supplied?.values?.[0] || "";
      control.value = other && current.startsWith(OTHER_CHOICE) ? OTHER_CHOICE : current;
      const wrap = node("span", undefined, "ys-select"); wrap.append(control, ysIcon("chevron-down")); control.id = id; group.append(wrap);
      if (other) { other.hidden = control.value !== OTHER_CHOICE; control.addEventListener("change", () => { other.hidden = control.value !== OTHER_CHOICE; if (!other.hidden) other.focus(); }); group.append(other); }
      read = () => control.value === OTHER_CHOICE ? (other.value.trim() ? [OTHER_CHOICE + other.value.trim()] : []) : control.value ? [control.value] : [];
    } else if (["short_text", "paragraph"].includes(type)) {
      const control = node(type === "paragraph" ? "textarea" : "input", undefined, "ys-input"); if (type === "paragraph") control.rows = 3; else control.type = "text";
      control.maxLength = 20000; control.value = supplied?.values?.[0] || ""; read = () => control.value.trim() ? [control.value.trim()] : [];
      control.id = id; group.append(control);
    } else if (several) {
      const options = []; group.id = id;
      for (const value of choices) { const option = node("label", undefined, "ys-check-label inline"), input = node("input", undefined, "ys-check"); input.type = "checkbox"; input.value = value; input.checked = supplied?.values?.includes(value) || false; option.append(input, withLang(node("span", value), value)); group.append(option); options.push(input); }
      let other = null, otherBox = null;
      if (field.other) {
        other = otherWords(id, supplied); otherBox = node("input", undefined, "ys-check"); otherBox.type = "checkbox"; otherBox.checked = !!other.value;
        const option = node("label", undefined, "ys-check-label inline"); option.append(otherBox, node("span", "其他")); group.append(option, other);
        other.addEventListener("input", () => { if (other.value.trim()) otherBox.checked = true; });
      }
      read = () => [...options.filter(x => x.checked).map(x => x.value), ...(otherBox?.checked && other.value.trim() ? [OTHER_CHOICE + other.value.trim()] : [])];
    } else if (["date", "time"].includes(type) && field.format) {
      const [control, value] = formatControl(id, field.format, supplied?.values?.[0]);
      group.append(control); if (!DATE_INPUTS[field.format]) group.append(node("p", `按 ${field.format} 填写。`, "ys-field-help small muted"));
      read = value;
    } else { group.append(node("p", type === "file_upload" ? "这一题要在原表单里自己上传文件。" : "这一题需要在原表单里填写。", "answer-native small muted")); content.append(group); continue; }
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
  const s = state.supplement, active = activeProvider();
  const name = s.model ? modelLabel(s.model, active, state.models) : state.models?.provider === active ? modelLabel(state.models.default_model, active, state.models) : "默认模型";
  $("supplementModel").textContent = answersOnly ? "只合并你的回答，不重新调用 AI。" : redoLine(name, active, s.from);
}
async function openSupplement(jobId, key) {
  try {
    const job = state.jobs.find(x => x.id === jobId) || await api(`/api/jobs/${jobId}`), item = job.items.find(x => keyOf(x) === key);
    const data = state.panel?.key === key && state.panel.data ? state.panel.data : item.package ? await api(`/api/jobs/${jobId}/items/${key}`) : null;
    const supplement = data?.supplement || await api(`/api/jobs/${jobId}/items/${key}/supplement`);
    // 强度 默认 sends no effort: a Claude job's effort never rides along onto the endpoint (redoChoice).
    const toolbar = {provider: state.models?.provider, model: $("modelSelect").value, effort: $("effortSelect").value};
    const from = data?.generation && data.generation.status !== "not_invoked" ? recordProvider(data.generation) : jobProvider(job);
    state.supplement = {jobId, key, ...redoChoice(toolbar, job, activeProvider()), provider: activeProvider(), from, editors: [], savedText: supplement.text || "", savedFacts: supplement.personal_facts || []};
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
  // The fill makes a new prefill link. A fill still running reopens the panel when it ends (pollJob); one that ended
  // before the state was read (a fill takes about a second) reopens it here, so 打开预填表单 never keeps the old link.
  if (apply && result?.id && state.panel && state.active !== result.id) openPanel(result.id, state.panel.key, state.panel.tab);
  return result;
}
async function saveSupplement(continueRun) {
  const {jobId, key, model, effort, provider} = state.supplement;
  if (state.supplement.editors.length && !supplementNotesChanged()) return saveFormInputs(continueRun);
  if (state.supplement.editors.length) await saveFormInputs(false, false);
  const payload = {text: $("supplementText").value, personal_facts: lines("supplementFacts")};
  if (continueRun) Object.assign(payload, {ai_confirmed: $("supplementAi").checked, model, effort, provider});
  const job = await api(`/api/jobs/${jobId}/items/${key}/${continueRun ? "continue" : "supplement"}`, "POST", payload);
  $("supplementDialog").close(); await loadState();
  if (continueRun && job?.id) { state.focus = job.id; state.dismissed = ""; renderJob(); }
  toast(continueRun ? "已保存，正在重做这项作业" : "补充已保存，下次处理时使用", "ok");
}

// ---- wiring --------------------------------------------------------------

$("refreshButton").addEventListener("click", () => action("/api/refresh", {}));
$("generateButton").addEventListener("click", startSelected);
$("openSettings").addEventListener("click", openSettings);
$("closePanel").addEventListener("click", closePanel);
document.addEventListener("keydown", event => { if (event.key === "Escape" && state.panel && !document.querySelector("dialog[open]")) closePanel(); });
for (const close of document.querySelectorAll(".close-dialog")) close.addEventListener("click", () => close.closest("dialog").close());
$("driveAuthorize").addEventListener("click", () => openDriveAuth());
$("driveForget").addEventListener("click", async () => {
  if (!confirm("取消授权？小主机会删掉这个权限；已经新建的文档仍留在你的云端硬盘里。")) return;
  try { drive.status = await api("/api/google-docs/forget", "POST", {}); renderDrive(); toast("已取消授权。", "ok"); } catch (error) { toast(error.message, "bad"); }
});
// The pasted address holds a one-time code: it leaves the field as soon as it is sent, or the dialog closes.
$("driveDialog").addEventListener("close", () => { $("driveUrl").value = ""; drive.then = null; });
$("driveForm").addEventListener("submit", async event => {
  event.preventDefault();
  const submit = $("driveForm").querySelector("[type=submit]"), pasted = $("driveUrl").value.trim(), then = drive.then;
  submit.disabled = true; $("driveError").hidden = true;
  try {
    drive.status = await api("/api/google-docs/finish", "POST", {url: pasted});
    drive.then = null; $("driveDialog").close(); renderDrive(); toast(`已授权 ${drive.status.email}。`, "ok");
    if (then) then();
  } catch (error) { $("driveError").textContent = error.message; $("driveError").hidden = false; $("driveError").parentElement.scrollIntoView({block: "nearest"}); }
  finally { submit.disabled = false; }
});
$("aiConfirmed").checked = localStorage.getItem("classroom-ai-enabled") !== "false";
$("aiConfirmed").addEventListener("change", () => { localStorage.setItem("classroom-ai-enabled", String(aiEnabled())); renderButtons(); renderIssues(); });
$("modelSelect").addEventListener("change", () => { renderEfforts(); saveModelChoice(); });
$("effortSelect").addEventListener("change", saveModelChoice);
$("searchInput").addEventListener("input", () => { renderList(); renderButtons(); });
$("courseFilter").addEventListener("change", () => { renderList(); renderButtons(); });
$("selectAll").addEventListener("change", () => { const latest = latestItems(); for (const item of selectTargets(visibleItems(latest), new Set(), latest)) $("selectAll").checked ? state.selected.add(keyOf(item)) : state.selected.delete(keyOf(item)); renderList(); renderButtons(); });
$("clearSelection").addEventListener("click", () => { state.selected.clear(); renderList(); renderButtons(); });
$("apiKeyForm").addEventListener("submit", event => { event.preventDefault(); saveClaude(); });
$("apiKeyClear").addEventListener("click", clearClaude);
for (const input of [$("apiKeyInput"), $("claudeMaxTokens")]) input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
$("providerSeg").addEventListener("click", event => { const b = event.target.closest("button[data-provider]"); if (b) chooseProvider(b.dataset.provider); });
for (const name in ENDPOINTS) {
  const el = ENDPOINTS[name].el();
  el.form.addEventListener("submit", event => { event.preventDefault(); saveEndpoint(name); });
  el.clear.addEventListener("click", () => clearEndpoint(name));
  for (const input of [el.base, el.key, el.models, el.limit]) input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
  for (const type of ["input", "focus"]) el.models.addEventListener(type, () => renderModelSuggestions(name));
}
for (const b of document.querySelectorAll("[data-test-provider]")) b.addEventListener("click", () => testConnection(b.dataset.testProvider));
// 设置 → 学校账号 and its sign-in view.
$("schoolSignIn").addEventListener("click", openSchoolLogin);
$("schoolLogout").addEventListener("click", schoolLogout);
$("schoolClose").addEventListener("click", cancelSchool);
$("schoolCancel").addEventListener("click", cancelSchool);
$("schoolFinish").addEventListener("click", finishSchool);
$("schoolTakeover").addEventListener("click", takeoverSchool);
$("schoolDialog").addEventListener("close", schoolClosed);
$("schoolSend").addEventListener("click", () => sendSchoolText(false));
$("schoolBack").addEventListener("click", () => { showSchoolError(null); school.chain = school.chain.then(() => api("/api/school-login/back", "POST", {})).catch(error => showSchoolError(error.message)); });
for (const b of document.querySelectorAll("[data-school-key]")) b.addEventListener("click", () => { showSchoolError(null); schoolInput({type: "key", key: b.dataset.schoolKey}); });
// The buttons beside the text field do not take the focus from it, so a phone's keyboard stays up while they are used.
for (const b of [$("schoolSend"), $("schoolReveal"), ...document.querySelectorAll("[data-school-key]")]) b.addEventListener("pointerdown", event => { if (document.activeElement === $("schoolText")) event.preventDefault(); });
$("schoolReveal").addEventListener("click", () => { school.reveal = !school.reveal; renderSchoolDialog(); });
// A touch screen's keyboard is up while the text field has the focus: the picture then keeps the view's width.
$("schoolText").addEventListener("focus", () => setSchoolTyping(true));
$("schoolText").addEventListener("blur", () => setSchoolTyping(false));
// 回车 on the keyboard sends the text and presses Enter on the page; not while an input method is still composing.
$("schoolText").addEventListener("keydown", event => { if (event.key === "Enter" && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); sendSchoolText(true); } });
$("schoolFrame").addEventListener("pointerdown", event => {
  if (!school.frame || school.ended) return;
  event.preventDefault();
  school.drag = {id: event.pointerId, x: event.clientX, y: event.clientY, lastX: event.clientX, lastY: event.clientY, moved: false, touch: event.pointerType !== "mouse"};
  try { $("schoolFrame").setPointerCapture(event.pointerId); } catch { /* a pointer the browser no longer tracks */ }
});
$("schoolFrame").addEventListener("pointermove", event => {
  const drag = school.drag;
  if (!drag || drag.id !== event.pointerId) return;
  if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 8) drag.moved = true;
  if (drag.moved && drag.touch) { queueSchoolScroll(event, drag.lastX - event.clientX, drag.lastY - event.clientY); drag.lastX = event.clientX; drag.lastY = event.clientY; }
});
$("schoolFrame").addEventListener("pointerup", event => {
  const drag = school.drag; school.drag = null;
  if (!drag || drag.id !== event.pointerId || !school.frame || school.ended) return;
  if (drag.moved) { flushSchoolScroll(); return; }
  showSchoolError(null); schoolInput({type: "tap", ...schoolPoint(event)});
});
$("schoolFrame").addEventListener("pointercancel", () => { school.drag = null; flushSchoolScroll(); });
$("schoolFrame").addEventListener("wheel", event => {
  if (!school.frame || school.ended) return;
  event.preventDefault();
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;  // lines or pages, as pixels
  queueSchoolScroll(event, event.deltaX * unit, event.deltaY * unit);
}, {passive: false});
$("schoolFrame").addEventListener("contextmenu", event => event.preventDefault());
// A page that goes away ends its sign-in at once (the server would also end it once the polling stops).
window.addEventListener("pagehide", () => {
  if (!school.started || school.ended) return;
  try { fetch(BASE + "/api/school-login/cancel", {method: "POST", keepalive: true, credentials: BASE ? "same-origin" : "omit", headers: {"Content-Type": "application/json", ...(token ? {Authorization: "Bearer " + token} : {})}, body: "{}"}); } catch { /* the server ends it anyway */ }
});
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
// The open panel is read again without blanking it, from the item's newest record (a fill or a redo started from here,
// another tab or the phone), and drawn again only when something changed, keeping the reader's place.
async function refreshPanel() {
  const panel = state.panel;
  if (!panel?.data) return;
  const latest = latestItems().get(panel.key), jobId = latest?.job.id || panel.jobId, item = latest?.item || panel.item;
  try {
    const data = await api(`/api/jobs/${jobId}/items/${panel.key}`);
    if (state.panel !== panel) return;
    if (jobId === panel.jobId && item.status === panel.item.status && item.error === panel.item.error && JSON.stringify(data) === JSON.stringify(panel.data)) return;
    const scroll = $("panel").scrollTop;
    Object.assign(panel, {jobId, item, data}); renderPanel(); $("panel").scrollTop = scroll;
  } catch { /* the next sync tries again */ }
}
async function syncWhenIdle() {
  if ((!token && !BASE) || state.active || state.busy || document.hidden || document.querySelector("dialog[open]")) return;
  try {
    await loadState();
    await refreshPanel();
    const age = Date.now() - Date.parse(state.pending?.refreshed_at || 0);
    if (!state.active && !(age < 15 * 60 * 1000) && Date.now() - lastRefreshAttempt > 60000) { lastRefreshAttempt = Date.now(); await action("/api/refresh", {}); }
  } catch (error) { state.error = error.message; renderIssues(); }
}
setInterval(syncWhenIdle, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) syncWhenIdle(); });
// A page the browser restores from its back-forward cache (the Back button, or returning to the app on a phone) is the
// page as it was when left: read everything again.
window.addEventListener("pageshow", event => { if (event.persisted) syncWhenIdle(); });

renderAll();
if (token || BASE) loadState().then(() => { loadModels(); syncWhenIdle(); }).catch(error => { booting = false; state.error = error.message; renderHeader(); renderIssues(); renderList(); });
