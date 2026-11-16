import test from 'node:test';
import assert from 'node:assert/strict';
import {JointIssueColors, JointIssueCache, motorIssuePresentation} from '../web/fk-view.js';

const motor=(issue,source_state='measured')=>({name:'elbow',source_state,motion_issue:issue});
const positive=(code='TRACKING_NOT_PROGRESSING',kind='stall',extra={})=>({
  active:true,kind,state:kind,code,feedback_fresh:true,clear_kinds:[],
  reason:'No measured progress for 1.2 s',source:'runtime_progress',tracking_error:.123,
  tracking_error_unit:'rad',...extra,
});
const clear=kinds=>({state:'clear',active:null,kind:null,feedback_fresh:true,clear_kinds:kinds,
  reason:'Fresh following feedback is within the configured goal tolerance.'});
const unknown={state:'unknown',active:null,kind:null,feedback_fresh:false,clear_kinds:[],reason:'Feedback unavailable'};

test('software tracking reports have distinct labels from reported servo protection',()=>{
  for(const [code,kind,label,source] of [
    ['TRACKING_NOT_PROGRESSING','stall','Tracking stalled','runtime_progress'],
    ['TRACKING_STALLED','stall','Tracking stalled','runtime_error'],
    ['REFERENCE_OUTSIDE_ENVELOPE','fault','Tracking blocked','runtime_tracking'],
    ['TRACKING_ENVELOPE_LOST','fault','Tracking blocked','runtime_tracking'],
    ['SERVO_PROTECTION','fault','Servo protection','runtime_error'],
    ['FEEDBACK_ERROR','fault','Feedback error','runtime_error'],
    ['CALIBRATION_MISMATCH','fault','Calibration fault','runtime_error'],
  ]) {
    const colors=new JointIssueColors(),row=motor(positive(code,kind,{source}));colors.update([row]);
    const display=motorIssuePresentation(row,colors.states);
    assert.equal(display.label,label);assert.equal(display.active,true);assert.equal(display.state,'active');
    assert.ok(display.detail.includes(code));assert.ok(display.detail.includes(kind));
    assert.ok(display.detail.includes(row.motion_issue.reason));
    assert.ok(display.detail.includes('tracking error 0.123 rad'));assert.ok(display.detail.includes(source));
  }
});

test('unknown or missing telemetry retains the prior issue and identifies it as a retained report',()=>{
  const colors=new JointIssueColors();colors.update([motor(positive())]);
  for(const row of [motor(unknown),{name:'elbow'},motor({...unknown,feedback_fresh:true})]) {
    colors.update([row]);const display=motorIssuePresentation(row,colors.states);
    assert.equal(display.active,true);assert.equal(display.state,'retained');
    assert.equal(display.label,'Tracking stalled · retained');assert.match(display.detail,/TRACKING_NOT_PROGRESSING/);
  }
});

test('transport staleness and unavailable motor rows cannot clear latched issues even with a stale fresh flag',()=>{
  for(const [row,options] of [[motor(clear(['stall','fault'])),{stale:true}],
    [motor(clear(['stall','fault']),'stale'),{}],[motor(clear(['stall','fault']),'unavailable'),{}]]) {
    const colors=new JointIssueColors();colors.update([motor(positive('SERVO_PROTECTION','fault'))]);
    colors.update([row],options);
    const display=motorIssuePresentation(row,colors.states,options);
    assert.equal(display.active,true);assert.equal(display.state,'retained');assert.match(display.label,/Servo protection/);
  }
});

test('overlapping software and servo reports remain distinct until their own fresh clear signals arrive',()=>{
  const colors=new JointIssueColors();colors.update([motor(positive('SERVO_PROTECTION','fault'))]);
  colors.update([motor(positive())]);
  let display=motorIssuePresentation(motor(positive()),colors.states);
  assert.match(display.label,/Servo protection \+1/);
  assert.match(display.detail,/SERVO_PROTECTION/);assert.match(display.detail,/TRACKING_NOT_PROGRESSING/);
  colors.update([motor(clear(['stall']))]);display=motorIssuePresentation(motor(clear(['stall'])),colors.states);
  assert.equal(display.active,true);assert.equal(display.kind,'fault');
  colors.update([motor(clear(['fault']))]);display=motorIssuePresentation(motor(clear(['fault'])),colors.states);
  assert.equal(display.active,false);assert.equal(display.label,'Following');
});

test('missing or unknown reports do not manufacture a clear state or a servo fault from legacy fields',()=>{
  for(const row of [{name:'elbow',fault:0},{name:'elbow'},motor(unknown)]) {
    const display=motorIssuePresentation(row,new Map());
    assert.equal(display.state,'unknown');assert.equal(display.label,'Not reported');assert.equal(display.active,false);
  }
  const stale=motorIssuePresentation(motor(clear(['fault','stall'])),new Map(),{stale:true});
  assert.equal(stale.state,'stale');assert.equal(stale.label,'Status stale');
});

test('issue caches are scoped to arm/session and retire old sessions while missing identity retains the current cache',()=>{
  const cache=new JointIssueCache();
  const first=cache.update('arm-a','session-1',[motor(positive())]);
  assert.equal(cache.forArm('arm-b','session-1').states.size,0);
  assert.equal(cache.update('arm-a','',[motor(unknown)]),first);
  assert.equal(first.states.size,1);
  const replacement=cache.update('arm-a','session-2',[motor(unknown)]);
  assert.notEqual(replacement,first);assert.equal(replacement.states.size,0);
  assert.equal(cache.caches.has(JSON.stringify(['arm-a','session-1'])),false);
  for(let index=3;index<100;index++)cache.update('arm-a','session-'+index,[motor(unknown)]);
  assert.equal(cache.caches.size,2,'at most the current session per observed arm');
  assert.equal(cache.forArm('arm-a').states.size,0);
});
