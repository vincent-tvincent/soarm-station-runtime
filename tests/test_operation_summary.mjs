import test from 'node:test';
import assert from 'node:assert/strict';
import {operationHealthSummary} from '../web/fk-view.js';
const row=(name,temperature_c,voltage_v,torque_enabled=true)=>({name,temperature_c,voltage_v,torque_enabled,source_state:'measured'});

test('motor means include gripper and only finite available readings, with coverage',()=>{
 const motors=[row('joint',30,12),row('gripper',40,11),row('missing',null,NaN)];
 const s=operationHealthSummary({motors});
 assert.deepEqual(s.temperature,{value:35,count:2,total:3});assert.equal(s.voltage.value,11.5);
 assert.equal(s.torque.value,'On');assert.equal(s.errors.value,'None');
});
test('stale rows and stale transport cannot manufacture healthy averages or torque',()=>{
 const motors=[row('good',30,12),{...row('old',90,20),source_state:'stale'}];
 const s=operationHealthSummary({motors});assert.equal(s.temperature.value,30);assert.equal(s.torque.value,'Unknown');
 const old=operationHealthSummary({motors,stale:true});assert.equal(old.temperature.value,null);assert.equal(old.voltage.value,null);assert.equal(old.torque.value,'Unknown');assert.equal(old.errors.value,'Stale');
 assert.equal(operationHealthSummary({motors,connected:false}).errors.value,'Offline');
});
test('measured mixed/off/unknown torque is not collapsed to an on value',()=>{
 assert.equal(operationHealthSummary({motors:[row('a',30,12,true),row('b',30,12,false)]}).torque.value,'Mixed');
 assert.equal(operationHealthSummary({motors:[row('a',30,12,false)]}).torque.value,'Off');
 assert.equal(operationHealthSummary({motors:[row('a',30,12,null)]}).torque.value,'Unknown');
});
test('operator hold is normal; real runtime errors and retained motor issues remain visible',()=>{
 const motors=[row('a',30,12)];
 for(const fault of ['OPERATOR_HOLD','CONTROL_OWNER_DISCONNECTED','TORQUE_DISABLED'])assert.equal(operationHealthSummary({motors,arm:{phase:'HOLD',fault}}).errors.value,'None');
 assert.equal(operationHealthSummary({motors,arm:{phase:'FAULT',fault:'Servo failed'}}).errors.active,true);
 const issues=new Map([['a',{kind:'stall',active:true,code:'TRACKING_STALLED',reason:'Cannot track target'}]]);
 const stale=operationHealthSummary({motors,issues,stale:true});assert.equal(stale.errors.value,'1 issue');assert.match(stale.errors.detail,/retained/i);
 assert.equal(operationHealthSummary({motors,recordError:'Malformed telemetry'}).errors.active,true);
 assert.equal(operationHealthSummary({motors,inference:[{level:2,name:'policy',message:'Failed'}]}).errors.active,true);
});
