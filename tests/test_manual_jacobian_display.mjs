import test from 'node:test';
import assert from 'node:assert/strict';
import {ManualPanel, conditioningPresentation} from '../web/manual-panel.js';

const full = {conditioning:{available:true,reciprocal_condition:.045678,characteristic_length_m:.5}};

test('continuous conditioning changes even when the legacy rank is unchanged',()=>{
  const normal=conditioningPresentation({...full,rank:5});
  const near=conditioningPresentation({rank:5,conditioning:{...full.conditioning,reciprocal_condition:1e-7}});
  assert.equal(normal.value,'0.0457');assert.equal(near.value,'1.00e-7');
  assert.match(normal.detail,/0 = singular/);assert.doesNotMatch(normal.detail,/timestamp|rank/i);
  assert.equal(conditioningPresentation({conditioning:{...full.conditioning,reciprocal_condition:0}}).value,'0');
});

test('missing, malformed or stale conditioning never looks singular',()=>{
  for(const value of [null,{}, {rank:5}, ...[NaN,-1,1.1,null].map(reciprocal_condition=>({conditioning:{...full.conditioning,reciprocal_condition}}))])
    assert.equal(conditioningPresentation(value).value,'—');
  assert.equal(conditioningPresentation(full,true).detail,'Waiting for fresh pose');
});

test('shared FK updates measured conditioning with Control hidden and clears it when stale',async()=>{
  const nodes=new Map();
  const el=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',style:{setProperty(){}}});return nodes.get(id);};
  const pose={position_m:[.2,0,.1],elevation_rad:0,tool_roll_rad:0,gripper_percent:40,jacobian:full};
  const panel=Object.create(ManualPanel.prototype);
  let fresh=true;
  Object.assign(panel,{visible:false,poseVisible:true,connected:true,arm:'arm1',mode:'ik',hosts:[],snapshot:{motor_status:{}},el,
    measured:()=>({positions:{joint:0},gripper:40}),stateForArm:()=>({feedback_valid:fresh}),
    element:{querySelector:()=>({textContent:''})},fk:{update(){}},
    client:{request:async()=>({measured:pose,target:{...pose,jacobian:{conditioning:{...full.conditioning,reciprocal_condition:0}}}})}});
  await panel.refreshPose();
  assert.equal(el('ik-conditioning-value').textContent,'0.0457','target geometry must not replace measured conditioning');
  fresh=false;
  await panel.refreshPose();
  assert.equal(el('ik-conditioning-value').textContent,'—');
  assert.equal(el('ik-mobility-detail').textContent,'Waiting for fresh pose');
});
