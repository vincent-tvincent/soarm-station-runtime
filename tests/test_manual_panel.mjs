import test from 'node:test';
import assert from 'node:assert/strict';
import {ManualPanel} from '../web/manual-panel.js';

globalThis.document = {hidden:false};
globalThis.Option = class { constructor(text,value){this.text=text;this.value=value;} };
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const target=(q=0,g=50)=>({positions:{joint:q},gripper:g,gripper_unit:'percent'});
function fixture(){
  const panel=Object.create(ManualPanel.prototype),calls=[];
  const states={a:{runtime_boot_id:'boot',controller_session:'session',feedback_valid:true,gripper_percent:50,permission:false},b:{runtime_boot_id:'bootB',controller_session:'sessionB',feedback_valid:true,gripper_percent:50,permission:false}};
  const elements=new Map();
  const el=id=>{if(!elements.has(id))elements.set(id,{value:id==='leader-host'?'host':id==='leader-device'?'leader':'',textContent:'Normal',disabled:false,dataset:{},replaceChildren(){},classList:{toggle(){}}});return elements.get(id);};
  const client={operatorSessionId:'operator',currentOperation:null,
    async enable(arm){calls.push(['enable',arm]);states[arm].permission=true;return{accepted:true};},
    async startManual(arm){calls.push(['start',arm]);this.currentOperation={armId:arm,type:'DIRECT',mode:'STREAM',ready:true};return this.currentOperation;},
    async hold(arm){calls.push(['hold',arm]);this.currentOperation=null;states[arm].permission=false;panel.updateOperation({event:'closed',state:'stopping'});return{success:true};},
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
  const start=panel.start();await panel.stop();gate.resolve({accepted:true,run_id:'late-run'});await start;
  assert.equal(panel.leaderRun,null);
  assert(calls.some(x=>x[0]==='request'&&x[1].endsWith('/stop')&&x[2].run_id==='late-run'));
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
