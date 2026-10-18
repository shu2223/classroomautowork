// Test the actual frontend helpers, with no browser or network dependency.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/classroomautowork/web/app.js'), 'utf8');
const code = source.split('// Pure progress presentation;')[1].split('// End pure progress presentation.')[0];
const {progressView,completedSuccessor,documentContentGaps,documentAnswersIncomplete} = vm.runInNewContext('// ' + code + '\n({progressView,completedSuccessor,documentContentGaps,documentAnswersIncomplete});', {activeStates:new Set(['queued','running','stopping']), labels:{failed:'失败',completed:'结束'},keyOf:x=>x.course_id+':'+x.assignment_id});
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
const uiCode = source.slice(source.indexOf('function estimatePanel('),source.indexOf('function jobCard('));
function element(tag,text,className) { return {tag,textContent:text,className,children:[],attributes:{},dataset:{},style:{},classList:{add(value){this.value=value;}},append(...children){this.children.push(...children);},setAttribute(key,value){this.attributes[key]=value;}}; }
const {jobProgress} = vm.runInNewContext(uiCode+'\n({jobProgress});',{progressView:(job,network)=>progressView(job,network,now),connection:network,node:element,Date:{now:()=>now},state:{monitorOnly:false},codexLink:g=>g?.thread_id ? element('a','Session') : null,friendlyError:x=>x,durationLabel:x=>String(x)});
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

// Estimates are supplied by fresh backend observations. The browser must never
// turn an old snapshot into a false countdown or a model completion percentage.
const estimate = {
  observed_at:new Date(now).toISOString(),state:'estimating',
  stage:{remaining_seconds:120,lower_seconds:90,upper_seconds:180,speed:2.5,unit:'media_seconds',source:'mixed',confidence:'medium'},
  overall:{remaining_seconds:300,lower_seconds:240,upper_seconds:420,percent:45.7,scope_complete:true,unknown_tasks:0}
};
const estimatedJob = {...job,estimate,progress:{stage:'transcribe',phase:'infer',current:1,total:2,unit:'segments',media_current_seconds:400,media_total_seconds:1200,activity_at:new Date(now).toISOString()}};
v = progressView(estimatedJob,network,now);
assert.equal(v.estimate.state,'estimating'); assert.equal(v.estimate.overallRemaining,300);
assert.equal(v.estimate.percent,45.7); assert.equal(v.estimate.percentText,'45%');
assert.match(v.estimate.headline,/总共还需 5 分/); assert.match(v.estimate.range,/4 分.*7 分/);
assert.match(v.estimate.finish,/09:15/); assert.match(v.estimate.finish,/日本时间/);
assert.match(v.estimate.speed,/Buzz 2.50× 实时/); assert.match(v.estimate.basis,/实测.*历史/);
assert.match(v.estimate.stageText,/本文件转录预计还需 2 分/);
assert.equal(v.measurement.value,400); assert.equal(v.measurement.max,1200); assert.equal(v.measurement.percent,33);
assert.match(v.measurement.text,/本文件已转录/); assert.match(v.measurement.text,/1\/2 段/);
assert.equal(progressView(estimatedJob,network,now+5000).estimate.overallRemaining,300);
assert.equal(progressView(estimatedJob,network,now+5000).estimate.finish,v.estimate.finish);
let estimatedDetail = jobProgress(estimatedJob);
const panel = estimatedDetail.children.find(x=>x.className === 'job-estimate');
assert.equal(panel.children[1].tag,'progress'); assert.equal(panel.children[1].value,45.7);
assert.match(panel.children[1].attributes['aria-label'],/估计/);
v = progressView({...estimatedJob,estimate:{...estimate,overall:{...estimate.overall,scope_complete:false,unknown_tasks:3}}},network,now);
assert.equal(v.estimate.percent,null); assert.equal(v.estimate.finish,null);
assert.match(v.estimate.headline,/已知工作/); assert.match(v.estimate.note,/3 项工作量待确认/);
v = progressView({...estimatedJob,estimate:{...estimate,overall:{...estimate.overall,unknown_tasks:3}}},network,now);
assert.equal(v.estimate.percent,null); // Contradictory scope metadata cannot fabricate full coverage.
v = progressView(job,network,now);
assert.equal(v.estimate.state,'calibrating'); assert.equal(v.estimate.overallRemaining,null); assert.equal(v.estimate.percent,null);
assert.match(v.estimate.note,/收集本机速度/);
assert.equal(v.estimate.progressTitle,'作业项进度'); assert.equal(v.estimate.percentText,'已处理 0/1 项');
assert.match(v.estimate.note,/旧版服务启动/);
v = progressView(estimatedJob,{lastSuccess:now+13000,error:null},now+13000);
assert.equal(v.estimate.state,'stale'); assert.equal(v.estimate.overallRemaining,null); assert.equal(v.estimate.speed,null); assert.equal(v.estimate.percent,null);
assert.match(v.estimate.note,/旧估计已冻结/);
v = progressView(estimatedJob,{lastSuccess:now,error:'offline'},now);
assert.equal(v.estimate.state,'stale'); assert.equal(v.estimate.stageRemaining,null); assert.equal(v.estimate.finish,null);
v = progressView({...estimatedJob,estimate:{...estimate,state:'stale'}},network,now);
assert.equal(v.estimate.state,'stale'); assert.equal(v.estimate.percent,null);
for (const status of ['stopping','paused']) {
  v = progressView({...estimatedJob,status},network,now);
  assert.equal(v.estimate.state,'paused'); assert.equal(v.estimate.overallRemaining,null); assert.equal(v.estimate.percent,null);
}
v = progressView({...estimatedJob,approval:{requested_at:new Date(now).toISOString()}},network,now);
assert.equal(v.estimate.state,'paused'); assert.equal(v.estimate.finish,null);
for (const status of ['failed','interrupted','completed_with_issues']) {
  v = progressView({...estimatedJob,status},network,now);
  assert.equal(v.estimate.state,'stopped'); assert.equal(v.estimate.percent,null); assert.equal(v.estimate.speed,null);
}
v = progressView({...estimatedJob,status:'completed'},network,now);
assert.equal(v.estimate.state,'complete'); assert.equal(v.estimate.percent,100); assert.equal(v.estimate.overallRemaining,null);
for (const invalid of [null,NaN,Infinity,-1,'120']) {
  v = progressView({...estimatedJob,estimate:{...estimate,stage:{...estimate.stage,remaining_seconds:invalid,speed:invalid},overall:{...estimate.overall,remaining_seconds:invalid,percent:invalid}}},network,now);
  assert.equal(v.estimate.stageRemaining,null); assert.equal(v.estimate.overallRemaining,null); assert.equal(v.estimate.percent,null); assert.equal(v.estimate.speed,null);
  assert.doesNotMatch(JSON.stringify(v.estimate),/NaN|Infinity|undefined/);
}
v = progressView({...estimatedJob,estimate:{...estimate,stage:{...estimate.stage,remaining_seconds:0},overall:{...estimate.overall,remaining_seconds:0,percent:100}}},network,now);
assert.equal(v.estimate.percent,null); assert.equal(v.estimate.overallRemaining,null);
assert.match(v.estimate.headline,/重新校准/); assert.doesNotMatch(v.estimate.headline,/0 秒/);
v = progressView({...estimatedJob,estimate:{...estimate,observed_at:'invalid'}},network,now);
assert.equal(v.estimate.state,'calibrating'); assert.equal(v.estimate.overallRemaining,null);
v = progressView({...estimatedJob,estimate:{...estimate,overall:{...estimate.overall,lower_seconds:400}}},network,now);
assert.equal(v.estimate.range,null);
v = progressView({...estimatedJob,progress:{...estimatedJob.progress,phase:'prepare',phase_current_seconds:20,phase_total_seconds:60}},network,now);
assert.match(v.title,/准备 Buzz/); assert.equal(v.measurement.value,20); assert.equal(v.measurement.max,60);
assert.match(v.measurement.text,/本段音频已准备/); assert.match(v.estimate.speed,/音频准备/);
assert.match(v.estimate.stageText,/本文件转录预计还需 2 分/); // The file ETA includes future preparation and inference phases.
v = progressView({...estimatedJob,progress:{...estimatedJob.progress,phase:'prepare'},estimate:{...estimate,stage:{...estimate.stage,phase:'prepare',phase_remaining_seconds:16}}},network,now);
assert.match(v.estimate.stageText,/本文件转录预计还需 2 分/); assert.match(v.estimate.phaseText,/本段音频准备预计还需 16 秒/);
v = progressView({...estimatedJob,progress:{...estimatedJob.progress,phase:'prepare'},estimate:{...estimate,stage:{...estimate.stage,phase:'prepare',remaining_seconds:null,phase_remaining_seconds:16},overall:{remaining_seconds:null,scope_complete:false,unknown_tasks:4},reason:'remaining_phases_uncalibrated'}},network,now);
assert.equal(v.estimate.stageRemaining,16); assert.match(v.estimate.stageText,/本段音频准备预计还需 16 秒/);
assert.equal(v.estimate.stageRange,null); assert.equal(v.estimate.phaseText,null); assert.match(v.estimate.speed,/音频准备/);
assert.match(v.estimate.note,/其他处理阶段仍在校准/); assert.doesNotMatch(v.estimate.note,/remaining_phases/);
for (const reason of ['measured_rate','awaiting_measurements','progress_not_updated','waiting_for_resume','complete','stopped','unknown_internal_reason']) {
  v = progressView({...estimatedJob,estimate:{...estimate,reason}},network,now);
  assert.doesNotMatch(v.estimate.note || '',new RegExp(reason));
}
v = progressView({...estimatedJob,estimate:{...estimate,reason:'当前文件耗时超过历史范围，正在校准。'}},network,now);
assert.match(v.estimate.note,/耗时超过历史范围/);
v = progressView({...estimatedJob,progress:{stage:'course_prepared'}},network,now);
assert.match(v.title,/本课程资料已准备/);
v = progressView({...estimatedJob,progress:{stage:'attachment_failed'}},network,now);
assert.match(v.title,/附件未能处理完成/);
v = progressView({...estimatedJob,progress:{...estimatedJob.progress,phase:'prepare'}},network,now);
assert.equal(v.measurement,null); // Segment counts do not measure audio preparation.
v = progressView({...aiJob,estimate,progress:{stage:'ai',current:7,total:10}},network,now);
assert.equal(v.measurement,null); // AI activity counts never become measured progress.
v = progressView({...estimatedJob,estimate:{...estimate,stage:{...estimate.stage,unit:'bytes',speed:2097152}}},network,now);
assert.equal(v.estimate.speed,'下载 2.00 MB/s');
v = progressView({...estimatedJob,estimate:{...estimate,stage:{...estimate.stage,unit:'pages',speed:0.5}}},network,now);
assert.equal(v.estimate.speed,'文档处理 0.50 页/s');
console.log('Live speed, estimate bounds, file scope, frozen/offline states and invalid estimate checks passed');

// A worker started before ETA support can still expose a current-file estimate
// from timestamped, non-cached segment events. Unopened files stay unestimated.
const filename = 'unit-lecture.mp4', prefix = filename + ' · ';
const legacyEvents = [
  ['00:00:00','正在处理录像第 1/5 段（0–300 秒）'],
  ['00:00:01','Buzz 大模型 large-v3-turbo 正在本机转录第 1/5 段，完成后缓存'],
  ['00:02:00','已完成并缓存 1/5 段带时间戳转录'],
  ['00:02:00','正在处理录像第 2/5 段（300–600 秒）'],
  ['00:02:01','Buzz 大模型 large-v3-turbo 正在本机转录第 2/5 段，完成后缓存'],
  ['00:04:00','已完成并缓存 2/5 段带时间戳转录'],
  ['00:09:00','正在处理录像第 3/5 段（600–900 秒）'],
  ['00:09:01','Buzz 大模型 large-v3-turbo 正在本机转录第 3/5 段，完成后缓存'],
].map(([time,message])=>({at:'2026-01-01T'+time+'Z',message:prefix+message}));
const legacyJob = {...job,runtime:{worker_alive:true},items:[{status:'document_ready'},{status:'preparing'},{status:'waiting'},{status:'waiting'}],events:legacyEvents,
  progress:{stage:'transcribe',unit:'segments',current:2,total:5,filename,started_at:'2026-01-01T00:00:00Z',activity_at:'2026-01-01T00:09:01Z'}};
v = progressView(legacyJob,network,now);
assert.equal(v.estimate.state,'estimating'); assert.equal(v.estimate.stageRemaining,300);
assert.equal(v.estimate.overallRemaining,null); assert.equal(v.estimate.finish,null); assert.equal(v.estimate.percent,null);
assert.equal(v.estimate.knownRemaining,300); assert.match(v.estimate.headline,/已知工作预计还需 5 分.*其余未计入/);
assert.equal(v.estimate.percentText,'已处理 1/4 项'); assert.equal(v.estimate.progressTitle,'作业项进度');
assert.match(v.estimate.stageText,/本文件转录预计还需 5 分/); assert.match(v.estimate.stageRange,/通常/);
assert.match(v.estimate.speed,/Buzz 2.50× 实时/); assert.match(v.estimate.note,/最近 2 段/);
assert.match(v.estimate.note,/其余附件、课程和作答耗时尚未计入/); assert.match(v.estimate.note,/末段/);
let legacyPanel = jobProgress(legacyJob).children.find(x=>x.className === 'job-estimate');
assert.equal(legacyPanel.children[0].children[0].textContent,'作业项进度');
assert.equal(legacyPanel.children[0].children[1].textContent,'已处理 1/4 项');
v = progressView({...legacyJob,progress:{...legacyJob.progress,media_total_seconds:1230,media_current_seconds:600}},network,now);
assert.equal(v.estimate.stageRemaining,192); assert.doesNotMatch(v.estimate.note,/末段/);
v = progressView({...legacyJob,progress:{...legacyJob.progress,media_total_seconds:1230,media_current_seconds:750}},network,now);
assert.equal(v.estimate.stageRemaining,192); // Intra-segment media progress is not subtracted twice.
const lastSegment = {...legacyJob,progress:{...legacyJob.progress,current:4},events:legacyEvents.map(x=>({...x,message:x.message.replace('第 3/5 段（600–900 秒）','第 5/5 段（1200–1500 秒）').replace('转录第 3/5 段','转录第 5/5 段')}))};
v = progressView(lastSegment,{lastSuccess:now+70000,error:null},now+70000);
assert.equal(v.estimate.state,'calibrating'); assert.equal(v.estimate.stageRemaining,null);
assert.match(v.estimate.note,/最后一段已超过本次预测/); // Never hold a false one-second countdown.
// Cached starts/completions, another file, a prior run and tiny timings must
// never become the sample population, even with a large completion count.
for (const events of [
  legacyEvents.filter(x=>!x.message.includes('正在本机转录第 1/')),
  legacyEvents.map(x=>({...x,message:x.message.replace(filename,'other.mp4')})),
  legacyEvents.map(x=>({...x,at:x.at.replace('2026-01-01','2025-12-31')})),
  legacyEvents.map(x=>x.message.includes('已完成并缓存 1/') ? {...x,at:'2026-01-01T00:00:01.100Z'} : x),
]) {
  v = progressView({...legacyJob,events},network,now);
  assert.equal(v.estimate.state,'calibrating'); assert.equal(v.estimate.stageRemaining,null); assert.equal(v.estimate.speed,null);
}
v = progressView(legacyJob,{lastSuccess:now+200000,error:null},now+200000);
assert.equal(v.estimate.state,'calibrating'); assert.equal(v.estimate.stageRemaining,null);
assert.match(v.estimate.note,/超过已测耗时范围/); // No countdown through zero.
v = progressView(legacyJob,{lastSuccess:now,error:'offline'},now);
assert.equal(v.estimate.state,'stale'); assert.equal(v.estimate.stageRemaining,null);
v = progressView({...legacyJob,status:'stopping'},network,now);
assert.equal(v.estimate.state,'paused'); assert.equal(v.estimate.stageRemaining,null);
v = progressView({...legacyJob,estimate},network,now);
assert.equal(v.estimate.stageRemaining,120); // Native backend estimates always win.
v = progressView({...estimatedJob,estimate:{...estimate,overall:{scope_complete:false,unknown_tasks:3,remaining_seconds:null,known_remaining_seconds:600,known_lower_seconds:480,known_upper_seconds:900}}},network,now);
assert.equal(v.estimate.overallRemaining,null); assert.equal(v.estimate.knownRemaining,600);
assert.match(v.estimate.headline,/已知工作预计还需 10 分.*其余未计入/);
assert.match(v.estimate.range,/8 分.*15 分/); assert.equal(v.estimate.finish,null); assert.equal(v.estimate.percent,null);
console.log('Legacy running-worker segment timing, cache exclusion, assignment count and partial workload estimate checks passed');

// A silent network stall must also freeze the visible ETA between polls.
const clockCode = source.slice(source.indexOf('function updateProgressClock('),source.indexOf('setInterval(updateProgressClock'));
let replacedEstimate = null;
const visibleEstimate = {dataset:{estimateState:'estimating'},replaceWith(value){replacedEstimate=value;}};
const warning = {}, clockBox = {dataset:{jobId:'clock-job'},querySelectorAll:()=>[],querySelector:selector=>selector === '.job-estimate' ? visibleEstimate : warning};
const {updateProgressClock} = vm.runInNewContext(clockCode+'\n({updateProgressClock});',{
  document:{querySelectorAll:()=>[clockBox]},state:{jobs:[{...estimatedJob,id:'clock-job'}]},activeStates:new Set(['running']),connection:network,
  progressView:(job,network)=>progressView(job,network,now+13000),estimatePanel:value=>value
});
updateProgressClock();
assert.equal(clockBox.dataset.progressState,'stale'); assert.equal(replacedEstimate.state,'stale');
assert.equal(replacedEstimate.overallRemaining,null); assert.equal(replacedEstimate.finish,null);
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
const cardCode = source.slice(source.indexOf('function jobCard('),source.indexOf('function renderJobs('));
const {jobCard} = vm.runInNewContext(cardCode+'\n({jobCard});',{
  state:{jobs:[],monitorOnly:false},activeStates:new Set(['queued','running','stopping']),labels:{completed_with_issues:'处理已结束'},
  node:element,button:element,pill:status=>element('span',status),safeLink:()=>null,codexLink:()=>null,
  keyOf:x=>x.course_id+':'+x.assignment_id,inputAction:()=>({label:'调整答案或说明（可选）'}),
  completedSuccessor,jobProgress:()=>element('div','progress'),friendlyError:x=>x
});
assert.doesNotThrow(()=>jobCard({...waiting,id:'unit-job',events:[]}));
const completedLater = {...waiting,id:'unit-later',created_at:'2026-01-02T00:00:00Z',status:'completed',items:[{...missing,status:'form_opened'}]};
assert.equal(completedSuccessor(waiting,[waiting,completedLater]).job.id,'unit-later');
assert.equal(completedSuccessor(waiting,[waiting,{...completedLater,items:[{...missing,status:'needs_form'}]}]),null);
v = progressView(waiting,network,now);
assert.equal(v.active,false); assert.equal(v.index,3); assert.equal(v.ready,1);
assert.match(v.title,/答案已交付/); assert.match(v.explanation,/7 栏/); assert.match(v.explanation,/6 项/);
assert.doesNotMatch(v.explanation,/等待补充|重试不会自动补齐/);
v = progressView({...waiting,items:[{...missing,form_fill:undefined}]},network,now);
assert.equal(v.ready,0); assert.doesNotMatch(v.title,/答案已交付/);
const retryCode = source.slice(source.indexOf('async function jobAction('),source.indexOf('function renderEfforts('));
const opened=[],requests=[];
const {jobAction} = vm.runInNewContext(retryCode+'\n({jobAction});',{state:{jobs:[{...waiting,id:'unit-job'}]},openSupplement:async(...args)=>opened.push(args),action:async(...args)=>requests.push(args)});
(async()=>{
  await jobAction('unit-job','retry'); assert.equal(opened.length,0); assert.equal(requests[0][0],'/api/jobs/unit-job/retry');
  await jobAction('unit-job','pause'); assert.equal(requests[1][0],'/api/jobs/unit-job/pause');
  console.log('Retry resumes automatic processing without forcing the optional editor');
})().catch(error=>{console.error(error);process.exitCode=1;});

const selectionCode = source.slice(source.indexOf('function hasAnswer('),source.indexOf('function filtered('));
const {hasAnswer,delivered,selectTargets} = vm.runInNewContext(selectionCode+'\n({hasAnswer,delivered,selectTargets});',{documentAnswersIncomplete,keyOf:x=>x.course_id+':'+x.assignment_id});
const visible = [{course_id:'1',assignment_id:'1'},{course_id:'1',assignment_id:'2'},{course_id:'1',assignment_id:'3'}];
const ready = {status:'ready',answer_document:{files:{'answer.docx':'verified-hash'}}};
const latest = new Map([['1:2',{item:ready}]]);
assert.equal(selectTargets(visible,new Set(),latest).length,2);
assert.equal(selectTargets(visible,new Set(['1:3','hidden:4']),latest).length,1);
assert.equal(selectTargets(visible,new Set(['hidden:4']),latest).length,0);
assert.equal(selectTargets(visible,new Set(['1:2']),latest).length,0);
assert.equal(delivered({status:'ready'}),false); // A status with no result is not delivery.
assert.equal(delivered({...ready,status:'needs_form'}),false); // Failed fill remains actionable.
assert.equal(hasAnswer({...ready,status:'needs_form'}),true); // Its local answer is still visible.
assert.equal(delivered({status:'document_ready',document_fill:{documents:[{url:'verified-document'}]}}),true);
const incompleteDoc = {status:'document_ready',answer_document:ready.answer_document,document_fill:{documents:[{url:'verified-document',verified_fields:10,remaining_required_fields:[{id:'q2',label:'最低何人で行うもの？その理由は？'},{id:'q4',label:'その理由は？'}]}]}};
assert.equal(delivered(incompleteDoc),false);
assert.equal(delivered({...incompleteDoc,status:'document_needs_user'}),false);
assert.equal(hasAnswer(incompleteDoc),true); // Preserve usable partial answers and their document link.
assert.equal(selectTargets(visible,new Set(['1:2']),new Map([['1:2',{item:incompleteDoc}]])).length,1);
assert.equal(delivered({status:'document_needs_user',document_fill:{documents:[{url:'verified-document',remaining_required_fields:[]}],identity_warnings:['Missing optional identity']}}),true);
assert.equal(delivered({status:'document_ready',document_fill:{status:'partial',documents:[{url:'verified-document'}]}}),false);
v = progressView({...job,status:'completed',items:[incompleteDoc]},network,now);
assert.equal(v.ready,0); assert.equal(v.localReady,0); assert.notEqual(v.index,3);
assert.match(v.title,/仍有题目未完成/); assert.match(v.explanation,/补全/);
assert.equal(documentContentGaps({remaining_required_fields:[{id:'q2'}],documents:[{remaining_required_fields:[{id:'q2'}]}]}).length,1);
console.log('One-click scope, existing-answer skip and missing-result regression checks passed');

// A generated answer must remain discoverable until Classroom reports submission.
const filterCode = source.slice(source.indexOf('function filtered('), source.indexOf('function targets('));
const controls = {searchInput:{value:''},courseFilter:{value:''},dateFilter:{value:''},includeNoDue:{checked:true}};
const filterState = {pending:{assignments:visible},hideOverdue:false};
const {filtered} = vm.runInNewContext(filterCode+'\n({filtered});',{
  state:filterState,$:id=>controls[id],overdue:()=>false,localDay:x=>x.slice(0,10),latestItems:()=>latest,delivered,keyOf:x=>x.course_id+':'+x.assignment_id
});
assert.equal(filtered().length,3);
assert.equal(selectTargets(filtered(),new Set(),latest).length,2);
controls.searchInput.value='nutrition';
filterState.pending.assignments=[{...visible[1],title:'Nutrition assignment',due_local:'2026-10-15T17:00:00+09:00'}];
assert.equal(filtered().length,1);
assert.equal(selectTargets(filtered(),new Set(),latest).length,0);
controls.dateFilter.value='2026-10-12';
assert.equal(filtered().length,0); // Explicit date filters still apply.
const refreshCode = source.slice(source.indexOf('function pendingRefreshDue('),source.indexOf('let syncChecking'));
const {pendingRefreshDue} = vm.runInNewContext(refreshCode+'\n({pendingRefreshDue});',{ageOf:(at,n)=>Number.isFinite(Date.parse(at)) ? n-Date.parse(at) : null});
assert.equal(pendingRefreshDue(null,now),true);
assert.equal(pendingRefreshDue({refreshed_at:'bad timestamp'},now),true);
assert.equal(pendingRefreshDue({refreshed_at:new Date(now-10*60*1000).toISOString()},now),false);
assert.equal(pendingRefreshDue({refreshed_at:new Date(now-16*60*1000).toISOString()},now),true);
console.log('Unsubmitted answers stay visible, filter scope and refresh expiry checks passed');
