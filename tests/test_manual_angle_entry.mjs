import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {ManualPanel,jointSliderDisplayRange} from '../web/manual-panel.js';

const source=await readFile(new URL('../web/app.js',import.meta.url),'utf8');
const declaration=source.match(/export function validateJointPositions\([\s\S]*?\n\}/)[0];
const {validateJointPositions}=await import('data:text/javascript;base64,'+Buffer.from(declaration).toString('base64'));

test('payload body angles outside old configured bounds remain exact while names and finite values are checked',()=>{
  const joints=[{name:'one',min:-.2,max:.2},{name:'two',min_rad:-.3,max_rad:.3}];
  const q={one:12.75,two:-8.125};assert.equal(validateJointPositions(q,joints),q);
  for(const positions of [{one:0},{one:0,two:0,foreign:0},{one:NaN,two:0},{one:Infinity,two:0},
    {one:'2',two:0},{one:true,two:0},[],null])assert.throws(()=>validateJointPositions(positions,joints));
});

test('slider display ranges expand to include finite targets without clamping them',()=>{
  const initial=jointSliderDisplayRange(7);assert.ok(initial.min<7&&initial.max>7);
  const lower=jointSliderDisplayRange(-20,initial);assert.ok(lower.min<-20);assert.equal(lower.max,initial.max);
  assert.deepEqual(jointSliderDisplayRange(0,lower),lower,'the view range does not shrink around a later target');
  for(const value of [NaN,Infinity,-Infinity,'1'])assert.throws(()=>jointSliderDisplayRange(value),/finite/);
});

class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attributes={};this.min='';this.max='';this._value='';}
  set value(value){
    const number=Number(value);
    this._value=this.type==='range'&&Number.isFinite(number)
      ?String(Math.max(Number(this.min||0),Math.min(Number(this.max||100),number))):String(value);
  }
  get value(){return this._value;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(name,value){this.attributes[name]=String(value);}
}
function sliders(t,position=7) {
  const previous=Object.getOwnPropertyDescriptor(globalThis,'document');
  Object.defineProperty(globalThis,'document',{configurable:true,value:{createElement:tag=>new Element(tag)}});
  t.after(()=>{if(previous)Object.defineProperty(globalThis,'document',previous);else delete globalThis.document;});
  const box=new Element('div'),sent=[];
  const owner={target:{positions:{joint:position},gripper:40},config:{joints:[{name:'joint',min:-.1,max:.1}],gripper:{name:'gripper'}},
    el:()=>box,mode:'joint',isOwnStream:()=>true,client:{setManualTarget:value=>sent.push(structuredClone(value))}};
  ManualPanel.prototype.renderSliders.call(owner);
  const fields=row=>({number:row.children[0].children[1],slider:row.children[1],ends:row.children[2]});
  return {owner,sent,body:fields(box.children[0]),gripper:fields(box.children[1])};
}

test('manual numeric inputs have no body-angle bounds and expand the HTML range before assigning a typed target',t=>{
  const {owner,sent,body}=sliders(t);
  assert.equal(body.number.min,'');assert.equal(body.number.max,'');assert.equal(Number(body.slider.value),7);
  body.number.value='19.5';body.number.oninput();
  assert.equal(owner.target.positions.joint,19.5);assert.equal(Number(body.slider.value),19.5);
  assert.ok(Number(body.slider.max)>19.5);assert.equal(sent.at(-1).positions.joint,19.5);
  body.number.value='-23.25';body.number.oninput();
  assert.equal(owner.target.positions.joint,-23.25);assert.equal(Number(body.slider.value),-23.25);
  assert.ok(Number(body.slider.min)<-23.25);
  assert.equal(body.ends.title,'Slider display range');
});

test('blank/nonfinite angle edits and invalid gripper percentages do not become new targets',t=>{
  const {owner,sent,body,gripper}=sliders(t,0);
  for(const value of ['', ' ', 'Infinity','not a number']) {body.number.value=value;body.number.oninput();}
  assert.equal(sent.length,0);assert.equal(owner.target.positions.joint,0);
  assert.equal(gripper.number.min,'0');assert.equal(gripper.number.max,'100');
  for(const value of ['-1','101']) {gripper.number.value=value;gripper.number.oninput();}
  assert.equal(sent.length,0);assert.equal(owner.target.gripper,40);
  gripper.number.value='100';gripper.number.oninput();assert.equal(owner.target.gripper,100);
});
