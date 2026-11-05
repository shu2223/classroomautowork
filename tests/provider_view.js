// The model-provider helpers of the page (app.js "Provider presentation"), with no browser or network.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/classroomautowork/web/app.js'), 'utf8');
const code = source.split('// Provider presentation; exercised by tests/provider_view.js.')[1].split('// End provider presentation.')[0];
const names = ['jobProvider', 'recordProvider', 'modelLabel', 'stepNames', 'redoChoice', 'redoLine', 'imagesDroppedWhy', 'imagesNote', 'recordModels', 'savedMessage'];
const f = vm.runInNewContext(code + `\n({${names.join(',')}})`, {});

// A job's provider: recorded; earlier AI review jobs were Claude's; a fill job and a materials-only job have none.
assert.equal(f.jobProvider({provider: 'openai', kind: 'review'}), 'openai');
assert.equal(f.jobProvider({kind: 'review', ai_confirmed: true}), 'anthropic');
assert.equal(f.jobProvider({kind: 'fill', ai_confirmed: true}), null);
assert.equal(f.jobProvider({kind: 'review', ai_confirmed: false}), null);
assert.equal(f.recordProvider({provider: 'openai_compatible'}), 'openai');
assert.equal(f.recordProvider({provider: 'anthropic'}), 'anthropic');
assert.equal(f.recordProvider({}), 'anthropic');

// Claude models keep their display names while the toolbar lists the endpoint's models.
const claudeNames = {'claude-haiku-5-5': 'Claude Haiku 5.5'};
const openaiList = {provider: 'openai', models: [{model: 'demo-gpt-5', displayName: 'demo-gpt-5'}], claude_names: claudeNames};
const claudeList = {provider: 'anthropic', models: [{model: 'claude-haiku-5-5', displayName: 'Claude Haiku 5.5'}], claude_names: claudeNames};
assert.equal(f.modelLabel('claude-haiku-5-5', 'anthropic', openaiList), 'Claude Haiku 5.5');
assert.equal(f.modelLabel('claude-haiku-5-5', null, openaiList), 'Claude Haiku 5.5');
assert.equal(f.modelLabel('claude-haiku-5-5', 'anthropic', claudeList), 'Claude Haiku 5.5');
assert.equal(f.modelLabel('demo-gpt-5', 'openai', openaiList), 'demo-gpt-5');
// An endpoint's model is named by its id even if it shares a Claude id; without any list, the id.
assert.equal(f.modelLabel('claude-haiku-5-5', 'openai', claudeList), 'claude-haiku-5-5');
assert.equal(f.modelLabel('claude-haiku-5-5', 'anthropic', null), 'claude-haiku-5-5');

// The job card's answering step: the job's provider, or the active one for a job that answers nothing.
assert.equal(f.stepNames({provider: 'openai'}, 'anthropic')[1], '模型作答');
assert.equal(f.stepNames({kind: 'review', ai_confirmed: true}, 'openai')[1], 'Claude 作答');
assert.equal(f.stepNames({kind: 'fill', ai_confirmed: true}, 'openai')[1], '模型作答');
assert.equal(f.stepNames({kind: 'fill', ai_confirmed: true}, 'anthropic')[1], 'Claude 作答');

// 补充后重做 with the endpoint's 强度 默认 sends no effort, even for an answer Claude wrote at 很高.
const claudeJob = {kind: 'review', ai_confirmed: true, provider: 'anthropic', model: 'claude-opus-5-5', effort: 'xhigh'};
assert.deepEqual({...f.redoChoice({provider: 'openai', model: 'demo-gpt-5', effort: ''}, claudeJob, 'openai')}, {model: 'demo-gpt-5', effort: null});
assert.deepEqual({...f.redoChoice({provider: 'openai', model: 'demo-gpt-5', effort: 'low'}, claudeJob, 'openai')}, {model: 'demo-gpt-5', effort: 'low'});
assert.deepEqual({...f.redoChoice({provider: 'anthropic', model: 'claude-sonnet-5-5', effort: 'high'}, claudeJob, 'anthropic')}, {model: 'claude-sonnet-5-5', effort: 'high'});
// No list for the active provider: the job's own choice only if it used that provider, else the server's defaults.
assert.deepEqual({...f.redoChoice({provider: undefined, model: '没有模型', effort: '—'}, claudeJob, 'anthropic')}, {model: 'claude-opus-5-5', effort: 'xhigh'});
assert.deepEqual({...f.redoChoice({provider: 'anthropic', model: 'claude-opus-5-5', effort: 'high'}, claudeJob, 'openai')}, {model: null, effort: null});

// The line above 保存并重做 names the provider when the answer came from the other one.
assert.equal(f.redoLine('demo-gpt-5', 'openai', 'openai'), '重做会使用 demo-gpt-5。');
assert.equal(f.redoLine('demo-gpt-5', 'openai', 'anthropic'), '重做会改用 OpenAI 兼容接口的 demo-gpt-5。');
assert.equal(f.redoLine('Claude Sonnet 5.5', 'anthropic', 'openai'), '重做会改用 Claude API 的 Claude Sonnet 5.5。');
assert.equal(f.redoLine('Claude Sonnet 5.5', 'anthropic', null), '重做会使用 Claude Sonnet 5.5。');

// Page images the endpoint never saw are said in the panel, with the reason.
assert.equal(f.imagesNote({images_dropped: 0}), null);
assert.equal(f.imagesNote({}), null);
assert.match(f.imagesNote({images_dropped: 2, degradations: ['images']}), /^2 张页面图片没有发给模型（接口不接受图片）/);
assert.match(f.imagesNote({images_dropped: 3, degradations: ['images_size']}), /（请求超出了接口的上限）/);
assert.equal(f.imagesDroppedWhy({degradations: ['json_schema', 'images']}), '接口不接受图片');

// The 记录 tab's 模型 row: the reported model, and the chosen one as its own fact when the server named another.
const label = id => id;
assert.deepEqual([...f.recordModels({provider: 'openai_compatible', model: 'demo-gpt-5-staging', requested_model: 'demo-gpt-5'}, label)], ['demo-gpt-5-staging', '所选 demo-gpt-5']);
assert.deepEqual([...f.recordModels({provider: 'openai_compatible', model: 'demo-gpt-5', requested_model: 'demo-gpt-5'}, label)], ['demo-gpt-5']);
assert.deepEqual([...f.recordModels({provider: 'anthropic', model: 'claude-x', requested_model: 'claude-y'}, label)], ['claude-x']);

// 保存并验证's toast says what was checked: a list that answers without the key did not check the key.
assert.equal(f.savedMessage({models_listed: true, key_checked: true, unlisted_models: []}, false), '已验证并保存');
assert.match(f.savedMessage({models_listed: true, key_checked: false, unlisted_models: []}, true), /^已保存，作答改用这个接口。这个接口不用密钥也能读取模型列表/);
assert.match(f.savedMessage({models_listed: false}, false), /没有提供模型列表/);
assert.match(f.savedMessage({models_listed: true, key_checked: null, unlisted_models: ['x']}, false), /^已验证并保存。接口的模型列表里没有 x/);
console.log('Provider view checks passed');
