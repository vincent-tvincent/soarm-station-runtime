import test from 'node:test';
import assert from 'node:assert/strict';
import {ManualPanel} from '../web/manual-panel.js';
import {StationClient} from '../web/transport.js';

globalThis.document = {hidden:false};
globalThis.Option = class { constructor(text,value){this.text=text;this.value=value;} };
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const target=(q=0,g=50)=>({positions:{joint:q},gripper:g,gripper_unit:'percent'});
function emitTransportClose(panel,reason='stopping',arm=panel.arm){
  let emitted;
  const transport=new StationClient({onOperation:event=>{emitted=event;panel.updateOperation(event);}});
  transport._active={armId:arm,type:'DIRECT',mode:'STREAM',transport:'websocket',state:'active',
    grant:null,timers:new Set(),pending:null,finished:{resolve(){}}};
  transport._stopLocalOperation(reason,'Test close event');
  return emitted;
}
function fixture(){
  const panel=Object.create(ManualPanel.prototype),calls=[];
  const states={a:{runtime_boot_id:'boot',controller_session:'session',feedback_valid:true,gripper_percent:50,permission:false},b:{runtime_boot_id:'bootB',controller_session:'sessionB',feedback_valid:true,gripper_percent:50,permission:false}};
  const elements=new Map();
  const el=id=>{if(!elements.has(id))elements.set(id,{value:id==='leader-host'?'host':id==='leader-device'?'leader':'',textContent:'Normal',disabled:false,dataset:{},replaceChildren(){},classList:{toggle(){}}});return elements.get(id);};
  const client={operatorSessionId:'operator',currentOperation:null,
    async enable(arm){calls.push(['enable',arm]);states[arm].permission=true;return{accepted:true};},
    async startManual(arm){calls.push(['start',arm]);this.currentOperation={armId:arm,type:'DIRECT',mode:'STREAM',operationId:'test-op',ready:true};return this.currentOperation;},
    async hold(arm){calls.push(['hold',arm]);this.currentOperation=null;states[arm].permission=false;emitTransportClose(panel,'stopping',arm);return{success:true};},
    setManualTarget(value){calls.push(['target',structuredClone(value)]);},
    async request(path,body){calls.push(['request',path,structuredClone(body)]);return{target:target(1)};},
  };
  Object.assign(panel,{client,arm:'a',mode:'ik',level:2,keys:new Set(),epoch:0,intentGeneration:0,pendingActions:0,expectedTermination:0,lastSolveAt:null,
    busy:false,connected:true,visible:true,target:target(),reference:target(),dirty:false,ownsStream:false,leaderRun:null,positionOnly:false,
    snapshot:{arms:states,joints:{a:{name:['joint'],position:[0]},b:{name:['joint'],position:[0]}}},resources:{arms:[]},config:{},
    element:{querySelectorAll:()=>[]},el,fk:{resetArm(){}},onError:e=>calls.push(['error',e.message]),note:m=>calls.push(['note',m]),
    updateControls(){},renderSliders(){},renderDevices(){},async loadDescription(){},refreshPose(){},
  });
  return{panel,client,calls,states};
}

test('late leader status reports the real ended-run reason without a mismatch toast',async()=>{
  const {panel,client,calls}=fixture();
  panel.leaderRun={host:'host',run_id:'old'};
  client.request=async()=>({accepted:false,code:'RUN_ENDED',reason:'Actual controller reason'});
  await panel.heartbeat();
  assert.equal(panel.leaderRun,null);
  assert(calls.some(row=>row[0]==='note'&&row[1]==='Actual controller reason'));
  assert(!calls.some(row=>row[0]==='error'));
});

test('a failed browser status poll does not stop a native leader stream',async()=>{
  const {panel,client,calls}=fixture();const run={host:'host',run_id:'live'};panel.leaderRun=run;
  client.request=async()=>{throw Error('temporary HTTP failure');};
  await panel.heartbeat();
  assert.equal(panel.leaderRun,run);
  assert(!calls.some(row=>row[0]==='error'||row[0]==='hold'));
});

test('late status response for old leader run cannot clear its replacement',async()=>{
  const {panel,client}=fixture(),gate=deferred();
  panel.leaderRun={host:'host',run_id:'old'};client.request=()=>gate.promise;
  const pending=panel.heartbeat();const replacement={host:'host',run_id:'new'};panel.leaderRun=replacement;
  gate.resolve({accepted:false,reason:'Old run ended'});await pending;
  assert.equal(panel.leaderRun,replacement);
});

test('default arm capabilities load when the initial connection becomes ready',async()=>{
  const {panel,calls}=fixture();
  panel.arm='';
  panel.loadDescription=async()=>calls.push(['describe',panel.arm]);
  const update=connected=>panel.update({snapshot:panel.snapshot,resources:panel.resources,arm:'a',connected});
  update(false);
  assert.equal(calls.filter(x=>x[0]==='describe').length,0);
  update(true);
  update(true);
  assert.deepEqual(calls.filter(x=>x[0]==='describe'),[['describe','a']]);
});

test('monitoring degradation does not discard an owned open control socket',()=>{
  const {panel,client}=fixture();
  client.connected=true;panel.ownsStream=true;panel.keys.add('KeyK');
  client.currentOperation={armId:'a',type:'DIRECT',mode:'STREAM',transport:'websocket',socketOpen:true,ready:true};
  const generation=panel.intentGeneration;
  panel.update({snapshot:panel.snapshot,resources:panel.resources,arm:'a',connected:false});
  assert.equal(panel.connected,true);assert.equal(panel.ownsStream,true);
  assert.equal(panel.intentGeneration,generation);assert(panel.keys.has('KeyK'));
});

for(const reason of ['client-disconnected','socket-closed'])test(`actual ${reason} still retires manual intent`,()=>{
  const {panel,client}=fixture();
  client.connected=reason!=='client-disconnected';panel.ownsStream=true;panel.keys.add('KeyK');
  client.currentOperation={armId:'a',type:'DIRECT',mode:'STREAM',transport:'websocket',socketOpen:reason!=='socket-closed',ready:true};
  panel.update({snapshot:panel.snapshot,resources:panel.resources,arm:'a',connected:false});
  assert.equal(panel.connected,false);assert.equal(panel.ownsStream,false);assert.equal(panel.keys.size,0);
});

 test('Stop while permission is pending prevents late Start and revokes late permission',async()=>{
  const{panel,client,calls,states}=fixture(),gate=deferred();
  client.enable=async arm=>{calls.push(['enable',arm]);await gate.promise;states[arm].permission=true;};
  const start=panel.start();assert(panel.busy);
  await panel.stop();assert(panel.busy,'pending start retains busy lock after Stop completes');
  await panel.start();assert.equal(calls.filter(x=>x[0]==='enable').length,1);
  gate.resolve();await start;
  assert(!calls.some(x=>x[0]==='start'||x[0]==='target'));
  assert.equal(states.a.permission,false);assert.equal(panel.busy,false);
});

test('key release cancels calculations but does not cancel an explicit Start intent',async()=>{
  const{panel,client,calls,states}=fixture(),gate=deferred();
  client.enable=async()=>{await gate.promise;states.a.permission=true;};
  const start=panel.start();const intent=panel.intentGeneration;
  panel.clearKeys();assert.equal(panel.intentGeneration,intent);
  gate.resolve();await start;
  assert.equal(calls.filter(x=>x[0]==='start').length,1);
  assert.equal(calls.filter(x=>x[0]==='target').length,1);
});

test('late websocket Start after Stop is held and never receives its staged target',async()=>{
  const{panel,client,calls,states}=fixture(),gate=deferred();states.a.permission=true;
  client.startManual=async arm=>{calls.push(['start',arm]);await gate.promise;client.currentOperation={armId:arm,type:'DIRECT',mode:'STREAM',ready:true};};
  const start=panel.start();await panel.stop();gate.resolve();await start;
  assert(!calls.some(x=>x[0]==='target'));assert.equal(client.currentOperation,null);assert.equal(panel.ownsStream,false);
});

for(const cancel of ['stop','arm','mode','disconnect'])test(`delayed Home cannot send or enable after ${cancel}`,async()=>{
  const{panel,client,calls}=fixture(),gate=deferred();client.request=async()=>gate.promise;
  const home=panel.preset('home');assert(panel.busy);
  if(cancel==='stop')await panel.stop();
  if(cancel==='arm')panel.update({snapshot:panel.snapshot,resources:panel.resources,arm:'b',connected:true});
  if(cancel==='mode')await panel.changeMode('joint');
  if(cancel==='disconnect')panel.update({snapshot:panel.snapshot,resources:panel.resources,arm:'a',connected:false});
  gate.resolve({target:target(1)});await home;
  assert(!calls.some(x=>['enable','start','target'].includes(x[0])));
  assert(!panel.busy);
});

test('a late leader start is explicitly stopped after cancellation',async()=>{
  const{panel,client,calls,states}=fixture(),gate=deferred();panel.mode='leader';states.a.permission=true;
  client.request=async(path,body)=>{calls.push(['request',path,body]);return path.endsWith('/start')?gate.promise:{accepted:true,stopped:true,hold_confirmed:true};};
  const start=panel.start();await Promise.resolve();await panel.stop();gate.resolve({accepted:true,run_id:'late-run'});await start;
  assert.equal(panel.leaderRun,null);
  assert(calls.some(x=>x[0]==='request'&&x[1].endsWith('/stop')&&x[2].run_id==='late-run'));
});

test('Follow leader retires only its own Home stream and requests smooth takeover without pickup',async()=>{
  const {panel,client,calls,states}=fixture();panel.mode='leader';states.a.permission=true;
  panel.leaderRun={host:'host',run_id:'first'};
  client.request=async(path,body)=>{
    calls.push(['request',path,body]);
    if(path.endsWith('/stop'))return {stopped:true,hold_confirmed:true}; // intentionally stale permission cache
    if(path.endsWith('/start'))return {accepted:true,run_id:'second'};
    return {target:target(.4,50)};
  };
  await panel.preset('home');
  assert(panel.isOwnStream());assert.equal(panel.leaderRun,null);
  assert.deepEqual(calls.filter(c=>['enable','start'].includes(c[0])),[['enable','a'],['start','a']]);
  ManualPanel.prototype.updateControls.call(panel);
  assert.equal(panel.el('start-manual').disabled,false);
  const offset=calls.length;
  await panel.start();
  assert.deepEqual(calls.slice(offset).filter(c=>['hold','enable','request'].includes(c[0])).map(c=>c[0]),['hold','enable','request']);
  assert.equal(panel.leaderRun.run_id,'second');assert(!panel.ownsStream);
  assert(calls.some(c=>c[0]==='request'&&c[1].endsWith('/start')&&c[2].pickup===false));
});

test('Leader unlock refuses a foreign operation without hold or permission changes',async()=>{
  const {panel,client,calls,states}=fixture();panel.mode='leader';
  states.a.operation_id='foreign';client.currentOperation={armId:'a',type:'DIRECT',mode:'STREAM'};
  await assert.rejects(panel.start(),/Another operation/);
  assert(!calls.some(c=>['hold','enable','request'].includes(c[0])));
});

test('Stop during the Home to Leader handoff cannot start following afterward',async()=>{
  const {panel,client,calls}=fixture(),gate=deferred();panel.mode='leader';panel.ownsStream=true;
  client.currentOperation={armId:'a',type:'DIRECT',mode:'STREAM',operationId:'test-op'};
  const hold=client.hold.bind(client);let first=true;
  client.hold=async arm=>{if(first){first=false;await gate.promise;}return hold(arm);};
  const pending=panel.start();await panel.stop();gate.resolve();await pending;
  assert.equal(panel.leaderRun,null);
  assert(!calls.some(c=>c[0]==='enable'||c[0]==='request'&&c[1].endsWith('/start')));
});

test('continuous IK advances its accepted reference with only one pending solve',async()=>{
  const{panel,client,calls,states}=fixture(),first=deferred();states.a.permission=true;panel.ownsStream=true;panel.keys.add('KeyK');
  client.currentOperation={armId:'a',type:'DIRECT',mode:'STREAM',ready:true};
  const bodies=[];client.request=async(path,body)=>{bodies.push(structuredClone(body));return bodies.length===1?first.promise:{status:'OK',target:target(2,52)};};
  const solving=panel.tick();await panel.tick();assert.equal(bodies.length,1);
  first.resolve({status:'OK',target:target(1,51)});await solving;
  await panel.tick();assert.deepEqual(bodies[1].reference_positions,{joint:1});assert.equal(bodies[1].reference_gripper,51);
  assert.deepEqual(panel.reference,target(2,52));assert.equal(calls.filter(x=>x[0]==='target').length,2);
  assert.equal(bodies[1].positions.joint,0,'measured pose is distinct from admitted reference');
  assert(bodies.every(b=>b.dt_s>=0&&b.dt_s<=.1));
});

test('keyup discards a pending solve and Home reseeds future jogs',async()=>{
  const{panel,client,calls,states}=fixture(),gate=deferred();states.a.permission=true;panel.ownsStream=true;panel.keys.add('KeyK');
  client.currentOperation={armId:'a',type:'DIRECT',mode:'STREAM',ready:true};client.request=async()=>gate.promise;
  const solving=panel.tick();panel.clearKeys();gate.resolve({status:'OK',target:target(1)});await solving;
  assert(!calls.some(x=>x[0]==='target'));
  client.request=async()=>({target:target(.4,60)});await panel.preset('home');
  assert.deepEqual(panel.reference,target(.4,60));assert.equal(panel.keys.size,0);
});

test('pending roll cannot reopen a stream after Stop',async()=>{
  const{panel,client,calls}=fixture(),gate=deferred();client.request=async()=>gate.promise;
  const roll=panel.roll('reference');await panel.stop();gate.resolve({target:target(.2)});await roll;
  assert(!calls.some(x=>['enable','start','target'].includes(x[0])));
});

test('unexpected operation fault invalidates pending Home even during internal teardown',async()=>{
  const{panel,client,calls}=fixture(),gate=deferred();client.request=async()=>gate.promise;
  const home=panel.preset('home');panel.expectedTermination=1;panel.updateOperation({event:'error',state:'error'});panel.expectedTermination=0;
  gate.resolve({target:target(.8)});await home;
  assert(!calls.some(x=>['enable','start','target'].includes(x[0])));
});

test('real transport expected-hold event preserves a gamepad mode-change intent',async()=>{
  const{panel,client,calls,states}=fixture();panel.mode='gamepad';panel.ownsStream=true;states.a.permission=true;
  panel.gamepad={suspend(){},operationEnded(){},setActive(){}};
  client.currentOperation={armId:'a',type:'DIRECT',mode:'STREAM',ready:true};
  await panel.changeMode('ik');
  assert.equal(panel.mode,'ik');assert.equal(panel.intentGeneration,1,'only the explicit mode change invalidates the old intent');
  assert.equal(panel.expectedTermination,0);assert.equal(panel.busy,false);
  assert.equal(calls.filter(call=>call[0]==='hold').length,1);
  assert(!calls.some(call=>['enable','start','target'].includes(call[0])));
});

test('gamepad Home survives the real close event when replacing another same-arm operation',async()=>{
  const{panel,client,calls,states}=fixture();panel.mode='gamepad';panel.ownsStream=false;states.a.permission=true;
  panel.gamepad={suspend(){},operationEnded(){}};
  client.currentOperation={armId:'a',type:'TRAJECTORY',mode:'INLINE',ready:true};
  client.request=async()=>({target:target(.4,60)});
  await panel.preset('home');
  assert.deepEqual(calls.filter(call=>['hold','enable','start'].includes(call[0])),[['hold','a'],['enable','a'],['start','a']]);
  assert.deepEqual(calls.filter(call=>call[0]==='target'),[['target',target(.4,60)]]);
  assert.equal(panel.intentGeneration,1);assert.equal(panel.ownsStream,true);assert.equal(panel.busy,false);
});

test('a real disconnect close still cancels Home while an internal hold is expected',async()=>{
  const{panel,client,calls}=fixture(),gate=deferred();client.request=async()=>gate.promise;
  const home=panel.preset('home');panel.expectedTermination=1;
  const event=emitTransportClose(panel,'disconnected');panel.expectedTermination=0;
  assert.equal(event.state,'closed');assert.equal(event.terminalReason,'disconnected');
  gate.resolve({target:target(.8)});await home;
  assert(!calls.some(call=>['enable','start','target'].includes(call[0])));
});

test('uncertain late permission reply after Stop still receives cleanup hold',async()=>{
  const{panel,client,states}=fixture(),gate=deferred();
  client.enable=async()=>{await gate.promise;states.a.permission=true;throw Error('response lost after grant');};
  const start=panel.start();await panel.stop();gate.resolve();await start;
  assert.equal(states.a.permission,false);assert.equal(client.currentOperation,null);assert.equal(panel.busy,false);
});


test('unconfirmed leader stop is retained as stopping without heartbeat renewal',async()=>{
  const{panel,client,calls}=fixture();client.request=async(path,body)=>{calls.push(['request',path,body]);return{stopped:false,hold_confirmed:false};};
  const run={host:'host',run_id:'run'};
  await assert.rejects(panel.stopLeader(run),/confirmation/);
  assert.equal(panel.leaderRun,run);assert.equal(run.stopping,true);
  const count=calls.length;await panel.heartbeat();assert.equal(calls.length,count);
});

for(const active of [null,{run_id:'different-run'}])test(`host reconciliation retires absent/replaced run ${JSON.stringify(active)}`,async()=>{
  const{panel,client}=fixture();panel.leaderRun={host:'host',run_id:'old-run'};
  client.request=async()=>({hosts:[{id:'host',state:active?'ACTIVE':'IDLE',active,devices:[]}]});
  await panel.refreshHosts();assert.equal(panel.leaderRun,null);
});

test('an old host inventory response cannot erase a newly started run',async()=>{
  const{panel,client}=fixture(),gate=deferred();client.request=async()=>gate.promise;
  const refresh=panel.refreshHosts();panel.leaderRun={host:'host',run_id:'new-run'};
  gate.resolve({hosts:[{id:'host',state:'IDLE',active:null,devices:[]}]});await refresh;
  assert.equal(panel.leaderRun.run_id,'new-run');
});


test('shared FK stays visible while hidden Control cannot accept keyboard or gamepad input',()=>{
  const {panel,calls}=fixture();panel.mode='gamepad';panel.keys.add('KeyJ');
  panel.gamepad={setActive:active=>calls.push(['gamepad-active',active])};
  panel.refreshPose=()=>calls.push(['pose']);
  panel.setVisible(false,true);
  assert.equal(panel.visible,false);assert.equal(panel.poseVisible,true);assert.equal(panel.keys.size,0);
  assert(calls.some(c=>c[0]==='pose'));assert(calls.some(c=>c[0]==='gamepad-active'&&c[1]===false));
  panel.keyDown({code:'KeyJ'});assert.equal(panel.keys.size,0);
  const count=calls.length;panel.setVisible(false,false);
  assert.equal(panel.poseVisible,false);assert(!calls.slice(count).some(c=>c[0]==='pose'));
});


test('first leader takeover requests follower-to-leader motion without pose matching',async()=>{
 const {panel,client,calls}=fixture();panel.mode='leader';
 client.request=async(path,body)=>{calls.push(['request',path,body]);return {accepted:true,run_id:'fresh-follow'};};
 await panel.start();
 const request=calls.find(c=>c[0]==='request'&&c[1].endsWith('/start'));
 assert.equal(request[2].pickup,false);assert.equal(request[2].hz,100);
 assert.equal(panel.leaderRun.run_id,'fresh-follow');
 assert(calls.some(c=>c[0]==='note'&&c[1].includes('follower will approach the leader')));
});
