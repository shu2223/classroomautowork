// Test the actual frontend helpers, with no browser or network dependency.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/classroomautowork/web/app.js'), 'utf8');
const code = source.split('// Pure presentation; exercised by tests/progress_view.js.')[1].split('// End pure presentation.')[0];
const names = ['answersIncomplete', 'hasAnswer', 'delivered', 'dueInfo', 'filterMatch', 'selectTargets', 'rowStatus', 'jobSummary', 'durationLabel'];
const labels = {waiting: '等待中', preparing: '读取资料', drafting: '作答中', failed: '失败', paused: '已暂停', materials_ready: '资料已整理'};
const f = vm.runInNewContext(code + `\n({${names.join(',')}})`, {activeStates: new Set(['queued', 'running', 'stopping']), labels, keyOf: x => x.course_id + ':' + x.assignment_id, STALE_MS: 180000});

const now = Date.parse('2026-11-02T00:00:00Z');
const word = {answer_document: {files: {'answer.docx': {}}}};

// Due dates: overdue, today, days ahead and undated work.
assert.equal(f.dueInfo({due_utc: '2026-11-01T00:00:00Z'}, now).overdue, true);
assert.equal(f.dueInfo({due_utc: '2026-11-02T10:00:00Z'}, now).text, '今天');
assert.equal(f.dueInfo({due_utc: '2026-11-05T01:00:00Z'}, now).text, '3 天后');
assert.equal(f.dueInfo({}, now).days, null);

// Filters count what the chip says.
const soon = {due_utc: '2026-11-04T00:00:00Z'}, later = {due_utc: '2026-11-20T00:00:00Z'}, late = {due_utc: '2026-10-01T00:00:00Z'};
assert.ok(f.filterMatch('week', soon, null, now) && !f.filterMatch('week', later, null, now) && !f.filterMatch('week', late, null, now));
assert.ok(f.filterMatch('overdue', late, null, now) && f.filterMatch('nodue', {}, null, now));
assert.ok(f.filterMatch('review', soon, {item: word}, now) && !f.filterMatch('review', soon, null, now));

// A delivered answer is skipped by one-click; an answer with unanswered form fields is not.
const ready = {status: 'ready', ...word}, gap = {status: 'form_opened', form_fill: {forms: [{url: 'x'}], remaining_answer_fields: [{entry_id: '1'}]}};
assert.ok(f.delivered(ready) && !f.delivered(gap) && f.answersIncomplete(gap) && f.hasAnswer(gap));
const items = [1, 2, 3].map(n => ({course_id: '1', assignment_id: String(n)}));
const latest = new Map([['1:1', {item: ready}], ['1:2', {item: gap}]]);
assert.deepEqual(f.selectTargets(items, new Set(), latest).map(x => x.assignment_id), ['2', '3']);
assert.deepEqual(f.selectTargets(items, new Set(['1:3']), latest).map(x => x.assignment_id), ['3']);
assert.deepEqual(f.selectTargets(items, new Set(['1:1']), latest).map(x => x.assignment_id), []);

// Row status: running, answer, needs input, failure with and without a package, untouched.
const running = {status: 'running'}, done = {status: 'completed'};
assert.deepEqual({...f.rowStatus({job: running, item: {status: 'drafting'}})}, {text: '作答中', tone: 'running', open: false});
assert.equal(f.rowStatus({job: done, item: ready}).text, '答案 ›');
assert.equal(f.rowStatus({job: done, item: gap}).tone, 'warn');
assert.deepEqual({...f.rowStatus({job: done, item: {status: 'failed', package: '/p'}})}, {text: '失败 ›', tone: 'bad', open: true});
assert.equal(f.rowStatus({job: done, item: {status: 'failed'}}).open, false);
assert.equal(f.rowStatus(undefined).text, '未开始');

// Job summary: progress, current item, step and stale warnings never invent completion.
const job = {status: 'running', created_at: '2026-11-01T23:50:00Z', started_at: '2026-11-01T23:50:00Z', updated_at: '2026-11-01T23:59:30Z', events: [],
  items: [{status: 'ready'}, {status: 'drafting', title: 'B', generation: {activity_at: '2026-11-01T23:59:50Z'}}, {status: 'waiting'}], runtime: {worker_alive: true}};
let s = f.jobSummary(job, now);
assert.equal(s.done, 1); assert.equal(s.total, 3); assert.equal(s.current.title, 'B'); assert.equal(s.step, 1);
assert.equal(Math.round(s.percent), 33); assert.equal(s.warning, null); assert.equal(s.elapsed, 600000);
s = f.jobSummary({...job, progress: {stage: 'validate'}}, now); assert.equal(s.step, 2);
s = f.jobSummary({...job, estimate: {state: 'estimating', overall: {percent: 47.5, remaining_seconds: 90}}}, now);
assert.equal(s.percent, 47.5); assert.equal(s.remaining, 90);
s = f.jobSummary({...job, estimate: {state: 'calibrating', overall: {percent: 80, remaining_seconds: 5}}}, now);
assert.equal(s.remaining, null);
const quiet = {...job, updated_at: '2026-11-01T23:50:00Z', items: [{status: 'drafting', generation: {activity_at: '2026-11-01T23:50:00Z'}}]};
assert.match(f.jobSummary(quiet, now).warning, /10 分钟没有新进展/);
assert.equal(f.jobSummary({...quiet, status: 'stopping'}, now).warning, null);
assert.match(f.jobSummary({...job, runtime: {worker_alive: false}}, now).warning, /不在运行/);
assert.equal(f.jobSummary({...job, status: 'completed', finished_at: '2026-11-01T23:55:00Z'}, now).elapsed, 300000);
assert.equal(f.durationLabel(65000), '1 分 5 秒');
console.log('Frontend state checks passed');
