import test from 'node:test';
import assert from 'node:assert/strict';
import {
  FLOW_LIMITS,
  TopicFlowActivity,
  cubicPoint,
  flowEdges,
  normalizeGraph,
  GraphExplorer,
} from '../web/graph.js';

const node=id=>({id,name:id,namespace:'/',fully_qualified_name:`/${id}`,instance_count:1});
const traffic=(messages=0,extra={})=>({
  state:'sampling',sample_id:'sample-a',messages,last_message_age_ms:0,rate_hz:20,
  ...extra,
});
function graph(messages=0,{sample={},snapshot={},extraTopics=[],extraEdges=[]}={}){
  return normalizeGraph({
    schema_version:1,state:'ready',age_ms:0,
    nodes:[node('publisher'),node('subscriber'),node('other-publisher')],
    topics:[{id:'topic',name:'/data',types:['std_msgs/msg/Float32'],traffic:traffic(messages,sample)},...extraTopics],
    services:[{id:'service',name:'/inspect',types:['example_interfaces/srv/Trigger']}],
    actions:[{id:'action',name:'/execute',types:['example_interfaces/action/Fibonacci']}],
    edges:[
      {id:'pub',kind:'publish',from:'publisher',to:'topic',endpoint_id:'publisher-gid'},
      {id:'sub',kind:'subscribe',from:'topic',to:'subscriber',endpoint_id:'subscriber-gid'},
      {id:'service-client',kind:'service_client',from:'subscriber',to:'service'},
      {id:'service-server',kind:'service_server',from:'service',to:'publisher'},
      {id:'action-client',kind:'action_client',from:'subscriber',to:'action'},
      {id:'action-server',kind:'action_server',from:'action',to:'publisher'},
      ...extraEdges,
    ],
    ...snapshot,
  });
}
function pulsing(activity=new TopicFlowActivity(),now=1000,options={}){
  activity.observe(graph(10,options),now-10);
  const current=graph(11,options);
  assert.equal(activity.observe(current,now),true);
  return{activity,current,now};
}

test('flow work has explicit fixed caps for edges, frame rate, pulse and freshness',()=>{
  assert.deepEqual(FLOW_LIMITS,{maxEdges:64,maxFps:30,pulseMs:1400,staleMs:4500});
});

test('first positive sample only primes; a later count advance triggers a finite pulse',()=>{
  const activity=new TopicFlowActivity();
  assert.equal(activity.observe(graph(120),100),false);
  assert.equal(activity.active('topic',100),false);
  assert.equal(activity.observe(graph(121),200),true);
  assert.equal(activity.active('topic',200),true);
  assert.equal(activity.active('topic',200+FLOW_LIMITS.pulseMs-1),true);
  assert.equal(activity.active('topic',200+FLOW_LIMITS.pulseMs+1),false);
});

test('unchanged counts and reported rates never renew a pulse',()=>{
  const{activity,now}=pulsing();
  assert.equal(activity.observe(graph(11,{sample:{rate_hz:999,last_message_age_ms:0}}),now+1100),false);
  assert.equal(activity.active('topic',now+FLOW_LIMITS.pulseMs+1),false);
  assert.equal(activity.observe(graph(11),now+5000),false);
  assert.equal(activity.active('topic',now+5000),false);
});

test('a different sample ID primes again even if its historical count is much larger',()=>{
  const{activity,now}=pulsing();
  assert.equal(activity.observe(graph(900,{sample:{sample_id:'sample-b'}}),now+10),false);
  assert.equal(activity.active('topic',now+10),false);
  assert.equal(activity.observe(graph(901,{sample:{sample_id:'sample-b'}}),now+20),true);
  assert.equal(activity.active('topic',now+20),true);
});

test('zero and same-sample counter resets stop old motion before later increments',()=>{
  for(const resetCount of [0,3]){
    const{activity,now}=pulsing();
    assert.equal(activity.observe(graph(resetCount),now+10),false);
    assert.equal(activity.active('topic',now+10),false);
    assert.equal(activity.observe(graph(resetCount+1),now+20),true);
    assert.equal(activity.active('topic',now+20),true);
  }
});

test('completed, stopped, blocked and unavailable samples stop even when their final count increased',()=>{
  for(const state of ['complete','stopped','blocked','unavailable','not_sampled']){
    const{activity,now}=pulsing();
    const final=graph(200,{sample:{state,rate_hz:100}});
    assert.equal(activity.observe(final,now+10),false,state);
    assert.equal(activity.active('topic',now+10),false,state);
    assert.deepEqual(flowEdges(final,final.edges,activity,now+10,{}),[],state);
  }
});

test('snapshot and message freshness require known finite nonnegative ages',()=>{
  for(const age_ms of [null,undefined,-1,NaN,Infinity,4501]){
    const{activity,now}=pulsing();
    activity.observe(graph(12,{snapshot:{age_ms}}),now+10);
    assert.equal(activity.active('topic',now+10),false,`graph age ${age_ms}`);
  }
  for(const last_message_age_ms of [null,undefined,-1,NaN,Infinity,2501]){
    const{activity,now}=pulsing();
    activity.observe(graph(12,{sample:{last_message_age_ms}}),now+10);
    assert.equal(activity.active('topic',now+10),false,`message age ${last_message_age_ms}`);
  }
});

test('stale, initializing and unknown graph states cannot animate; fresh partial discovery can',()=>{
  for(const state of ['stale','initializing','unknown']){
    const{activity,now}=pulsing();
    activity.observe(graph(12,{snapshot:{state,age_ms:0}}),now+10);
    assert.equal(activity.active('topic',now+10),false,state);
  }
  const{activity,now}=pulsing(new TopicFlowActivity(),1000,{snapshot:{state:'partial'}});
  assert.equal(activity.active('topic',now),true);
});

test('freshness expires using local elapsed time even without another network response',()=>{
  for(const options of [{snapshot:{age_ms:4400}},{sample:{last_message_age_ms:2400}}]){
    const{activity,now}=pulsing(new TopicFlowActivity(),1000,options);
    assert.equal(activity.active('topic',now+50),true);
    assert.equal(activity.active('topic',now+101),false);
  }
});

test('invalid counters or absent sample identities never produce flow',()=>{
  for(const messages of [null,undefined,-1,1.5,NaN,Infinity]){
    const{activity,now}=pulsing();
    assert.equal(activity.observe(graph(11,{sample:{messages}}),now+10),false);
    assert.equal(activity.active('topic',now+10),false);
  }
  for(const sample_id of [null,undefined,'']){
    const{activity,now}=pulsing();
    assert.equal(activity.observe(graph(12,{sample:{sample_id}}),now+10),false);
    assert.equal(activity.active('topic',now+10),false);
  }
});

test('an old batch observed while stale is not replayed when freshness returns',()=>{
  const{activity,now}=pulsing();
  activity.observe(graph(20,{snapshot:{state:'stale',age_ms:9000}}),now+10);
  assert.equal(activity.active('topic',now+10),false);
  assert.equal(activity.observe(graph(20),now+20),false);
  assert.equal(activity.active('topic',now+20),false);
  assert.equal(activity.observe(graph(21),now+30),true);
});

test('removed topics and clear retire all previous pulse state',()=>{
  const{activity,now}=pulsing();
  activity.observe(normalizeGraph({schema_version:1,state:'ready',age_ms:0,nodes:[],topics:[],services:[],actions:[],edges:[]}),now+10);
  assert.equal(activity.active('topic',now+10),false);
  assert.equal(activity.observe(graph(40),now+20),false);
  activity.observe(graph(41),now+30);assert.equal(activity.active('topic',now+30),true);
  activity.clear();assert.equal(activity.active('topic',now+31),false);
  assert.equal(activity.observe(graph(100),now+40),false);
});

test('only live topic publisher/subscriber routes animate and returned decorations do not mutate discovery',()=>{
  const{activity,current,now}=pulsing(),before=JSON.stringify(current.edges);
  const selected=flowEdges(current,current.edges,activity,now,{});
  assert.deepEqual(selected.map(e=>e.id).sort(),['pub','sub']);
  assert.equal(selected.find(e=>e.id==='pub').topicId,'topic');
  assert.equal(selected.find(e=>e.id==='pub').hollow,false);
  assert.equal(selected.find(e=>e.id==='sub').topicId,'topic');
  assert.equal(selected.find(e=>e.id==='sub').hollow,true);
  assert.equal(JSON.stringify(current.edges),before);
  assert.notEqual(selected[0],current.edges.find(e=>e.id===selected[0].id));
  assert.deepEqual(flowEdges(current,[],activity,now,{}),[]);
});

test('inactive neighboring topics never animate because a focused topic is active',()=>{
  const options={extraTopics:[{id:'quiet',name:'/quiet',types:['std_msgs/msg/String'],traffic:traffic(0,{sample_id:'quiet-sample'})}],
    extraEdges:[{id:'quiet-pub',kind:'publish',from:'publisher',to:'quiet',endpoint_id:'quiet-gid'}]};
  const{activity,current,now}=pulsing(new TopicFlowActivity(),1000,options);
  const selected=flowEdges(current,current.edges,activity,now,{focus:'publisher'});
  assert.equal(selected.some(e=>e.id==='quiet-pub'),false);
});

test('multiple publisher endpoints suppress publication attribution even on one node and after view filtering',()=>{
  const options={extraEdges:[{id:'second-pub',kind:'publish',from:'publisher',to:'topic',endpoint_id:'another-publisher-gid'}]};
  const{activity,current,now}=pulsing(new TopicFlowActivity(),1000,options);
  const visible=current.edges.filter(e=>e.id==='pub'||e.id==='sub');
  const selected=flowEdges(current,visible,activity,now,{focus:'topic'});
  assert.deepEqual(selected.map(e=>e.id),['sub']);
  assert.equal(selected[0].hollow,true);
  assert.equal(flowEdges(current,current.edges,activity,now,{}).some(e=>e.kind==='publish'),false);
});

function largeGraph(messages){
  const topics=Array.from({length:80},(_,i)=>({id:`topic${i}`,name:`/topic${i}`,types:['std_msgs/msg/Float32'],traffic:traffic(messages,{sample_id:`sample${i}`})}));
  return normalizeGraph({schema_version:1,state:'ready',age_ms:0,nodes:[node('publisher'),node('focus-node')],topics,services:[],actions:[],
    edges:[...topics.map((t,i)=>({id:`edge${i}`,kind:'publish',from:'publisher',to:t.id,endpoint_id:`pub${i}`})),
      {id:'focus-sub',kind:'subscribe',from:'topic79',to:'focus-node',endpoint_id:'focus-sub-gid'}]});
}

test('flow edge cap is bounded and topic focus is prioritized before unrelated routes',()=>{
  const activity=new TopicFlowActivity();activity.observe(largeGraph(0),0);const current=largeGraph(1);activity.observe(current,100);
  const visible=[...current.edges],selected=flowEdges(current,visible,activity,100,{focus:'topic79'});
  assert.equal(selected.length,FLOW_LIMITS.maxEdges);
  assert.equal(new Set(selected.map(e=>e.id)).size,selected.length);
  assert.deepEqual(new Set(selected.slice(0,2).map(e=>e.id)),new Set(['edge79','focus-sub']));
  assert.equal(visible.length,81);
});

test('node focus also prioritizes its eligible route at the animation cap',()=>{
  const activity=new TopicFlowActivity();activity.observe(largeGraph(0),0);const current=largeGraph(1);activity.observe(current,100);
  const selected=flowEdges(current,current.edges,activity,100,{focus:'focus-node'});
  assert.equal(selected.length,FLOW_LIMITS.maxEdges);assert.equal(selected[0].id,'focus-sub');
});

test('cubicPoint follows the actual cubic curve and preserves its control points',()=>{
  const points=[[0,0],[0,10],[10,10],[10,0]],before=JSON.stringify(points);
  assert.deepEqual(cubicPoint(points,0),[0,0]);
  assert.deepEqual(cubicPoint(points,1),[10,0]);
  assert.deepEqual(cubicPoint(points,.5),[5,7.5]);
  assert.deepEqual(cubicPoint([[2,3],[4,5],[6,7],[8,9]],.25),[3.5,4.5]);
  assert.equal(JSON.stringify(points),before);
});

const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
function browserDocument(t,{hidden=false}={}){
  const previous=Object.getOwnPropertyDescriptor(globalThis,'document');
  const meta={textContent:''};
  const document={hidden,getElementById:()=>meta};
  Object.defineProperty(globalThis,'document',{value:document,writable:true,configurable:true});
  t.after(()=>{if(previous)Object.defineProperty(globalThis,'document',previous);else delete globalThis.document;});
  return document;
}
function watcher({sample={},snapshot={},extraTopics=[],...overrides}={}){
  const calls=[],owner=Object.create(GraphExplorer.prototype);
  Object.assign(owner,{
    data:graph(0,{sample:{state:'not_sampled',messages:null,last_message_age_ms:null,...sample},
      snapshot:{sampling:{enabled:true,max_topics:2,max_duration_s:10,cleanup_pending:0},...snapshot},extraTopics}),
    focus:'topic',watchTopic:'topic',watchPending:false,watchError:'',visible:true,connected:true,
    flowActivity:new TopicFlowActivity(),flowDots:[],flowFrame:null,inspections:0,renders:0,stops:0,
    request:async(path,body)=>{calls.push({path,body});return{accepted:true,sample_id:'owned-sample',state:'sampling'};},
    inspector(){this.inspections+=1;},render(){this.renders+=1;},stopFlow(){this.stops+=1;},
    ...overrides,
  });
  return{owner,calls};
}

test('watch is opt-in and requests exactly one bounded sample for only the selected topic',async t=>{
  browserDocument(t);
  const{owner,calls}=watcher({watchTopic:null,extraTopics:[{id:'other-topic',name:'/other',types:['std_msgs/msg/String'],traffic:{state:'not_sampled'}}]});
  await owner.watchSelected();assert.deepEqual(calls,[]);
  owner.watchTopic='topic';await owner.watchSelected();
  assert.deepEqual(calls,[{path:'/api/v1/ros/sample',body:{topic:'/data',duration_s:5}}]);
  assert.equal(owner.watchPending,false);assert.equal(owner.watchTopic,'topic');
  assert.equal(calls.some(c=>'stop' in c.body),false);
});

test('a successful fresh watch acknowledgment permits the first subsequent observed count advance to pulse',async t=>{
  browserDocument(t);const{owner}=watcher();
  await owner.watchSelected();assert.equal(owner.flowActivity.active('topic',100),false);
  const observed=graph(4,{sample:{sample_id:'owned-sample'}});
  assert.equal(owner.flowActivity.observe(observed,100),true);
  assert.equal(owner.flowActivity.active('topic',100),true);
  assert.equal(owner.flowActivity.observe(observed,900),false);
  assert.equal(owner.flowActivity.active('topic',1501),false);
});

test('hidden browser, hidden app page, disconnected state and stale discovery never start a watch sample',async t=>{
  const document=browserDocument(t);
  for(const setup of [
    ({owner})=>{document.hidden=true;},
    ({owner})=>{owner.visible=false;},
    ({owner})=>{owner.connected=false;},
    ({owner})=>{owner.data.state='stale';},
    ({owner})=>{owner.data.state='initializing';},
    ({owner})=>{owner.data.age_ms=4500;},
    ({owner})=>{owner.data.age_ms=null;},
    ({owner})=>{owner.data.age_ms=NaN;},
    ({owner})=>{owner.data.age_ms=-1;},
  ]){
    document.hidden=false;const value=watcher();setup(value);
    await value.owner.watchSelected();assert.equal(value.calls.length,0);
  }
});

test('watch never samples a node, service, action or a topic that is no longer selected',async t=>{
  browserDocument(t);
  for(const focus of ['publisher','service','action']){
    const{owner,calls}=watcher({focus,watchTopic:focus});await owner.watchSelected();assert.equal(calls.length,0,focus);
  }
  const{owner,calls}=watcher({focus:'publisher',watchTopic:'topic'});await owner.watchSelected();assert.equal(calls.length,0);
});

test('already sampling traffic is reused without replacement or resetting its observed baseline',async t=>{
  browserDocument(t);const{owner,calls}=watcher({sample:{state:'sampling',sample_id:'borrowed-sample',messages:100,last_message_age_ms:0}});
  assert.equal(owner.flowActivity.observe(owner.data,10),false);
  await owner.watchSelected();assert.deepEqual(calls,[]);assert.equal(owner.watchTopic,'topic');
  assert.equal(owner.flowActivity.observe(owner.data,20),false);
  assert.equal(owner.flowActivity.active('topic',20),false);
});

test('full sampler slots and pending cleanup prevent requests while retaining the explicit watch intent',async t=>{
  browserDocument(t);
  const peer=id=>({id,name:`/${id}`,types:['std_msgs/msg/String'],traffic:{state:'sampling',sample_id:`s-${id}`,messages:1,last_message_age_ms:0}});
  for(const options of [
    {extraTopics:[peer('a'),peer('b')]},
    {extraTopics:[peer('a')],snapshot:{sampling:{enabled:true,max_topics:2,max_duration_s:10,cleanup_pending:1}}},
    {snapshot:{sampling:{enabled:true,max_topics:2,max_duration_s:10,cleanup_pending:2}}},
  ]){
    const{owner,calls}=watcher(options);await owner.watchSelected();
    assert.equal(calls.length,0);assert.equal(owner.watchTopic,'topic');assert.equal(owner.watchPending,false);
  }
});

test('watch duration honors a lower configured sampler ceiling',async t=>{
  browserDocument(t);const{owner,calls}=watcher({snapshot:{sampling:{enabled:true,max_topics:2,max_duration_s:2,cleanup_pending:0}}});
  await owner.watchSelected();assert.equal(calls[0].body.duration_s,2);
});

test('blocked, unavailable, disabled or budget-exhausted sampling pauses the watch without retrying',async t=>{
  browserDocument(t);
  for(const options of [
    {sample:{state:'blocked',reason:'High-bandwidth type blocked'}},
    {sample:{state:'unavailable',reason:'Message type unavailable'}},
    {sample:{state:'complete',reason:'Sample message/byte budget reached'}},
    {sample:{state:'complete',reason:'Message exceeds 64 KiB observation budget'}},
    {snapshot:{sampling:{enabled:false,max_topics:2,max_duration_s:10,cleanup_pending:0}}},
  ]){
    const{owner,calls}=watcher(options);await owner.watchSelected();await owner.watchSelected();
    assert.equal(calls.length,0);assert.equal(owner.watchTopic,null);assert.ok(owner.watchError);
  }
});

test('server denial or request failure pauses the watch and clears its pending latch',async t=>{
  browserDocument(t);
  for(const failure of [false,true]){
    const{owner,calls}=watcher();owner.request=async(path,body)=>{calls.push({path,body});if(failure)throw Error('Transport unavailable');return{accepted:false,reason:'Concurrent sampling limit reached'};};
    await owner.watchSelected();await owner.watchSelected();
    assert.equal(calls.length,1);assert.equal(owner.watchTopic,null);assert.equal(owner.watchPending,false);assert.ok(owner.watchError);
    assert.equal(owner.flowActivity.observe(graph(4,{sample:{sample_id:'not-admitted'}}),100),false);
  }
});

test('concurrent watch checks share a single pending sample request',async t=>{
  browserDocument(t);const response=deferred(),{owner,calls}=watcher();
  owner.request=(path,body)=>{calls.push({path,body});return response.promise;};
  const first=owner.watchSelected();assert.equal(owner.watchPending,true);
  await owner.watchSelected();assert.equal(calls.length,1);
  response.resolve({accepted:true,sample_id:'owned-sample'});await first;assert.equal(owner.watchPending,false);
});

test('late sample response after app/browser hiding neither seeds flow nor sends a stop or renewal',async t=>{
  const document=browserDocument(t);
  for(const hide of [owner=>owner.setVisible(false),()=>{document.hidden=true;}]){
    document.hidden=false;const response=deferred(),{owner,calls}=watcher();
    owner.request=(path,body)=>{calls.push({path,body});return response.promise;};
    const pending=owner.watchSelected();hide(owner);
    response.resolve({accepted:true,sample_id:'late-sample'});await pending;await owner.watchSelected();
    assert.equal(calls.length,1);assert.equal(calls.some(c=>c.body.stop),false);
    assert.equal(owner.flowActivity.observe(graph(9,{sample:{sample_id:'late-sample'}}),100),false);
    assert.equal(owner.watchPending,false);
  }
});

test('late response after selection change never samples the replacement selection or resets another sample',async t=>{
  browserDocument(t);const response=deferred(),{owner,calls}=watcher({extraTopics:[{id:'other-topic',name:'/other',types:['std_msgs/msg/String'],traffic:{state:'sampling',sample_id:'another-client',messages:30,last_message_age_ms:0}}]});
  owner.request=(path,body)=>{calls.push({path,body});return response.promise;};
  const pending=owner.watchSelected();owner.select('other-topic');
  response.resolve({accepted:true,sample_id:'late-sample'});await pending;await owner.watchSelected();
  assert.equal(owner.watchTopic,null);assert.equal(owner.focus,'other-topic');assert.equal(calls.length,1);
  assert.equal(calls[0].body.topic,'/data');assert.equal(calls.some(c=>c.body.stop),false);
  assert.equal(owner.flowActivity.observe(graph(9,{sample:{sample_id:'late-sample'}}),100),false);
});

test('disconnect while sample creation is pending retires watch intent without a cleanup stop',async t=>{
  browserDocument(t);const response=deferred(),{owner,calls}=watcher();
  owner.request=(path,body)=>{calls.push({path,body});return response.promise;};
  const pending=owner.watchSelected();owner.setConnected(false);
  response.resolve({accepted:true,sample_id:'late-sample'});await pending;owner.setConnected(true);await owner.watchSelected();
  assert.equal(calls.length,1);assert.equal(owner.watchTopic,null);assert.equal(calls.some(c=>c.body.stop),false);
});
