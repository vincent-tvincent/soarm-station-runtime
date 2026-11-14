import test from 'node:test';
import assert from 'node:assert/strict';
import { FKView } from '../web/fk-view.js';
const near=(a,b)=>assert.ok(Math.abs(a-b)<1e-10,`${a} != ${b}`);
const dot=(a,b)=>a.reduce((sum,x,i)=>sum+x*b[i],0);
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
function basis(yaw,pitch){
 const project=p=>FKView.prototype.project.call({yaw,pitch},p);
 const o=project([0,0,0]),v=[[1,0,0],[0,1,0],[0,0,1]].map(project);
 return {project,right:v.map(p=>(p.x-o.x)/650),up:v.map(p=>-(p.y-o.y)/650),back:v.map(p=>p.depth-o.depth)};
}
test('orbit uses a right-handed orthonormal camera frame, including painter depth',()=>{
 for(const yaw of [-Math.PI,-1.2,-.68,0,.7,Math.PI])for(const pitch of [-1.,0,.42,1.]){
  const {right,up,back}=basis(yaw,pitch);
  for(const axis of [right,up,back])near(dot(axis,axis),1);
  near(dot(right,up),0);near(dot(right,back),0);near(dot(up,back),0);
  cross(right,up).forEach((x,i)=>near(x,back[i]));
 }
});
test('positive elevation looks down from above and painter depth includes height',()=>{
 const {project,back}=basis(-.68,.42);
 assert.ok(back[2]>0);
 near(project(back).depth,1);
 assert.ok(project([0,0,.2]).depth>project([0,0,0]).depth);
});
test('jaw opening in the independently checked measured pose projects to the right',()=>{
 // FK displacement from 0 to 100 percent at the inspected arm pose; its base Y
 // component is negative (right of the arm). Do not negate the hardware input.
 const opening=[-.07010665298798521,-.0737071775637233,.03150215749636414];
 const {project}=basis(-.68,.42);
 assert.ok(project(opening).x>project([0,0,0]).x);
});
