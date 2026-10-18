// Offline power UI checks. No browser, subprocess, or operating system power action.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/classroomautowork/web/app.js'), 'utf8');
const code = source.slice(source.indexOf('// Pure shutdown presentation:'), source.indexOf('function node('));
const now = Date.parse('2026-01-01T00:00:00Z');
const state = {shutdown:null, shutdownPending:false, shutdownError:null, shutdownReceivedAt:0, monitorOnly:false};
const elements = Object.fromEntries(['shutdownControl','shutdownEnabled','shutdownNote','shutdownCancel'].map(id => [id, {classList:{toggle(){}}, hidden:false, disabled:false, checked:false, textContent:''}]));
const calls = [], timers = [];
let respond = async () => { throw new Error('Unexpected request'); };
const helpers = vm.runInNewContext('let shutdownPolling = null, shutdownPollInFlight = false, shutdownRequestVersion = 0;\n' + code + '\n({shutdownView,receiveShutdown,renderShutdown,pollShutdown,changeShutdown});', {
  state, $:id=>elements[id], Date:{now:()=>now,parse:Date.parse},
  api:async (...args)=>{calls.push(args); return respond(...args);},
  setInterval:(fn,delay)=>{timers.push({fn,delay}); return timers.length;}, toast:()=>{}
});
const {shutdownView,receiveShutdown,renderShutdown,pollShutdown,changeShutdown} = helpers;
const off = {supported:true,enabled:false,state:'off',message:'关闭'};
const armed = {supported:true,enabled:true,state:'armed',job_id:'unit-job',message:'当前任务成功后关机'};
const countdown = {...armed,state:'countdown',remaining_seconds:60,deadline_at:'2026-01-01T00:01:00Z'};
const cancelled = {...off,state:'cancelled',message:'已取消自动关机'};

assert.equal(shutdownView(null,now).enabled,false);
assert.equal(shutdownView({...armed,supported:false},now).enabled,false);
assert.equal(shutdownView(countdown,now+10000,now).remaining,50);
assert.match(shutdownView(countdown,now+10000,now).message,/50 秒后/);
assert.equal(shutdownView({...countdown,deadline_at:null},now+10000,now).remaining,50);
assert.match(shutdownView(countdown,now+61000,now).message,/等待系统响应/);
assert.equal(shutdownView(countdown,now+61000,now).canCancel,true);
assert.equal(shutdownView({...armed,state:'shutting_down'},now).canCancel,false);
assert.equal(shutdownView({...off,state:'failed'},now).enabled,false);
receiveShutdown(armed); renderShutdown();
assert.equal(elements.shutdownEnabled.checked,true);
assert.equal(elements.shutdownEnabled.disabled,false); // It stays usable during a running task.
assert.equal(elements.shutdownCancel.hidden,false);
assert.equal(calls.length,0); // Loading or rendering a saved state never enables shutdown.
state.monitorOnly = true; renderShutdown();
assert.equal(elements.shutdownControl.hidden,true);
assert.equal(elements.shutdownEnabled.disabled,true);
state.monitorOnly = false;
receiveShutdown({...off,supported:false});
assert.equal(elements.shutdownEnabled.disabled,true);
assert.equal(elements.shutdownEnabled.checked,false);

(async () => {
  receiveShutdown(off);
  let finishEnable;
  respond = () => new Promise(resolve=>{finishEnable=resolve;});
  const enable = changeShutdown(true);
  assert.equal(elements.shutdownEnabled.disabled,true);
  assert.equal(elements.shutdownEnabled.checked,false); // Only server-confirmed state is displayed.
  assert.equal(calls.at(-1)[0],'/api/shutdown');
  assert.equal(calls.at(-1)[1],'POST');
  assert.equal(calls.at(-1)[2].enabled,true);
  finishEnable(armed); await enable;
  assert.equal(elements.shutdownEnabled.checked,true);
  assert.equal(elements.shutdownEnabled.disabled,false);
  assert.equal(timers.length,1);
  assert.equal(timers[0].delay,2000);

  receiveShutdown(countdown);
  respond = async (url,method)=>{if (method === 'POST') throw new Error('取消失败'); return countdown;};
  await changeShutdown(false,true);
  assert.equal(elements.shutdownEnabled.checked,true);
  assert.equal(elements.shutdownCancel.hidden,false);
  assert.match(elements.shutdownNote.textContent,/取消失败.*可能仍在后台生效/);
  assert.equal(calls.at(-2)[0],'/api/shutdown/cancel');
  assert.equal(calls.at(-1)[1],undefined); // A failed change re-reads the actual backend state.

  // A status read started before cancellation cannot overwrite its newer response.
  let finishOldPoll;
  respond = () => new Promise(resolve=>{finishOldPoll=resolve;});
  const oldPoll = pollShutdown();
  respond = async ()=>cancelled;
  await changeShutdown(false,true);
  finishOldPoll(countdown); await oldPoll;
  assert.equal(elements.shutdownEnabled.checked,false);
  assert.equal(elements.shutdownCancel.hidden,true);
  assert.equal(state.shutdown.state,'cancelled');

  state.monitorOnly = true;
  const before = calls.length; await changeShutdown(true); await pollShutdown();
  assert.equal(calls.length,before);
  assert.equal(code.includes('localStorage'),false); // Enabling applies only through an explicit request.
  console.log('Automatic shutdown UI, cancellation and stale-response checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
