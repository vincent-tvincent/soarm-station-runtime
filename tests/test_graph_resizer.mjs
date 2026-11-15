import test from 'node:test';
import assert from 'node:assert/strict';
import {GRAPH_INTERFACE_PANE, interfacePaneHeight, GraphInterfaceResizer, GraphExplorer} from '../web/graph.js';

class Element {
  constructor(height=0,id='') {
    this.id=id;this.height=height;this.clientHeight=height;this.children=[];
    this.attributes=new Map();this.listeners=new Map();this.properties=new Map();this.captured=new Set();
    this.style={setProperty:(name,value)=>this.properties.set(name,value)};
    const classes=new Set();this.classList={add:name=>classes.add(name),remove:name=>classes.delete(name),contains:name=>classes.has(name)};
  }
  getBoundingClientRect(){return{height:this.height};}
  setAttribute(name,value){this.attributes.set(name,String(value));}
  addEventListener(name,fn){this.listeners.set(name,fn);}
  removeEventListener(name,fn){if(this.listeners.get(name)===fn)this.listeners.delete(name);}
  setPointerCapture(id){this.captured.add(id);}
  releasePointerCapture(id){this.captured.delete(id);}
  focus(){this.focused=true;}
  emit(type,values={}) {
    const event={button:0,isPrimary:true,pointerId:1,clientY:500,
      preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;},...values};
    this.listeners.get(type)?.(event);return event;
  }
}
class Observer {
  constructor(callback){this.callback=callback;this.elements=[];}
  observe(element){this.elements.push(element);}
  disconnect(){this.disconnected=true;}
}
function setup({height=600,saved,storage}={}) {
  const main=new Element(height,'graph-main'),canvas=new Element(300,'graph-canvas');
  const pane=new Element(152,'graph-interface-pane'),handle=new Element(12,'graph-interface-resizer');
  main.children=[new Element(40),canvas,new Element(48),handle,pane];
  const records=new Map(saved===undefined?[]:[[GRAPH_INTERFACE_PANE.storageKey,saved]]),writes=[];
  storage??={getItem:key=>records.get(key)??null,setItem:(key,value)=>{records.set(key,value);writes.push([key,value]);},removeItem:key=>records.delete(key)};
  const resizer=new GraphInterfaceResizer({main,canvas,pane,handle,storage,ResizeObserverClass:Observer});
  return{resizer,main,canvas,pane,handle,records,writes};
}

test('pane limits retain graph space and handle invalid saved values and very short workspaces',()=>{
  assert.deepEqual(interfacePaneHeight(null,600),{min:100,max:420,height:152});
  assert.deepEqual(interfacePaneHeight(900,600),{min:100,max:420,height:420});
  assert.deepEqual(interfacePaneHeight(20,600),{min:100,max:420,height:100});
  assert.deepEqual(interfacePaneHeight(300,300),{min:100,max:180,height:180});
  assert.deepEqual(interfacePaneHeight(300,140),{min:20,max:20,height:20});
  for(const value of ['invalid','',-30,Infinity,NaN])assert.equal(interfacePaneHeight(value,600).height,152);
  for(const available of [NaN,Infinity,-5,0])assert.deepEqual(interfacePaneHeight(200,available),{min:0,max:0,height:0});
});

test('upward dragging grows the list, clamps its limits, and saves only on release',()=>{
  const{resizer,main,handle,writes}=setup();
  const down=handle.emit('pointerdown');
  assert.equal(down.prevented,true);assert.equal(handle.focused,true);assert.ok(handle.captured.has(1));
  assert.equal(main.classList.contains('is-resizing-interfaces'),true);
  handle.emit('pointermove',{clientY:400});assert.equal(resizer.size.height,252);
  assert.equal(main.properties.get('--graph-interface-height'),'252px');
  handle.emit('pointermove',{clientY:-1000});assert.equal(resizer.size.height,350);
  handle.emit('pointermove',{clientY:1000});assert.equal(resizer.size.height,100);
  assert.equal(writes.length,0);
  handle.emit('pointerup');
  assert.deepEqual(writes,[[GRAPH_INTERFACE_PANE.storageKey,'100']]);
  assert.equal(resizer.drag,null);assert.equal(handle.captured.size,0);
  assert.equal(main.classList.contains('is-resizing-interfaces'),false);
});

test('unrelated pointers cannot resize or finish a drag and cancellation restores the saved preference',()=>{
  for(const cancellation of ['pointercancel','lostpointercapture']) {
    const{resizer,handle,writes}=setup({saved:'300'});
    handle.emit('pointerdown',{button:2});assert.equal(resizer.drag,null);
    handle.emit('pointerdown',{isPrimary:false});assert.equal(resizer.drag,null);
    handle.emit('pointerdown');handle.emit('pointermove',{pointerId:7,clientY:1000});
    assert.equal(resizer.size.height,300);
    handle.emit('pointerup',{pointerId:7});assert.ok(resizer.drag);
    handle.emit('pointermove',{clientY:450});assert.equal(resizer.size.height,350);
    handle.emit(cancellation);assert.equal(resizer.size.height,300);
    assert.equal(resizer.preferredHeight,300);assert.deepEqual(writes,[]);
  }
});

test('keyboard separator controls update accessible values and Home clears the preference',()=>{
  const{resizer,handle,records}=setup();
  assert.equal(handle.attributes.get('role'),'separator');
  assert.equal(handle.attributes.get('aria-orientation'),'horizontal');
  assert.equal(handle.attributes.get('aria-controls'),'graph-interface-pane');
  assert.equal(handle.attributes.get('tabindex'),'0');
  assert.equal(handle.attributes.get('aria-valuemin'),'100');
  assert.equal(handle.attributes.get('aria-valuemax'),'350');
  assert.ok(handle.attributes.get('title').includes('Home'));
  assert.equal(handle.emit('keydown',{key:'ArrowUp'}).prevented,true);
  assert.equal(resizer.size.height,172);
  handle.emit('keydown',{key:'ArrowDown'});assert.equal(resizer.size.height,152);
  handle.emit('keydown',{key:'ArrowUp',shiftKey:true});assert.equal(resizer.size.height,212);
  handle.emit('keydown',{key:'End'});assert.equal(resizer.size.height,350);
  assert.equal(handle.attributes.get('aria-valuenow'),'350');
  assert.equal(handle.attributes.get('aria-valuetext'),'350 pixels');
  assert.equal(handle.emit('keydown',{key:'ArrowDown',ctrlKey:true}).prevented,undefined);
  assert.equal(resizer.size.height,350);
  handle.emit('keydown',{key:'Home'});assert.equal(resizer.size.height,152);
  assert.equal(records.has(GRAPH_INTERFACE_PANE.storageKey),false);
});

test('responsive clamping and hidden pages do not overwrite the preferred height',()=>{
  const{resizer,main,handle,writes}=setup({height:700,saved:'320'});
  assert.equal(resizer.size.height,320);
  main.clientHeight=350;resizer.observer.callback();
  assert.equal(resizer.size.height,130);assert.equal(resizer.preferredHeight,320);
  assert.equal(handle.attributes.get('aria-valuemax'),'130');
  main.clientHeight=0;resizer.observer.callback();assert.equal(resizer.size.height,130);
  main.clientHeight=700;resizer.observer.callback();assert.equal(resizer.size.height,320);
  assert.deepEqual(writes,[]);
});

test('unavailable browser storage does not disable resizing and disposal removes active handlers',()=>{
  const failing=()=>{throw new Error('Storage unavailable');};
  const{resizer,main,handle}=setup({storage:{getItem:failing,setItem:failing,removeItem:failing}});
  handle.emit('keydown',{key:'ArrowUp'});assert.equal(resizer.size.height,172);
  handle.emit('keydown',{key:'Home'});assert.equal(resizer.size.height,152);
  handle.emit('pointerdown');handle.emit('pointermove',{clientY:450});
  resizer.dispose();assert.equal(resizer.size.height,152);
  assert.equal(resizer.observer.disconnected,true);assert.equal(handle.listeners.size,0);
  assert.equal(handle.captured.size,0);assert.equal(main.classList.contains('is-resizing-interfaces'),false);
  main.clientHeight=1000;resizer.observer.callback();assert.equal(resizer.size.height,152);
});

test('canvas resizing preserves the same graph center, zoom, and selection without fitting again',()=>{
  const owner={host:{clientWidth:900,clientHeight:400},canvasSize:{width:900,height:600},
    transform:{x:40,y:80,k:1.2},focus:'selected-topic',didFit:true,data:{},applied:0,
    fit(){throw new Error('Pane resizing must not refit the selected graph');},applyTransform(){this.applied++;}};
  const center=[(900/2-40)/1.2,(600/2-80)/1.2];
  GraphExplorer.prototype.resizeCanvas.call(owner);
  assert.deepEqual(owner.transform,{x:40,y:-20,k:1.2});
  assert.deepEqual([(900/2-owner.transform.x)/1.2,(400/2-owner.transform.y)/1.2],center);
  assert.equal(owner.focus,'selected-topic');assert.equal(owner.didFit,true);assert.equal(owner.applied,1);
  owner.host.clientHeight=0;GraphExplorer.prototype.resizeCanvas.call(owner);
  assert.deepEqual(owner.canvasSize,{width:900,height:400});assert.equal(owner.applied,1);
  owner.host.clientHeight=450;GraphExplorer.prototype.resizeCanvas.call(owner);
  assert.equal(owner.transform.y,5);assert.equal(owner.transform.k,1.2);
});

test('initial fit records the current canvas size so its resize callback does not shift it twice',()=>{
  const owner={host:{clientWidth:900,clientHeight:400},canvasSize:{width:900,height:600},
    bounds:{width:1200,height:800},didFit:false,data:{},applied:0,applyTransform(){this.applied++;}};
  owner.fit=()=>GraphExplorer.prototype.fit.call(owner);
  GraphExplorer.prototype.resizeCanvas.call(owner);
  assert.equal(owner.didFit,true);assert.equal(owner.applied,1);
  const transform={...owner.transform};
  GraphExplorer.prototype.resizeCanvas.call(owner);
  assert.deepEqual(owner.transform,transform);assert.equal(owner.applied,1);
});
