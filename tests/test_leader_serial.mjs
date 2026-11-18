import test from 'node:test';
import assert from 'node:assert/strict';
import { BrowserLeaderSerial } from '../web/leader-serial.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const later = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return { promise, resolve, reject }; };
const copy = (value) => structuredClone(value);
const profile = () => ({ id:'leader',label:'USB leader',motor_model:'sts3215',
  joint_map:{pan:'shoulder_pan',elbow:'elbow_flex'},gripper_source:'jaw',match:{vid:0x1a86,pid:0x55d3},
  calibration:{pan:{id:1,drive_mode:0,homing_offset:-12,range_min:1000,range_max:3000},
    elbow:{id:2,drive_mode:0,homing_offset:33,range_min:0,range_max:4095},
    jaw:{id:3,drive_mode:0,homing_offset:0,range_min:1000,range_max:2000}} });
function reply(id,value,status=0) {
  const bytes=Uint8Array.of(255,255,id,4,status,value&255,(value>>>8)&255,0);
  bytes[7]=(~bytes.slice(2,7).reduce((a,b)=>a+b,0))&255;return bytes;
}
function join(chunks) {
  const bytes=new Uint8Array(chunks.reduce((n,v)=>n+v.length,0));let offset=0;
  for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}return bytes;
}
class FakePort {
  constructor(saved=profile()) {
    this.saved=saved;this.readable=null;this.writable=null;this.rx=null;this.state='closed';
    this.openCalls=[];this.closeCalls=0;this.writes=[];this.cancels=0;this.aborts=0;
    this.positions={1:4095,2:0x8000|100,3:1200};this.present=true;this.openGate=null;
    this.fragment=false;this.handler=null;this.info={usbVendorId:0x1a86,usbProductId:0x55d3};
  }
  getInfo(){return {...this.info};}
  async open(options) {
    if(!this.present)throw new DOMException('Device removed','NotFoundError');
    assert.equal(this.state,'closed','Port was opened twice concurrently');
    this.state='opening';this.openCalls.push(options);
    if(this.openGate)await this.openGate.promise;
    this.state='open';
    this.readable=new ReadableStream({start:controller=>{this.rx=controller;},cancel:()=>{this.cancels++;}});
    this.writable=new WritableStream({write:bytes=>{
      const packet=Uint8Array.from(bytes);this.writes.push(packet);
      assert.equal(packet[4],0x82,'Only vendor SYNC READ may reach the adapter');
      assert.equal(packet[2],254);assert.equal(packet[3],packet.length-4);assert.equal(packet[6],2);
      assert.ok([3,9,11,31,56].includes(packet[5]));
      assert.equal(packet.slice(2).reduce((a,b)=>a+b,0)&255,255);
      const responses=Array.from(packet.slice(7,-1),id=>{
        const motor=Object.values(this.saved.calibration).find(m=>m.id===id);
        const value=packet[5]===3?777:packet[5]===9?motor.range_min:packet[5]===11?motor.range_max:
          packet[5]===31?(motor.homing_offset<0?2048|-motor.homing_offset:motor.homing_offset):this.positions[id];
        return reply(id,value);
      });
      if(this.handler)return this.handler(packet,responses,this);
      this.deliver(join(responses));
    },abort:()=>{this.aborts++;}});
  }
  deliver(bytes) {
    if(!this.fragment){this.rx.enqueue(bytes);return;}
    let offset=0,index=0;
    while(offset<bytes.length){const size=[1,2,5,3][index++%4];this.rx.enqueue(bytes.slice(offset,offset+size));offset+=size;}
  }
  async close(){
    assert.equal(this.readable?.locked??false,false,'Read lock leaked into port.close');
    assert.equal(this.writable?.locked??false,false,'Write lock leaked into port.close');
    assert.equal(this.state,'open');this.state='closed';this.readable=null;this.writable=null;this.closeCalls++;
  }
}
class FakeSerial extends EventTarget {
  constructor(port){super();this.choices=[Promise.resolve(port)];this.requests=[];}
  requestPort(options){this.requests.push(options);return this.choices.shift()??Promise.reject(new DOMException('Canceled','NotFoundError'));}
  disconnect(port){const event=new Event('disconnect');Object.defineProperty(event,'port',{value:port});this.dispatchEvent(event);}
}
async function verified(options={},readerOptions={transactionTimeoutMs:30,lifecycleTimeoutMs:100}) {
  const saved=profile(),port=new FakePort(saved);Object.assign(port,options);
  const serial=new FakeSerial(port),reader=new BrowserLeaderSerial({serial,...readerOptions});
  await reader.choose(saved);await reader.verify(saved);return {reader,port,serial,saved};
}


test('feature detection and chooser run in the original synchronous gesture',async()=>{
  const port=new FakePort(),serial=new FakeSerial(port),reader=new BrowserLeaderSerial({serial});
  const chosen=reader.choose(profile());
  assert.equal(serial.requests.length,1,'requestPort was deferred beyond the gesture');
  assert.equal(reader.selected,false);assert.equal(port.openCalls.length,0);
  assert.deepEqual(serial.requests[0],{filters:[{usbVendorId:0x1a86,usbProductId:0x55d3}]});
  assert.equal((await chosen).profile_id,'leader');assert.equal(reader.selected,true);assert.equal(reader.verified,false);
  assert.equal(new BrowserLeaderSerial({serial:null}).supported,false);await reader.close();
});

test('verification reads every calibration register and samples synchronized signed positions',async()=>{
  const {reader,port}=await verified();
  try {
    assert.equal(reader.verified,true);
    assert.deepEqual(port.openCalls[0],{baudRate:1_000_000,dataBits:8,stopBits:1,parity:'none',flowControl:'none',bufferSize:1024});
    assert.deepEqual(port.writes.map(p=>p[5]),[3,9,11,31]);
    assert.ok(port.writes.every(p=>[...p.slice(7,-1)].join(',')==='1,2,3'));
    const sample=await reader.sample();
    assert.equal(sample.positions.shoulder_pan,(4095-2000)*2*Math.PI/4095);
    assert.equal(sample.positions.elbow_flex,(-100-2047.5)*2*Math.PI/4095);
    assert.ok(sample.positions.shoulder_pan>Math.PI);assert.ok(sample.positions.elbow_flex<-Math.PI);
    assert.deepEqual(Object.keys(sample),['positions','gripper','gripper_unit']);
    assert.equal(sample.gripper,20);assert.equal(sample.gripper_unit,'percent');
    assert.ok(reader.lastSampleTiming.completed_ms>=reader.lastSampleTiming.started_ms);
    assert.deepEqual(port.writes.map(p=>p[4]),[0x82,0x82,0x82,0x82,0x82]);
  } finally {await reader.close();}
  assert.equal(port.closeCalls,1);assert.equal(reader.selected,true);assert.equal(reader.verified,false);
});

test('fragmented and concatenated responses produce exactly one complete sample',async()=>{
  const {reader,port}=await verified({fragment:true});
  try {assert.equal((await reader.sample()).gripper,20);assert.equal(port.writes.length,5);}
  finally {await reader.close();}
});

test('complete unique response IDs may arrive in a different order',async()=>{
  const {reader,port}=await verified();
  port.handler=(_packet,responses)=>port.deliver(join(responses.reverse()));
  try {assert.equal((await reader.sample()).gripper,20);}finally{await reader.close();}
});

test('jaw is clamped to calibrated percent while joint angles remain unclipped',async()=>{
  const {reader,port}=await verified();
  try {
    port.positions[3]=0;assert.equal((await reader.sample()).gripper,0);
    port.positions[3]=4095;assert.equal((await reader.sample()).gripper,100);
    port.positions[1]=32767;assert.ok((await reader.sample()).positions.shoulder_pan>40);
  }finally{await reader.close();}
});

for(const [field,value] of [['motor_model','other'],['drive_mode',1],['id',254],['homing_offset',2048],['range_min',5000],['range_max',0]])
  test(`invalid profile ${field} cannot request permission or emit bytes`,async()=>{
    const saved=profile();if(field==='motor_model')saved[field]=value;else saved.calibration.pan[field]=value;
    const port=new FakePort(),serial=new FakeSerial(port),reader=new BrowserLeaderSerial({serial});
    await assert.rejects(reader.choose(saved),{code:'PROFILE_INVALID'});
    assert.equal(serial.requests.length,0);assert.equal(port.writes.length,0);
  });

test('duplicate IDs or follower mappings and incomplete calibration are rejected',async()=>{
  for(const change of [p=>p.calibration.elbow.id=1,p=>p.joint_map.elbow='shoulder_pan',p=>delete p.calibration.jaw]){
    const saved=profile();change(saved);const reader=new BrowserLeaderSerial({serial:new FakeSerial(new FakePort())});
    await assert.rejects(reader.choose(saved),{code:'PROFILE_INVALID'});
  }
});

test('mutating the original profile after verification cannot change normalization',async()=>{
  const {reader,saved}=await verified();saved.joint_map.pan='hijacked';saved.calibration.pan.range_min=3000;
  try {assert.ok(Object.hasOwn((await reader.sample()).positions,'shoulder_pan'));}finally{await reader.close();}
});

for(const address of [3,9,11,31])test(`register ${address} mismatch rejects verification without writes`,async()=>{
  const saved=profile(),port=new FakePort(saved),serial=new FakeSerial(port),reader=new BrowserLeaderSerial({serial});
  port.handler=(packet,responses)=>{if(packet[5]===address)responses[0]=reply(1,123);port.deliver(join(responses));};
  await reader.choose(saved);await assert.rejects(reader.verify(saved),{code:'CALIBRATION_MISMATCH'});
  assert.equal(reader.verified,false);assert.equal(port.state,'closed');assert.ok(port.writes.every(p=>p[4]===0x82));
});

for(const kind of ['checksum','header','length','unknown-id','duplicate','extra','servo-error','partial'])
  test(`${kind} response is never returned as a sample and releases all locks`,async()=>{
    const {reader,port}=await verified();
    port.handler=(_packet,replies)=>{
      let bytes=join(replies);
      if(kind==='checksum')bytes[7]^=1;
      if(kind==='header')bytes[0]=0;
      if(kind==='length')bytes[3]=255;
      if(kind==='unknown-id')bytes=join([reply(9,1),...replies.slice(1)]);
      if(kind==='duplicate')bytes=join([replies[0],replies[0],replies[2]]);
      if(kind==='extra')bytes=join([...replies,Uint8Array.of(255)]);
      if(kind==='servo-error')bytes=join([reply(1,1,32),...replies.slice(1)]);
      if(kind==='partial')bytes=bytes.slice(0,-1);
      port.deliver(bytes);
    };
    await assert.rejects(reader.sample(),error=>['PROTOCOL_ERROR','SERVO_ERROR','SERIAL_TIMEOUT'].includes(error.code));
    assert.equal(reader.verified,false);assert.equal(port.state,'closed');assert.equal(port.closeCalls,1);
    await reader.close();assert.equal(port.closeCalls,1);
  });

test('concurrent sample is rejected without canceling the first transaction',async()=>{
  const {reader,port}=await verified();
  try {const first=reader.sample();await assert.rejects(reader.sample(),{code:'BUSY'});assert.equal((await first).gripper,20);}
  finally {await reader.close();}
  assert.equal(port.writes.filter(p=>p[5]===56).length,1);
});

test('a broken USB read times out once with no automatic retry or reconnect',async()=>{
  const {reader,port}=await verified();port.handler=()=>{};
  await assert.rejects(reader.sample(),{code:'SERIAL_TIMEOUT'});
  assert.equal(port.state,'closed');assert.equal(port.writes.length,5);assert.equal(port.openCalls.length,1);
  await assert.rejects(reader.sample(),{code:'NOT_VERIFIED'});assert.equal(port.openCalls.length,1);
});

test('AbortSignal cancels a pending read and closes the port',async()=>{
  const {reader,port}=await verified();port.handler=()=>{};const abort=new AbortController();
  const sample=reader.sample({signal:abort.signal});const rejected=assert.rejects(sample,{name:'AbortError'});
  await sleep(4);abort.abort();await rejected;
  assert.equal(port.state,'closed');assert.equal(reader.verified,false);assert.equal(reader.selected,true);
});

test('already-aborted or malformed signal cannot leave a busy sample slot',async()=>{
  const {reader,port}=await verified();
  await assert.rejects(reader.sample({signal:{}}),{code:'INVALID_SIGNAL'});
  assert.equal((await reader.sample()).gripper,20);
  const abort=new AbortController();abort.abort();await assert.rejects(reader.sample({signal:abort.signal}),{name:'AbortError'});
  assert.equal(port.state,'closed');
});

test('close cancels a stalled read; explicit verify is required for reuse',async()=>{
  const {reader,port,saved}=await verified();port.handler=()=>{};
  const sample=reader.sample();const rejected=assert.rejects(sample,{name:'AbortError'});
  await sleep(4);await Promise.all([reader.close(),reader.close(),rejected]);
  assert.equal(port.state,'closed');assert.equal(port.closeCalls,1);assert.equal(reader.selected,true);
  port.handler=null;await reader.verify(saved);assert.equal((await reader.sample()).gripper,20);
  assert.equal(port.openCalls.length,2);await reader.close();
});

test('USB disconnect aborts the current operation but retains the selected object',async()=>{
  const {reader,port,serial,saved}=await verified();port.handler=()=>{};
  const sample=reader.sample();const rejected=assert.rejects(sample,{code:'SERIAL_DISCONNECTED'});
  await sleep(4);port.present=false;serial.disconnect(port);await rejected;
  assert.equal(reader.selected,true);assert.equal(reader.verified,false);assert.equal(port.state,'closed');
  await assert.rejects(reader.verify(saved),{name:'NotFoundError'});assert.equal(port.openCalls.length,1);
});

test('unexpected idle replies poison verification instead of becoming the next sample',async()=>{
  const {reader,port}=await verified();port.deliver(reply(1,2500));await sleep(3);
  assert.equal(reader.verified,false);await reader.close();assert.equal(port.state,'closed');
  await assert.rejects(reader.sample(),{code:'NOT_VERIFIED'});
});

test('stale chooser cannot replace a newer selection',async()=>{
  const a=new FakePort(),b=new FakePort(),serial=new FakeSerial(a),old=later(),fresh=later();
  serial.choices=[old.promise,fresh.promise];const reader=new BrowserLeaderSerial({serial});
  const first=reader.choose(profile());const rejected=assert.rejects(first,{name:'AbortError'});
  const second=reader.choose(profile());fresh.resolve(b);await second;old.resolve(a);await rejected;
  await reader.verify(profile());assert.equal(a.openCalls.length,0);assert.equal(b.openCalls.length,1);await reader.close();
});

test('close invalidates an outstanding chooser without opening its result',async()=>{
  const port=new FakePort(),serial=new FakeSerial(port),choice=later();serial.choices=[choice.promise];
  const reader=new BrowserLeaderSerial({serial}),pending=reader.choose(profile());const rejected=assert.rejects(pending,{name:'AbortError'});
  await reader.close();choice.resolve(port);await rejected;assert.equal(reader.selected,false);assert.equal(port.openCalls.length,0);
});

test('a canceled chooser preserves the last selected port without rearming it',async()=>{
  const {reader,port,serial,saved}=await verified();serial.choices=[Promise.reject(new DOMException('Canceled','NotFoundError'))];
  await assert.rejects(reader.choose(saved),{name:'NotFoundError'});await reader.close();
  assert.equal(reader.selected,true);assert.equal(reader.verified,false);assert.equal(port.closeCalls,1);
  await reader.verify(saved);assert.equal(port.openCalls.length,2);await reader.close();
});

test('close during asynchronous open closes the late port and never verifies it',async()=>{
  const port=new FakePort(),serial=new FakeSerial(port),reader=new BrowserLeaderSerial({serial});port.openGate=later();
  await reader.choose(profile());const verify=reader.verify(profile());const rejected=assert.rejects(verify,{name:'AbortError'});
  await sleep(1);const close=reader.close();port.openGate.resolve();await Promise.all([close,rejected]);
  assert.equal(port.state,'closed');assert.equal(port.writes.length,0);assert.equal(reader.verified,false);
});

test('overlapping verify calls retire the earlier open before starting another',async()=>{
  const port=new FakePort(),serial=new FakeSerial(port),reader=new BrowserLeaderSerial({serial});port.openGate=later();
  await reader.choose(profile());const first=reader.verify(profile());const rejected=assert.rejects(first,{name:'AbortError'});
  await sleep(1);const second=reader.verify(profile());port.openGate.resolve();await rejected;await second;
  assert.equal(port.openCalls.length,2);assert.equal(port.closeCalls,1);assert.equal(reader.verified,true);await reader.close();
});

test('choosing another port during open never lets late verification replace it',async()=>{
  const a=new FakePort(),b=new FakePort(),serial=new FakeSerial(a),reader=new BrowserLeaderSerial({serial});a.openGate=later();
  await reader.choose(profile());const first=reader.verify(profile());const rejected=assert.rejects(first,{name:'AbortError'});
  await sleep(1);serial.choices=[Promise.resolve(b)];const chosen=reader.choose(profile());a.openGate.resolve();
  await Promise.all([chosen,rejected]);await reader.verify(profile());assert.equal(a.state,'closed');assert.equal(a.writes.length,0);
  assert.equal(b.state,'open');assert.equal(reader.verified,true);await reader.close();
});

test('a 300ms USB transaction remains valid; caller may drop its expired grant without reconnect',async()=>{
  const {reader,port}=await verified({},{}); // Production timeout defaults, not the short test bound.
  port.handler=(_packet,responses)=>{setTimeout(()=>port.deliver(join(responses)),300);};
  try {
    assert.equal((await reader.sample()).gripper,20);
    assert.ok(reader.lastSampleTiming.completed_ms-reader.lastSampleTiming.started_ms>=290);
    assert.equal(reader.verified,true);assert.equal(port.openCalls.length,1);
    port.handler=null;assert.equal((await reader.sample()).gripper,20);
  } finally {await reader.close();}
});

test('a permanently pending open has bounded public waits and quarantines the same port',async()=>{
  const port=new FakePort(),serial=new FakeSerial(port),reader=new BrowserLeaderSerial({serial,lifecycleTimeoutMs:20});
  port.openGate=later();await reader.choose(profile());const started=performance.now();
  await assert.rejects(reader.verify(profile()),{code:'SERIAL_OPEN_TIMEOUT'});
  assert.ok(performance.now()-started<300);assert.equal(reader.verified,false);assert.equal(port.writes.length,0);
  await assert.rejects(reader.close(),{code:'SERIAL_CLOSE_TIMEOUT'});
  await assert.rejects(reader.verify(profile()),{code:'SERIAL_BUSY'});assert.equal(port.openCalls.length,1);
  port.openGate.resolve();await sleep(2);await reader.close();assert.equal(port.state,'closed');
  await reader.verify(profile());assert.equal(port.openCalls.length,2);await reader.close();
});

test('a late old open cannot interrupt a verified replacement on another port',async()=>{
  const old=new FakePort(),fresh=new FakePort(),serial=new FakeSerial(old);
  const reader=new BrowserLeaderSerial({serial,lifecycleTimeoutMs:20});old.openGate=later();
  await reader.choose(profile());await assert.rejects(reader.verify(profile()),{code:'SERIAL_OPEN_TIMEOUT'});
  serial.choices=[Promise.resolve(fresh)];await reader.choose(profile());await reader.verify(profile());
  old.openGate.resolve();await sleep(2);
  assert.equal(old.state,'closed');assert.equal(old.writes.length,0);assert.equal(reader.verified,true);
  assert.equal((await reader.sample()).gripper,20);await reader.close();
});

test('a stuck write cannot hold either stream lock or contaminate a later read',async()=>{
  const {reader,port,saved}=await verified(),write=later();let oldController;
  port.handler=(_packet,responses)=>{
    oldController=port.rx;
    return write.promise.then(()=>{assert.throws(()=>oldController.enqueue(join(responses)));});
  };
  const started=performance.now();await assert.rejects(reader.sample(),{code:'SERIAL_TIMEOUT'});
  assert.ok(performance.now()-started<300);assert.equal(port.state,'closed');assert.equal(reader.verified,false);
  port.handler=null;port.positions[1]=2000;await reader.verify(saved);
  write.resolve();await sleep(2);
  const sample=await reader.sample();assert.equal(sample.positions.shoulder_pan,0);assert.equal(reader.verified,true);
  await reader.close();
});

test('a stuck native close releases JS locks and does not allow overlapping reopen',async()=>{
  const {reader,port,saved}=await verified({}, {transactionTimeoutMs:30,lifecycleTimeoutMs:20});
  const closeGate=later(),original=port.close.bind(port);let closeCalls=0;
  port.close=async()=>{closeCalls++;assert.equal(port.readable.locked,false);assert.equal(port.writable.locked,false);await closeGate.promise;await original();};
  await assert.rejects(reader.close(),{code:'SERIAL_CLOSE_TIMEOUT'});
  assert.equal(port.readable.locked,false);assert.equal(port.writable.locked,false);
  await assert.rejects(reader.verify(saved),{code:'SERIAL_BUSY'});assert.equal(port.openCalls.length,1);assert.equal(closeCalls,1);
  closeGate.resolve();await sleep(2);await reader.close();await reader.verify(saved);await reader.close();
  assert.equal(port.state,'closed');assert.equal(closeCalls,2);
});

test('old buffered bytes are drained only before verification, never used as calibration replies',async()=>{
  const port=new FakePort(),original=port.open.bind(port);
  port.open=async options=>{await original(options);port.rx.enqueue(join([reply(1,123),Uint8Array.of(1,2,3)]));};
  const reader=new BrowserLeaderSerial({serial:new FakeSerial(port)});
  await reader.choose(profile());await reader.verify(profile());
  assert.equal((await reader.sample()).gripper,20);assert.deepEqual(port.writes.slice(0,4).map(p=>p[5]),[3,9,11,31]);
  await reader.close();
});

test('reserved homing bits cannot masquerade as the saved signed offset',async()=>{
  const saved=profile(),port=new FakePort(saved),reader=new BrowserLeaderSerial({serial:new FakeSerial(port)});
  port.handler=(packet,responses)=>{if(packet[5]===31)responses[0]=reply(1,0x880c);port.deliver(join(responses));};
  await reader.choose(saved);await assert.rejects(reader.verify(saved),{code:'PROTOCOL_ERROR'});
  assert.equal(port.state,'closed');assert.equal(reader.verified,false);
});
