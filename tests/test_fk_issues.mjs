import test from 'node:test';
import assert from 'node:assert/strict';
import {JointIssueColors} from '../web/fk-view.js';
test('joints begin black; only the reported motor turns red; missing telemetry preserves it',()=>{
 const colors=new JointIssueColors(); assert.equal(colors.states.size,0);
 colors.update([{name:'elbow',motion_issue:{active:true,kind:'stall',state:'stall',reason:'TRACKING_STALLED'}}]);
 assert.equal(colors.states.get('elbow').kind,'stall');assert.equal(colors.states.has('wrist'),false);
 for(const rows of [[],[{name:'elbow'}],[{name:'elbow',source_state:'stale',motion_issue:{state:'unknown',clear_kinds:[],feedback_fresh:false}}]]){colors.update(rows);assert.equal(colors.states.has('elbow'),true);}
 colors.update([{name:'elbow',motion_issue:{state:'clear',feedback_fresh:true,clear_kinds:['stall']}}]);assert.equal(colors.states.size,0);
});
test('a stall-only recovery cannot erase a hardware fault; stale clear cannot erase either',()=>{
 const colors=new JointIssueColors();colors.update([{name:'wrist',motion_issue:{active:true,kind:'fault',state:'fault'}}]);
 colors.update([{name:'wrist',motion_issue:{state:'clear',feedback_fresh:true,clear_kinds:['stall']}}]);assert.ok(colors.states.has('wrist'));
 colors.update([{name:'wrist',motion_issue:{state:'clear',feedback_fresh:false,clear_kinds:['fault','stall']}}]);assert.ok(colors.states.has('wrist'));
 colors.update([{name:'wrist',motion_issue:{state:'clear',feedback_fresh:true,clear_kinds:['fault','stall']}}]);assert.equal(colors.states.size,0);
});
