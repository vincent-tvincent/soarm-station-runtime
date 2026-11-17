import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

// Load the actual dependency-free presentation exports without starting the
// dashboard's connection, cameras, controls, or polling bootstrap.
const source=await readFile(new URL('../web/app.js',import.meta.url),'utf8');
const declarations=['readinessPresentation','readinessErrorSummary','renderReadinessSection'].map(name=>{
  const declaration=source.match(new RegExp(`export function ${name}\\([\\s\\S]*?\\n\\}`));
  assert.ok(declaration,`Missing presentation export ${name}`);return declaration[0];
});
const {readinessPresentation,readinessErrorSummary,renderReadinessSection}=await import(
  'data:text/javascript;base64,'+Buffer.from(declarations.join('\n')).toString('base64'));
const health={value:'None',active:false,detail:'No current errors reported by the runtime or motors'};
const component=(id,state,required=true,detail='Reported detail')=>({id,label:id,state,required,detail});

test('ready required components and an optional offline leader do not create a global fault',()=>{
  const input={state:'ready',components:[component('controller:arm1','ready'),component('camera:wrist','ready'),
    component('telemetry','ready'),component('input:mac','offline',false,'Leader host is not connected.')]};
  const before=structuredClone(input),view=readinessPresentation(input);
  assert.equal(view.state,'ready');assert.equal(view.summary,'Ready');assert.deepEqual(view.problems,[]);
  assert.equal(view.components.at(-1).stateLabel,'Offline');assert.equal(view.components.at(-1).required,false);
  assert.deepEqual(readinessErrorSummary(health,view),health);assert.deepEqual(input,before);
});

test('a required camera fault overrides an optimistic aggregate and appears in the Errors summary',()=>{
  const view=readinessPresentation({state:'ready',components:[component('controller:arm1','ready'),
    component('camera:global','fault',true,'Capture device could not be opened.')]});
  const errors=readinessErrorSummary(health,view);
  assert.equal(view.state,'degraded');assert.equal(errors.value,'1 not ready');assert.equal(errors.active,true);
  assert.match(errors.detail,/camera:global: Fault/);assert.match(errors.detail,/Capture device could not be opened/);
  assert.doesNotMatch(errors.detail,/No current errors/);
});

test('required starting and waiting components are visible without being relabeled as hardware faults',()=>{
  const view=readinessPresentation({state:'starting',components:[
    component('controller:arm1','waiting',true,'Waiting for first joint feedback.'),
    component('telemetry','starting',true,'Opening the history database.'),
    component('input:mac','offline',false,'Optional leader host offline.'),
  ]});
  const errors=readinessErrorSummary(health,view);
  assert.equal(view.state,'starting');assert.equal(errors.value,'2 not ready');
  assert.match(errors.detail,/Waiting for first joint feedback/);assert.match(errors.detail,/Opening the history database/);
  assert.doesNotMatch(errors.detail,/Optional leader|Fault/);
});

test('required offline components and existing motor issues both contribute to the summary',()=>{
  const view=readinessPresentation({state:'degraded',components:[component('telemetry','offline',true,'No telemetry status received.')]});
  const errors=readinessErrorSummary({value:'2 issues',active:true,detail:'Elbow: tracking stalled\nWrist: feedback error'},view);
  assert.equal(errors.value,'3 issues');assert.match(errors.detail,/tracking stalled/);
  assert.match(errors.detail,/feedback error/);assert.match(errors.detail,/No telemetry status received/);
});

test('waiting during a degraded runtime is not mislabeled as a new startup',()=>{
  const view=readinessPresentation({state:'degraded',components:[
    component('camera:wrist','waiting',true,'Capture is live; waiting for image delivery to dashboard')]});
  assert.equal(view.state,'degraded');assert.equal(view.summary,'Needs attention');
  assert.equal(view.components[0].stateLabel,'Waiting');
});

test('required faults and waiting components precede ready and optional rows',()=>{
  const view=readinessPresentation({state:'degraded',components:[component('input:mac','offline',false),
    component('controller:arm1','ready'),component('telemetry','waiting'),component('camera:wrist','fault')]});
  assert.deepEqual(view.components.map(row=>row.id),['camera:wrist','telemetry','controller:arm1','input:mac']);
});

test('missing, empty, or malformed readiness never turns an incomplete status into Errors None',()=>{
  for(const input of [undefined,null,{}, {state:'ready',components:[]},{state:'ready',components:'invalid'}]) {
    const view=readinessPresentation(input),errors=readinessErrorSummary(health,view);
    assert.equal(view.state,'unknown');assert.equal(errors.value,'Unknown');assert.equal(errors.active,false);
  }
  const unknown=readinessPresentation({state:'ready',components:[component('camera:wrist','unrecognized')]});
  assert.equal(unknown.components[0].stateLabel,'Unknown');assert.notEqual(readinessErrorSummary(health,unknown).value,'None');
});

test('an aggregate starting or degraded report without component detail remains explicit',()=>{
  for(const state of ['starting','degraded']) {
    const view=readinessPresentation({state,components:[]}),errors=readinessErrorSummary(health,view);
    assert.equal(errors.value,state==='starting'?'Starting':'Degraded');
    assert.equal(errors.active,state==='degraded');assert.ok(errors.detail.length);
  }
});

test('disconnection and delayed status preserve reported detail but do not present it as current readiness',()=>{
  const input={state:'ready',components:[component('controller:arm1','ready',true,'Feedback received.') ]};
  for(const [options,state,value] of [[{connected:false},'offline','Offline'],[{delayed:true},'stale','Stale']]) {
    const view=readinessPresentation(input,options);
    assert.equal(view.state,state);assert.equal(view.components[0].lastReported,true);
    assert.equal(view.components[0].detail,'Feedback received.');assert.equal(readinessErrorSummary(health,view).value,value);
  }
  const view=readinessPresentation({state:'degraded',components:[component('camera:wrist','fault')]},{connected:false});
  assert.match(readinessErrorSummary(health,view).detail,/Last reported · camera:wrist/);
});

class Element {
  constructor(tag,document){this.tag=tag;this.ownerDocument=document;this.children=[];this.dataset={};this.attributes={};this.parentElement=null;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  set innerHTML(_){throw Error('Readiness detail must be inserted as text, not HTML');}
  get nextSibling(){const siblings=this.parentElement?.children||[];return siblings[siblings.indexOf(this)+1]||null;}
  append(...children){for(const child of children){child.remove();child.parentElement=this;this.children.push(child);}}
  insertBefore(child,reference){if(!reference){this.append(child);return;}child.remove();child.parentElement=this;this.children.splice(this.children.indexOf(reference),0,child);}
  remove(){if(this.parentElement){const siblings=this.parentElement.children;siblings.splice(siblings.indexOf(this),1);this.parentElement=null;}}
  querySelector(selector){
    const match=element=>selector.startsWith('#')?element.id===selector.slice(1):element.className?.split(' ').includes(selector.slice(1));
    for(const child of this.children){if(match(child))return child;const nested=child.querySelector(selector);if(nested)return nested;}
    return null;
  }
}
function panel(){
  const document={createElement:tag=>new Element(tag,document)},root=document.createElement('section');
  const summary=document.createElement('div');summary.className='status-summary';
  const history=document.createElement('div');history.id='history';root.append(summary,history);
  return root;
}

test('the readiness section stays inside Status after operation/policy and before history, rendering details as text',()=>{
  const root=panel(),detail='<img src=x onerror=alert(1)> device not available';
  const view=readinessPresentation({state:'degraded',components:[component('camera:wrist','fault',true,detail),
    component('input:mac','offline',false,'No leader connected.')]});
  renderReadinessSection(root,view);
  assert.deepEqual(root.children.map(child=>child.id||child.className),['status-summary','component-readiness','history']);
  const section=root.querySelector('#component-readiness'),{rows}=section.readinessElements;
  assert.equal(section.attributes['aria-labelledby'],'component-readiness-heading');
  assert.equal(rows.get('camera:wrist').detail.textContent,detail);
  assert.equal(rows.get('camera:wrist').optional.hidden,true);assert.equal(rows.get('input:mac').optional.hidden,false);
  assert.equal(rows.get('input:mac').state.textContent,'Offline');
});

test('refreshes reuse component rows, remove retired resources, and mark last reported states',()=>{
  const root=panel();renderReadinessSection(root,readinessPresentation({state:'degraded',components:[
    component('camera:wrist','fault'),component('input:mac','offline',false)]}));
  const section=root.querySelector('#component-readiness'),row=section.readinessElements.rows.get('camera:wrist').row;
  renderReadinessSection(root,readinessPresentation({state:'ready',components:[component('camera:wrist','ready')]},{delayed:true}));
  const elements=section.readinessElements;
  assert.equal(root.children.length,3);assert.equal(elements.rows.size,1);
  assert.equal(elements.rows.get('camera:wrist').row,row);assert.equal(elements.rows.get('camera:wrist').state.textContent,'Last: Ready');
  assert.equal(elements.summary.textContent,'Status delayed');
});
