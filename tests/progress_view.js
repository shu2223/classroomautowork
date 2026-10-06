// Test the actual frontend helpers, with no browser or network dependency.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/classroomautowork/web/app.js'), 'utf8');
const code = source.split('// Pure progress presentation;')[1].split('// End pure progress presentation.')[0];
const {progressView} = vm.runInNewContext('// ' + code + '\n({progressView});', {activeStates:new Set(['queued','running','stopping']), labels:{failed:'失败',completed:'结束'}});
const now = Date.parse('2026-01-01T00:10:00Z'), network = {lastSuccess:now,error:null};
const job = {status:'running',kind:'review',items:[{status:'preparing'}],created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z',message:'Checking attachment offline-id',events:[]};
let v = progressView(job, network, now);
assert.equal(v.measurement,null); assert.equal(v.legacy,true); assert.equal(v.workerAlive,false); assert.match(v.warning,/没有新进展/);
v = progressView({...job,progress:{stage:'download',current:0,total:10,unit:'bytes',activity_at:'2026-01-01T00:09:59Z'}},network,now);
assert.equal(v.measurement.value,0); assert.equal(v.warning,null);
v = progressView({...job,items:[{status:'drafting'}],message:'AI running'},network,now);
assert.equal(v.stage,'ai'); assert.equal(v.measurement,null); assert.equal(v.index,1);
v = progressView({...job,progress:{stage:'transcribe',current:1,total:3,unit:'segments',activity_at:'2026-01-01T00:02:00Z'}},network,now);
assert.equal(v.measurement.value,1); assert.match(v.warning,/不能仅凭时间/);
v = progressView({...job,approval:{requested_at:'2026-01-01T00:09:00Z'}},network,now);
assert.equal(v.stage,'approval'); assert.match(v.title,/审批/); assert.match(v.warning,/不会继续/);
v = progressView(job,{lastSuccess:now-15000,error:'offline'},now);
assert.equal(v.disconnected,true); assert.match(v.warning,/不一定停止/);
v = progressView({...job,status:'failed'},network,now);
assert.equal(v.index,0); assert.equal(v.ready,0); assert.equal(v.measurement,null);
v = progressView({...job,status:'completed',items:[{status:'document_ready'}],finished_at:'2026-01-01T00:08:00Z'},network,now);
assert.equal(v.ready,1); assert.equal(v.finished,1); assert.equal(v.index,3); assert.equal(v.elapsed,480000);
console.log('Production progress-state checks passed');
v = progressView({...job,status:'completed',items:[{status:'ready'}],finished_at:'2026-01-01T00:08:00Z'},network,now);
assert.equal(v.localReady,1); assert.equal(v.ready,0); assert.equal(v.finished,1); assert.equal(v.index,3);
assert.match(v.explanation,/继续自动填写/);
v = progressView({...job,items:[{status:'filling_form'}],message:'Opening native form'},network,now);
assert.equal(v.stage,'form_fill'); assert.equal(v.index,2); assert.match(v.explanation,/不会点击提交/);
v = progressView({...job,status:'completed',items:[{status:'form_opened'}]},network,now);
assert.equal(v.ready,1); assert.equal(v.index,3); assert.match(v.explanation,/预填原表单审阅/);

const editorCode = source.slice(source.indexOf('function supplementFields('), source.indexOf('function renderAnswerInputs('));
const {supplementFields} = vm.runInNewContext(editorCode + '\n({supplementFields});');
const form = {url:'unit-form', context_sha256:'unit-context', questions:[
  {page:2,title:'2) Actual situation',fields:[{entry_id:'20',choices:['A','B・C']}]},
  {page:1,title:'1) Known identity',fields:[{entry_id:'10',choices:[]}]},
  {page:2,title:'1) Other situation',fields:[{entry_id:'30',choices:[]}]}
]};
const base = {response_fields:[form], model_form_answers:[{form_url:form.url,entry_id:'10',values:['Known user identity']}]};
let fields = supplementFields(base);
assert.equal(fields.length,2); assert.equal(fields[0].field.entry_id,'30');
assert.equal(fields[1].field.choices[1],'B・C'); assert.equal(fields[1].supplied,undefined);
fields = supplementFields({...base,user_form_answers:{answers:[{form_url:form.url,entry_id:'10',values:['Known user identity']} ]}});
assert.equal(fields.length,3); assert.equal(fields[0].supplied.values[0],'Known user identity');
assert.equal(supplementFields(null).length,0);
console.log('Production native-question editor checks passed');
