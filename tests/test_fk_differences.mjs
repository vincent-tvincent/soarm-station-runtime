import test from 'node:test';
import assert from 'node:assert/strict';
import {FKView, JointIssueColors, jointPositionDifferences} from '../web/fk-view.js';

const joint = (name, position_m) => ({name, position_m});
const pose = joints => ({frame_id:'arm_base', units:{position:'m'}, joints});

test('position differences pair unique joint names rather than array indices and use physical 3D millimeters', () => {
  const measured = pose([joint('elbow',[0,0,0]), joint('wrist',[.1,0,0])]);
  const target = pose([joint('wrist',[.1,.012,0]), joint('elbow',[.003,0,.004])]);
  const before = structuredClone({measured,target});
  const differences = jointPositionDifferences(measured,target);
  assert.deepEqual(differences.map(d => [d.name,d.distance_mm]), [['elbow',5],['wrist',12]]);
  assert.deepEqual(differences[0].target,[.003,0,.004]);
  differences[0].target[0]=99;
  assert.deepEqual({measured,target},before);
});

test('missing or different frames and explicitly non-meter geometry cannot produce misleading connectors', () => {
  const measured = pose([joint('elbow',[0,0,0])]);
  const target = pose([joint('elbow',[1,0,0])]);
  for (const frame_id of [undefined,null,'','another_arm_base']) {
    assert.deepEqual(jointPositionDifferences(measured,{...target,frame_id}),[]);
  }
  assert.deepEqual(jointPositionDifferences({...measured,frame_id:undefined},{...target,frame_id:undefined}),[]);
  assert.deepEqual(jointPositionDifferences(measured,{...target,units:{position:'mm'}}),[]);
  assert.deepEqual(jointPositionDifferences(null,target),[]);
  assert.deepEqual(jointPositionDifferences(measured,null),[]);
});

test('coincident, missing, ambiguous and nonfinite joint positions are omitted without a distance limit', () => {
  const measured = pose([
    joint('same',[0,0,0]), joint('missing',[0,0,0]), joint('duplicate',[0,0,0]),
    joint('bad',[0,0,0]), joint('tiny',[0,0,0]), joint('far',[0,0,0]),
  ]);
  const target = pose([
    joint('same',[0,0,0]), joint('duplicate',[1,0,0]), joint('duplicate',[2,0,0]),
    joint('bad',[0,NaN,0]), joint('tiny',[1e-12,0,0]), joint('far',[100,0,0]),
  ]);
  const differences = jointPositionDifferences(measured,target);
  assert.deepEqual(differences.map(d=>d.name),['tiny','far']);
  assert.equal(differences[0].distance_mm,1e-9);
  assert.equal(differences[1].distance_mm,100000);
  for (const invalid of [null,[],[1,2],[1,2,3,4],[Infinity,0,0],['1',0,0]]) {
    assert.deepEqual(jointPositionDifferences(pose([joint('a',invalid)]),pose([joint('a',[0,0,0])])),[]);
  }
  assert.deepEqual(jointPositionDifferences(pose([joint('a',[0,0,0]),joint('a',[0,0,0])]),pose([joint('a',[1,0,0])])),[]);
});

class Element {
  constructor(tag='div') { this.tag=tag; this.attributes={}; this.children=[]; }
  setAttribute(name,value) { this.attributes[name]=String(value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
}
function documentFixture(t) {
  const previous=Object.getOwnPropertyDescriptor(globalThis,'document');
  Object.defineProperty(globalThis,'document',{configurable:true,value:{createElementNS:(_ns,tag)=>new Element(tag)}});
  t.after(()=>previous?Object.defineProperty(globalThis,'document',previous):delete globalThis.document);
}
function view(measured,target) {
  return Object.assign(Object.create(FKView.prototype),{
    geometry:measured,target,yaw:-.68,pitch:.42,zoom:1,svg:new Element('svg'),
  });
}

test('connector tooltips keep the 3D distance when orbit changes the displayed separation', t => {
  documentFixture(t);
  const owner=view(pose([joint('elbow',[0,0,0])]),pose([joint('elbow',[.003,0,.004])]));
  owner.drawPositionDifferences();
  const first=owner.svg.children[0];
  assert.equal(first.attributes['data-distance-mm'],'5');
  assert.equal(first.children[0].textContent,'elbow · Position difference 5.00 mm (3D)');
  assert.equal(first.attributes.stroke,'#b45b54');
  assert.equal(first.attributes['stroke-dasharray'],'3 4');
  owner.yaw=.9; owner.pitch=-.7; owner.zoom=2;
  owner.svg.replaceChildren(); owner.drawPositionDifferences();
  const second=owner.svg.children[0];
  assert.notEqual(second.attributes.x2,first.attributes.x2);
  assert.equal(second.attributes['data-distance-mm'],first.attributes['data-distance-mm']);
  assert.equal(second.children[0].textContent,first.children[0].textContent);
});

test('difference lines render behind existing measured, target and fault layers', t => {
  documentFixture(t);
  const geometry = p => ({...pose([joint('elbow',p)]),position_m:p,tcp:{position_m:p},
    rotation_matrix:[[1,0,0],[0,1,0],[0,0,1]],segments:[{from:'base',to:'elbow',start_m:[0,0,0],end_m:p}]});
  const owner=view(geometry([.05,.03,.08]),geometry([.055,.04,.09]));
  const elements=new Map();
  owner.element={querySelector(selector){if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector);}};
  owner.issues=new JointIssueColors();
  owner.issues.update([{name:'elbow',motion_issue:{active:true,kind:'fault',state:'fault'}}]);
  owner.draw();
  const marks=owner.svg.children;
  const difference=marks.findIndex(n=>n.attributes.class==='fk-position-difference');
  const segment=marks.findIndex(n=>n.attributes['data-segment-to']==='elbow');
  assert.ok(difference>=0 && difference<segment);
  const segments=marks.filter(n=>n.attributes['data-segment-to']==='elbow');
  assert.deepEqual(segments.map(n=>[n.attributes.stroke,n.attributes['stroke-dasharray']]),[['#a5a5a0','5 5'],['#191919','']]);
  const circles=marks.filter(n=>n.tag==='circle');
  assert.deepEqual(circles.map(n=>n.attributes.fill),['#a5a5a0','#d9231b']);
  assert.equal(circles[1].attributes['data-issue'],'fault');
});
