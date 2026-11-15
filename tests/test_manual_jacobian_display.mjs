import test from 'node:test';
import assert from 'node:assert/strict';
import {ManualPanel, jacobianRankPresentation} from '../web/manual-panel.js';

const full = {rank:5,max_rank:5,rows:6,columns:5};

test('Jacobian rank is the matrix rank, not a condition-derived score',()=>{
  const good = jacobianRankPresentation({...full,condition_number:2});
  const illConditioned = jacobianRankPresentation({...full,condition_number:1e9});
  assert.deepEqual(good,illConditioned);
  assert.equal(good.rank,'5 / 5');
  assert.equal(good.color,'rgb(25, 25, 25)');
  assert.match(good.detail,/6 × 5 geometric Jacobian/);
});

test('each loss of rank increases red intensity, including zero rank',()=>{
  const red = value => Number(value.color.match(/^rgb\((\d+)/)[1]);
  const grades = [5,4,3,2,1,0].map(rank=>jacobianRankPresentation({...full,rank}));
  for(let i=1;i<grades.length;i++)assert.ok(red(grades[i])>red(grades[i-1]));
  assert.equal(grades.at(-1).rank,'0 / 5');
  assert.equal(grades.at(-1).color,'rgb(217, 35, 27)');
});

test('missing, malformed, or stale rank never looks like a valid low rank',()=>{
  for(const value of [null,{}, {...full,rank:6},{...full,rank:NaN},{...full,max_rank:0}])
    assert.equal(jacobianRankPresentation(value).rank,'—');
  assert.deepEqual(jacobianRankPresentation(full,true),{rank:'—',color:'#666',detail:'Waiting for fresh pose'});
});

test('FK display uses the current measured Jacobian and clears it when feedback is stale',async()=>{
  const nodes=new Map();
  const el=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',style:{setProperty(){}}});return nodes.get(id);};
  const pose={position_m:[.2,0,.1],elevation_rad:0,tool_roll_rad:0,gripper_percent:40,jacobian:full};
  const panel=Object.create(ManualPanel.prototype);
  let fresh=true;
  Object.assign(panel,{visible:true,connected:true,arm:'arm1',mode:'ik',hosts:[],snapshot:{motor_status:{}},el,
    measured:()=>({positions:{joint:0},gripper:40}),stateForArm:()=>({feedback_valid:fresh}),
    element:{querySelector:()=>({textContent:''})},fk:{update(){}},
    client:{request:async()=>({measured:pose,target:{...pose,jacobian:{...full,rank:0}}})}});
  await panel.refreshPose();
  assert.equal(el('ik-mobility-rank').textContent,'5 / 5','target Jacobian must not replace measured rank');
  fresh=false;
  await panel.refreshPose();
  assert.equal(el('ik-mobility-rank').textContent,'—');
  assert.equal(el('ik-mobility-detail').textContent,'Waiting for fresh pose');
});
