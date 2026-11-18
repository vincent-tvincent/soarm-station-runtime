import test from 'node:test';
import assert from 'node:assert/strict';
import {BrowserLeaderPanel,validateBrowserLeaderProfile} from '../web/browser-leader-panel.js';
import {ManualPanel} from '../web/manual-panel.js';

const copy=value=>structuredClone(value),flush=async()=>{for(let i=0;i<18;i++)await Promise.resolve();};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
const sample=(value=.2,gripper=60)=>({positions:{joint:value},gripper,gripper_unit:'percent'});
const profile=()=>({id:'desk',label:'Desk leader',motor_model:'sts3215',joint_map:{source_joint:'joint'},gripper_source:'jaw',
  calibration:{source_joint:{id:1,drive_mode:0,homing_offset:0,range_min:0,range_max:4095},
    jaw:{id:2,drive_mode:0,homing_offset:0,range_min:100,range_max:3000}}});
class Element {
  constructor(){this.nodes=new Map();this.value='';this.files=[];this.hidden=false;this.dataset={};this.attributes={};this.classList={toggle(){}};}
  querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new Element());return this.nodes.get(selector);}
  querySelectorAll(){return[];}
  replaceChildren(...children){this.children=children;}
  setAttribute(name,value){this.attributes[name]=String(value);}
}
function fixture(t,{paired=true}={}) {
  const oldDocument=Object.getOwnPropertyDescriptor(globalThis,'document'),oldOption=Object.getOwnPropertyDescriptor(globalThis,'Option');
  Object.defineProperty(globalThis,'document',{configurable:true,value:{hidden:false}});
  Object.defineProperty(globalThis,'Option',{configurable:true,value:class{constructor(label,value){this.textContent=label;this.value=value;}}});
  t.after(()=>{if(oldDocument)Object.defineProperty(globalThis,'document',oldDocument);else delete globalThis.document;
    if(oldOption)Object.defineProperty(globalThis,'Option',oldOption);else delete globalThis.Option;});
  const log=[],errors=[],element=new Element(),controls=new Element(),p=profile();let serialSample=0,localId=0,time=10;
  const serial={supported:true,selected:paired,open:0,
    choose(value){log.push(['choose',value.id]);this.selected=true;return Promise.resolve({profile_id:value.id});},
    async verify(value){log.push(['verify',value.id]);this.open=1;return{verified:true};},
    async sample(){log.push(['sample',++serialSample]);return sample(serialSample/10,serialSample===1?10:70);},
    close(){log.push(['serial-close']);this.open=0;return Promise.resolve();}};
  const states={arm1:{runtime_boot_id:'boot1',controller_session:'session1',permission:false,feedback_valid:true,gripper_percent:50},
    arm2:{runtime_boot_id:'boot2',controller_session:'session2',permission:false,feedback_valid:true,gripper_percent:40}};
  const resources={arms:[{id:'arm1',joints:[{name:'joint'}],gripper:{name:'gripper'}},
    {id:'arm2',joints:[{name:'joint'}],gripper:{name:'gripper'}}]};
  const manual=Object.create(ManualPanel.prototype);
  const client={connected:true,operatorSessionId:'operator',currentOperation:null,options:null,
    ownsPermission:arm=>states[arm].permission,
    async enable(arm){log.push(['enable',arm]);states[arm].permission=true;return{accepted:true,permission:true};},
    async startManual(arm,options={}){log.push(['start',arm]);this.options=options;
      const op={localId:'local-'+ ++localId,operationId:'native-'+localId,armId:arm,type:'DIRECT',mode:'STREAM',transport:'websocket',socketOpen:true,ready:true};
      this.currentOperation=op;return op;},
    async hold(arm,scope){log.push(['hold',arm,copy(scope)]);
      const op=this.currentOperation;assert.equal(scope.operation_id,op.operationId);
      this.currentOperation=null;states[arm].permission=false;
      manual.updateOperation({...op,event:'closed',state:'closed',terminalReason:'stopping'});return{success:true};},
    async retireOperation(id){log.push(['retire',id]);if(this.currentOperation?.localId!==id)return false;
      const op=this.currentOperation;this.currentOperation=null;states[op.armId].permission=false;
      manual.updateOperation({...op,event:'closed',state:'closed',terminalReason:'stopping'});return true;},
    async releaseIdlePermission(arm,scope){log.push(['idle-release',arm,copy(scope)]);if(this.currentOperation?.armId===arm)return{success:false};
      states[arm].permission=false;return{success:true};},
    setManualTarget(target){log.push(['manual-target',copy(target)]);},
    async request(path,body){log.push(['request',path,body?copy(body):undefined]);
      if(path==='/api/leader-profiles')return body?{profile:{...p,id:body.id,label:body.label,calibration:body.calibration}}:{profiles:[p]};
      if(path.endsWith('/preset'))return{target:sample(.9,50)};
      return{hosts:[]};},
  };
  Object.assign(manual,{client,arm:'arm1',mode:'leader',leaderSource:'browser',followGripper:true,connected:true,visible:true,poseVisible:true,
    config:resources.arms[0],resources,snapshot:{arms:states,joints:{arm1:{name:['joint'],position:[0]},arm2:{name:['joint'],position:[0]}}},
    keys:new Set(),epoch:0,intentGeneration:0,pendingActions:0,expectedTermination:0,busy:false,ownsStream:false,leaderRun:null,
    target:sample(0,50),reference:sample(0,50),element:controls,el:id=>controls.querySelector('#'+id),
    updateControls(){},renderSliders(){},refreshPose(){},loadDescription:async()=>{},renderConditioning(){},fk:{resetArm(){}},
    preservedGripper:measured=>measured.gripper,onError:error=>errors.push(error),note:text=>log.push(['note',text]),
    installJogSource(){log.push(['jog-source']);},
  });
  const panel=new BrowserLeaderPanel({element,manual,serial,now:()=>time});manual.browserLeader=panel;
  panel.profiles=[p];panel.selectedId=p.id;panel.renderProfiles();
  return{manual,client,panel,serial,log,errors,states,resources,profile:p,setTime:value=>{time=value;}};
}

test('USB chooser is invoked synchronously in the Pair click, without fetching or enabling first',async t=>{
  const f=fixture(t,{paired:false}),gate=deferred();
  f.serial.choose=value=>{f.log.push(['choose',value.id]);return gate.promise.then(()=>{f.serial.selected=true;});};
  const pairing=f.panel.el('browser-leader-pair').onclick();
  assert.deepEqual(f.log,[['choose','desk']]);assert.equal(f.panel.pairing,true);
  gate.resolve();await pairing;assert.equal(f.panel.paired,true);
  assert(!f.log.some(([kind])=>kind==='enable'||kind==='verify'||kind==='start'));
});

test('chooser cancellation and unsupported browsers never enable or start control',async t=>{
  const f=fixture(t,{paired:false});f.serial.choose=()=>Promise.reject(new DOMException('Canceled','NotFoundError'));
  assert.equal(await f.panel.pair(),false);assert.equal(f.panel.paired,false);assert.equal(f.errors.length,0);
  f.serial.supported=false;f.panel.render();
  assert.match(f.panel.el('browser-leader-status').textContent,/desktop Chrome or Edge/);
  assert.equal(f.panel.el('browser-leader-pair').disabled,true);
  await assert.rejects(f.manual.start(),/Web Serial/);
  assert(!f.log.some(([kind])=>kind==='enable'||kind==='start'));
});

test('joint-map mismatch and physical calibration mismatch are rejected before permission',async t=>{
  const f=fixture(t);f.panel.profiles=[{...f.profile,joint_map:{source_joint:'wrong_follower'}}];
  await assert.rejects(f.manual.start(),/does not match/);
  assert(!f.log.some(([kind])=>['verify','enable','start'].includes(kind)));
  f.panel.profiles=[f.profile];f.serial.verify=async()=>{throw Error('Leader calibration does not match');};
  await assert.rejects(f.manual.start(),/calibration does not match/);
  assert.equal(f.serial.open,0);assert(!f.log.some(([kind])=>kind==='enable'||kind==='start'));
  assert.throws(()=>validateBrowserLeaderProfile({...f.profile,joint_map:{a:'joint',b:'joint'}},f.manual.config),/does not match/);
});

test('Follow verifies and discards preflight before permission, then transport requests a new fresh sample',async t=>{
  const f=fixture(t);await f.manual.start();
  assert.deepEqual(f.log.filter(([kind])=>['verify','sample','enable','start'].includes(kind)).map(([kind])=>kind),
    ['verify','sample','enable','start']);
  const body=await f.client.options.readLeader({signal:new AbortController().signal});
  assert.equal(body.positions.joint,.2);assert.equal(body.gripper,70,'the .1 /10% preflight sample is not replayed');
  f.client.options.onLeaderSent(body);assert.equal(f.manual.ownsStream,true);
  assert(!f.log.some(([kind])=>kind==='manual-target'||kind==='jog-source'));
  await f.manual.stop();
});

test('Freeze releases only its owned run/reader and a new Follow reuses the selected port with re-verification',async t=>{
  const f=fixture(t,{paired:false});await f.panel.pair();await f.manual.start();
  const old=f.client.currentOperation;await f.manual.stop();
  assert.equal(f.serial.open,0);assert.equal(f.panel.paired,true);assert.equal(f.client.currentOperation,null);
  const hold=f.log.find(([kind])=>kind==='hold');
  assert.deepEqual(hold[2],{controller_session:'session1',operator_session_id:'operator',operation_id:old.operationId});
  assert.ok(f.log.findIndex(([kind])=>kind==='hold')<f.log.findIndex(([kind])=>kind==='serial-close'));
  await f.manual.start();assert.equal(f.log.filter(([kind])=>kind==='choose').length,1);
  assert.equal(f.log.filter(([kind])=>kind==='verify').length,2);
  await f.manual.stop();
});

test('gripper follow off preserves the last transmitted jaw, including a toggle during an in-flight read',async t=>{
  const f=fixture(t);await f.manual.start();
  const first=await f.client.options.readLeader({});f.client.options.onLeaderSent(first);
  const gate=deferred();f.serial.sample=()=>gate.promise;
  const reading=f.client.options.readLeader({});await f.manual.setGripperFollow(false);
  gate.resolve(sample(.3,5));const body=await reading;
  assert.equal(body.gripper,70);assert.equal(body.positions.joint,.3);
  assert.equal(f.panel.overlay.gripper,5,'overlay remains the observed leader, not the frozen follower jaw');
  f.client.options.onLeaderSent(body);await f.manual.stop();
});

test('onLeaderSent updates status DOM only once while successive samples update pose caches',async t=>{
  const f=fixture(t);await f.manual.start();let renders=0;const render=f.panel.render.bind(f.panel);
  f.panel.render=()=>{renders++;render();};
  for(let i=0;i<20;i++)f.client.options.onLeaderSent(sample(i/100,40));
  assert.equal(renders,1);assert.equal(f.manual.target.positions.joint,.19);
  await f.manual.stop();
});

for(const change of ['freeze','mode','tab','arm','disconnect','connection-event'])test(`${change} during USB verification closes late-opened serial and cannot request motion permission`,async t=>{
  const f=fixture(t),gate=deferred();
  f.serial.verify=async()=>{f.log.push(['verify']);await gate.promise;f.serial.open=1;return{verified:true};};
  const starting=f.manual.start();await flush();let transition;
  if(change==='freeze')transition=f.manual.stop();
  if(change==='mode')transition=f.manual.changeMode('joint');
  if(change==='tab')f.manual.setVisible(false,true);
  if(change==='arm')f.manual.update({snapshot:f.manual.snapshot,resources:f.resources,arm:'arm2',connected:true});
  if(change==='disconnect'){f.client.connected=false;f.manual.update({snapshot:f.manual.snapshot,resources:f.resources,arm:'arm1',connected:false});}
  if(change==='connection-event'){f.client.connected=false;ManualPanel.prototype.updateControls.call(f.manual);}
  gate.resolve();await starting;await transition;await flush();
  assert.equal(f.serial.open,0);assert.equal(f.panel.session,null);assert.equal(f.panel.paired,true);
  assert(!f.log.some(([kind])=>kind==='enable'||kind==='start'));assert.equal(f.errors.length,0);
});

test('Freeze during permission acquisition prevents a late grant from opening a leader run',async t=>{
  const f=fixture(t),gate=deferred();
  f.client.enable=async arm=>{f.log.push(['enable',arm]);await gate.promise;f.states[arm].permission=true;return{accepted:true};};
  const starting=f.manual.start();await flush();await f.manual.stop();gate.resolve();await starting;
  assert(!f.log.some(([kind])=>kind==='start'));assert.equal(f.states.arm1.permission,false);assert.equal(f.serial.open,0);
});

test('Home closes USB following before sending its separate preset target',async t=>{
  const f=fixture(t);await f.manual.start();await f.manual.preset('home');
  assert.equal(f.panel.session,null);assert.equal(f.serial.open,0);assert.equal(f.panel.paired,true);
  const hold=f.log.findIndex(([kind])=>kind==='hold'),preset=f.log.findIndex(([kind,path])=>kind==='request'&&path.endsWith('/preset'));
  assert.ok(hold>=0&&preset>hold);assert.equal(f.client.options.readLeader,undefined,'the Home stream is not fed by USB samples');
  assert.ok(f.log.some(([kind])=>kind==='manual-target'));
});

test('import posts calibration under an explicit profile/follower and collapses the successful import drawer',async t=>{
  const f=fixture(t);f.panel.el('browser-leader-import').hidden=false;
  f.panel.el('browser-leader-import-id').value='desk-copy';f.panel.el('browser-leader-import-label').value='Desk copy';
  f.panel.el('browser-leader-calibration').files=[{text:async()=>JSON.stringify(f.profile.calibration)}];
  await f.panel.importProfile();
  const request=f.log.find(([kind,path,body])=>kind==='request'&&path==='/api/leader-profiles'&&body);
  assert.deepEqual(request[2],{id:'desk-copy',label:'Desk copy',arm_id:'arm1',calibration:f.profile.calibration});
  assert.equal(f.panel.selectedId,'desk-copy');assert.equal(f.panel.el('browser-leader-import').hidden,true);
  assert.equal(f.panel.el('browser-leader-import-toggle').attributes['aria-expanded'],'false');
  assert(!f.log.some(([kind])=>['choose','verify','enable','start'].includes(kind)));
});

test('a terminal USB/follower operation event releases the reader without reconnecting or restarting',async t=>{
  const f=fixture(t);await f.manual.start();const op=f.client.currentOperation,starts=f.log.filter(([kind])=>kind==='start').length;
  f.manual.updateOperation({...op,event:'error',state:'error',detail:'USB disconnected'});f.client.currentOperation=null;await flush();
  assert.equal(f.panel.session,null);assert.equal(f.serial.open,0);assert.equal(f.manual.ownsStream,false);
  assert.equal(f.log.filter(([kind])=>kind==='start').length,starts);
});
