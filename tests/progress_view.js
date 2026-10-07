// Test the actual frontend helpers, with no browser or network dependency.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/classroomautowork/web/app.js'), 'utf8');
const code = source.split('// Pure progress presentation;')[1].split('// End pure progress presentation.')[0];
const {progressView,resumeTarget,completedSuccessor} = vm.runInNewContext('// ' + code + '\n({progressView,resumeTarget,completedSuccessor});', {activeStates:new Set(['queued','running','stopping']), labels:{failed:'失败',completed:'结束'},keyOf:x=>x.course_id+':'+x.assignment_id});
const now = Date.parse('2026-01-01T00:10:00Z'), network = {lastSuccess:now,error:null};
const job = {status:'running',kind:'review',items:[{status:'preparing'}],created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z',message:'Checking attachment offline-id',events:[]};
let v = progressView(job, network, now);
assert.equal(v.measurement,null); assert.equal(v.legacy,true); assert.equal(v.workerAlive,false); assert.match(v.warning,/没有新进展/);
v = progressView({...job,progress:{stage:'download',current:0,total:10,unit:'bytes',activity_at:'2026-01-01T00:09:59Z'}},network,now);
assert.equal(v.measurement.value,0); assert.equal(v.warning,null);
v = progressView({...job,items:[{status:'drafting'}],message:'AI running'},network,now);
assert.equal(v.stage,'ai'); assert.equal(v.measurement,null); assert.equal(v.index,1);
assert.equal(v.ai,null); // A live worker alone must not claim a model session exists.
const generation = {thread_id:'00000000-0000-0000-0000-000000000001',status:'running',model:'unit-model',activity_count:12,activity_at:'2026-01-01T00:09:59Z',activity_label:'Codex 正在输出回答，完整返回后再核验',events:[{type:'userMessage',status:'completed'},{type:'commandExecution',status:'completed'},{type:'commandExecution',status:'failed'},{type:'reasoning',status:'completed'}]};
const aiJob = {...job,message:'Model active',progress:{stage:'ai',activity_at:'2026-01-01T00:00:00Z'},items:[{status:'drafting',generation}]};
v = progressView(aiJob,network,now);
assert.equal(v.measurement,null); assert.match(v.title,/输出回答/);
assert.equal(v.silence,1000); assert.equal(v.warning,null); // Prefer the newest real model activity over an old stage log.
assert.match(v.ai.text,/12 次实际活动更新/); assert.match(v.ai.text,/1 次工具活动/);
v = progressView({...aiJob,events:[{at:'2026-01-01T00:04:00Z',message:'Codex 正在输出回答，完整返回后再核验'}]},network,now);
assert.match(v.warning,/等待时间异常/); assert.match(v.warning,/不代表答案能正常完成/);
assert.equal(v.active,true); // An alert must not cancel a user-preserved running turn.
v = progressView({...aiJob,items:[{status:'drafting',generation:{...generation,activity_at:'2026-01-01T00:00:00Z'}}]},network,now);
assert.match(v.warning,/未收到新的模型活动通知/); assert.match(v.warning,/不能仅凭服务连接正常/);
v = progressView({...aiJob,status:'completed'},network,now); assert.equal(v.ai,null);
const uiCode = source.slice(source.indexOf('function jobProgress('),source.indexOf('function jobCard('));
function element(tag,text,className) { return {tag,textContent:text,className,children:[],attributes:{},dataset:{},style:{},classList:{add(value){this.value=value;}},append(...children){this.children.push(...children);},setAttribute(key,value){this.attributes[key]=value;}}; }
const {jobProgress} = vm.runInNewContext(uiCode+'\n({jobProgress});',{progressView,connection:network,node:element,Date:{now:()=>now},state:{monitorOnly:false},resumeTarget,codexLink:g=>g?.thread_id ? element('a','Session') : null,friendlyError:x=>x,durationLabel:x=>String(x)});
let detail = jobProgress(aiJob), bar = detail.children.find(x=>x.className === 'job-progress');
assert.equal(bar.tag,'div'); assert.equal(bar.children[0].className,'job-progress-sweep');
assert.equal(bar.value,undefined); assert.match(bar.children[0].style.animationDelay,/^-\d+ms$/);
const session = detail.children.find(x=>x.className === 'job-session');
assert.match(session.children[1].textContent,/打开本次 Codex 会话/);
detail = jobProgress({...job,progress:{stage:'download',current:3,total:10,unit:'bytes'}});
bar = detail.children.find(x=>x.className === 'job-progress'); assert.equal(bar.tag,'progress'); assert.equal(bar.value,3); assert.equal(bar.max,10);
console.log('Real model activity, stale-status warnings and live session rendering checks passed');
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
v = progressView({...job,status:'completed',items:[{status:'ready',answer_document:{files:{'answer.docx':'offline-hash'}}}]},network,now);
assert.equal(v.localReady,1); assert.equal(v.ready,0); assert.match(v.explanation,/下载 Word 审阅/);
assert.doesNotMatch(v.explanation,/继续自动填写/);
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

const missing = {course_id:'1',assignment_id:'2',status:'needs_user',package:'unit-private-package',form_fill:{forms:[{prefilled_fields:7,remaining_fields:Array.from({length:6},()=>({required:true,title:'Actual original field'}))}]}};
const waiting = {...job,status:'completed_with_issues',kind:'fill',items:[missing]};
const completedLater = {...waiting,id:'unit-later',created_at:'2026-01-02T00:00:00Z',status:'completed',items:[{...missing,status:'form_opened'}]};
assert.equal(completedSuccessor(waiting,[waiting,completedLater]).job.id,'unit-later');
assert.equal(completedSuccessor(waiting,[waiting,{...completedLater,items:[{...missing,status:'needs_form'}]}]),null);
v = progressView(waiting,network,now);
assert.equal(v.active,false); assert.equal(v.stage,'input'); assert.equal(v.index,2);
assert.match(v.title,/处理已结束/); assert.match(v.explanation,/7 栏/); assert.match(v.explanation,/6 项/);
assert.equal(resumeTarget(waiting).label,'填写 6 项信息');
assert.equal(resumeTarget({...waiting,status:'running'}),null);
assert.equal(resumeTarget({...waiting,items:[{...missing,status:'needs_form'}]}),null);
const retryCode = source.slice(source.indexOf('async function jobAction('),source.indexOf('function renderEfforts('));
const opened=[],requests=[];
const {jobAction} = vm.runInNewContext(retryCode+'\n({jobAction});',{state:{jobs:[{...waiting,id:'unit-job'}]},resumeTarget,openSupplement:async(...args)=>opened.push(args),action:async(...args)=>requests.push(args)});
(async()=>{
  await jobAction('unit-job','retry'); assert.equal(opened.length,1); assert.equal(opened[0][1],'1:2'); assert.equal(requests.length,0);
  await jobAction('unit-job','pause'); assert.equal(requests[0][0],'/api/jobs/unit-job/pause');
  console.log('Missing-input retry opens editor without another processing request');
})().catch(error=>{console.error(error);process.exitCode=1;});
