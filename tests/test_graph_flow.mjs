import test from 'node:test';
import assert from 'node:assert/strict';
import {
  FLOW_LIMITS, GRAPH_UPDATE_INTERVALS, normalizeUpdateInterval, graphStructureSignature, TopicFlowActivity, cubicPoint, flowEdges, normalizeGraph, GraphLiveWatch, GraphExplorer, activityLabel,
} from '../web/graph.js';

const node=id=>({id,name:id,namespace:'/',fully_qualified_name:`/${id}`,instance_count:1});
const activity=(messages=0,extra={})=>({
  state:'watching',sample_id:'counter-a',source:'ros_subscription',messages,bytes:messages*100,
  last_message_age_ms:0,rate_hz:20,publisher_counts:[{publisher_gid:'aa01',messages,last_message_age_ms:0}],
  unknown_publisher_messages:0,...extra,
});
function graph(messages=0,{counter={},snapshot={},extraTopics=[],extraEdges=[]}={}){
  return normalizeGraph({
    schema_version:1,state:'ready',age_ms:0,
    watch:{enabled:true,active:true,lease_count:1,lease_s:12,max_topics:64,coverage:'complete'},
    nodes:[node('publisher'),node('subscriber'),node('other-publisher')],
    topics:[{id:'topic',name:'/data',types:['std_msgs/msg/Float32'],
      publishers:[{id:'publisher:aa01',gid:'aa01',node_id:'publisher'}],
      subscriptions:[{id:'subscription:bb01',gid:'bb01',node_id:'subscriber'}],
      activity:activity(messages,counter)},...extraTopics],
    services:[{id:'service',name:'/inspect',types:['example_interfaces/srv/Trigger']}],
    actions:[{id:'action',name:'/execute',types:['example_interfaces/action/Fibonacci']}],
    edges:[
      {id:'pub',kind:'publish',from:'publisher',to:'topic',endpoint_id:'publisher:aa01'},
      {id:'sub',kind:'subscribe',from:'topic',to:'subscriber',endpoint_id:'subscription:bb01'},
      {id:'service-client',kind:'service_client',from:'subscriber',to:'service'},
      {id:'service-server',kind:'service_server',from:'service',to:'publisher'},
      {id:'action-client',kind:'action_client',from:'subscriber',to:'action'},
      {id:'action-server',kind:'action_server',from:'action',to:'publisher'},
      ...extraEdges,
    ],...snapshot,
  });
}
function live(owner=new TopicFlowActivity(),now=1000,options={}){
  owner.observe(graph(10,options),now-10);const current=graph(11,options);
  assert.equal(owner.observe(current,now),true);return{owner,current,now};
}
const near=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-9,`${actual} != ${expected}`);

test('continuous flow separates cycle/evidence limits and bounds rendering work',()=>{
  assert.deepEqual(FLOW_LIMITS,{maxEdges:128,maxFps:30,cycleMs:1400,evidenceMs:3500,staleMs:4500});
});
test('first historical counters prime without replay; later advances start evidence',()=>{
  const owner=new TopicFlowActivity();assert.equal(owner.observe(graph(120),100),false);
  assert.equal(owner.active('topic',100),false);assert.equal(owner.active('topic::publisher:aa01',100),false);
  assert.equal(owner.observe(graph(121),200),true);
  assert.equal(owner.active('topic',200),true);assert.equal(owner.active('topic::publisher:aa01',200),true);
});
test('advancing counters sustain twenty seconds of flow without restarting phase',()=>{
  const owner=new TopicFlowActivity();owner.observe(graph(0),0);const start=2000;
  for(let now=start;now<=24000;now+=2000){
    if(now>start)assert.equal(owner.active('topic',now-1),true,`gap before ${now}`);
    assert.equal(owner.observe(graph(now/10),now),true);
    assert.equal(owner.active('topic',now+1900),true,`gap after ${now}`);
    near(owner.progress('topic',now+500),((now+500-start)%FLOW_LIMITS.cycleMs)/FLOW_LIMITS.cycleMs);
    near(owner.progress('topic::publisher:aa01',now+500),((now+500-start)%FLOW_LIMITS.cycleMs)/FLOW_LIMITS.cycleMs);
  }
});
test('phase wraps while fresh and faster counter updates do not starve the later part of a cycle',()=>{
  const{owner,now}=live();near(owner.progress('topic',now+100),100/FLOW_LIMITS.cycleMs);
  near(owner.progress('topic',now+FLOW_LIMITS.cycleMs+100),100/FLOW_LIMITS.cycleMs);
  for(let elapsed=200;elapsed<=1000;elapsed+=200)owner.observe(graph(11+elapsed),now+elapsed);
  near(owner.progress('topic',now+1100),1100/FLOW_LIMITS.cycleMs);assert.equal(owner.active('topic',now+2000),true);
});
test('fresh polls with frozen counters or rates never extend evidence',()=>{
  const{owner,now}=live();
  for(const elapsed of [1000,2000,3000])assert.equal(owner.observe(graph(11,{counter:{rate_hz:999,last_message_age_ms:0}}),now+elapsed),false);
  assert.equal(owner.active('topic',now+FLOW_LIMITS.evidenceMs-1),true);
  assert.equal(owner.active('topic',now+FLOW_LIMITS.evidenceMs+1),false);
  assert.equal(owner.progress('topic',now+FLOW_LIMITS.evidenceMs+1),null);
  assert.equal(owner.observe(graph(11),now+9000),false);assert.equal(owner.active('topic',now+9000),false);
});
test('new counter epochs and resets retire previous evidence before later increments',()=>{
  const{owner,now}=live();assert.equal(owner.observe(graph(900,{counter:{sample_id:'counter-b'}}),now+10),false);
  assert.equal(owner.active('topic',now+10),false);
  assert.equal(owner.observe(graph(901,{counter:{sample_id:'counter-b'}}),now+20),true);
  for(const count of [3,0]){
    assert.equal(owner.observe(graph(count,{counter:{sample_id:'counter-b'}}),now+30),false);
    assert.equal(owner.active('topic',now+30),false);
  }
  assert.equal(owner.observe(graph(1,{counter:{sample_id:'counter-b'}}),now+40),true);
});
test('inactive blocked limited throttled and unavailable records stop despite higher final counts',()=>{
  for(const state of ['inactive','blocked','unavailable','limited','throttled','cleanup_pending']){
    const{owner,now}=live();const current=graph(200,{counter:{state,rate_hz:100}});
    assert.equal(owner.observe(current,now+10),false,state);assert.equal(owner.active('topic',now+10),false,state);
    assert.deepEqual(flowEdges(current,current.edges,owner,now+10,{}),[],state);
  }
});
test('freshness must be finite, nonnegative and within the advertised observation window',()=>{
  for(const age_ms of [null,undefined,-1,NaN,Infinity,4501]){
    const{owner,now}=live();owner.observe(graph(12,{snapshot:{age_ms}}),now+10);assert.equal(owner.active('topic',now+10),false,`snapshot ${age_ms}`);
  }
  for(const last_message_age_ms of [null,undefined,-1,NaN,Infinity,3501]){
    const{owner,now}=live();owner.observe(graph(12,{counter:{last_message_age_ms}}),now+10);assert.equal(owner.active('topic',now+10),false,`message ${last_message_age_ms}`);
  }
  for(const state of ['stale','initializing','unknown']){
    const{owner,now}=live();owner.observe(graph(12,{snapshot:{state,age_ms:0}}),now+10);assert.equal(owner.active('topic',now+10),false,state);
  }
});
test('local elapsed time expires the remaining graph or message freshness budget',()=>{
  for(const options of [{snapshot:{age_ms:4400}},{counter:{last_message_age_ms:3400}}]){
    const{owner,now}=live(new TopicFlowActivity(),1000,options);
    assert.equal(owner.active('topic',now+50),true);assert.equal(owner.active('topic',now+101),false);
  }
  const{owner,now}=live(new TopicFlowActivity(),1000,{snapshot:{state:'partial'}});assert.equal(owner.active('topic',now),true);
});
test('malformed counters and absent identities do not become invented zero baselines',()=>{
  for(const messages of [null,undefined,-1,1.5,NaN,Infinity]){
    const{owner,now}=live();owner.observe(graph(11,{counter:{messages}}),now+10);assert.equal(owner.active('topic',now+10),false);
  }
  for(const sample_id of [null,undefined,'']){
    const{owner,now}=live();assert.equal(owner.observe(graph(12,{counter:{sample_id}}),now+10),false);assert.equal(owner.active('topic',now+10),false);
  }
});
test('missing parent counter epochs cannot become synthetic per-GID epochs on repeated observations',()=>{
  for(const sample_id of [null,undefined,'']){
    const owner=new TopicFlowActivity();owner.observe(graph(10,{counter:{sample_id}}),0);
    const current=graph(11,{counter:{sample_id}});assert.equal(owner.observe(current,100),false);
    assert.equal(owner.active('topic::publisher:aa01',100),false);
    assert.deepEqual(flowEdges(current,current.edges,owner,100,{}),[]);
  }
});
test('stale batches never replay; removal and clear retire aggregate and GID state',()=>{
  const{owner,now}=live();owner.observe(graph(20,{snapshot:{state:'stale',age_ms:9000}}),now+10);
  assert.equal(owner.observe(graph(20),now+20),false);assert.equal(owner.active('topic',now+20),false);
  assert.equal(owner.observe(graph(21),now+30),true);
  owner.observe(normalizeGraph({schema_version:1,state:'ready',age_ms:0,nodes:[],topics:[],services:[],actions:[],edges:[]}),now+40);
  assert.equal(owner.active('topic',now+40),false);assert.equal(owner.active('topic::publisher:aa01',now+40),false);
  assert.equal(owner.observe(graph(30),now+50),false);owner.observe(graph(31),now+60);owner.clear();
  assert.equal(owner.active('topic',now+61),false);assert.equal(owner.observe(graph(40),now+70),false);
});
test('finite sampling is only a fallback when live activity is absent',()=>{
  const owner=new TopicFlowActivity();
  const old=count=>{const g=graph(count);delete g.topics[0].activity;g.topics[0].traffic={state:'sampling',sample_id:'old-sample',messages:count,last_message_age_ms:0};return g;};
  assert.equal(owner.observe(old(10),0),false);assert.equal(owner.observe(old(11),100),true);
  const ended=old(12);ended.topics[0].traffic.state='complete';owner.observe(ended,200);assert.equal(owner.active('topic',200),false);
  const unavailable=graph(14,{counter:{state:'unavailable'}});unavailable.topics[0].traffic={state:'sampling',sample_id:'old-sample',messages:14,last_message_age_ms:0};
  owner.observe(unavailable,300);assert.equal(owner.active('topic',300),false);
});
test('publisher GID evidence is filled, subscriber routes hollow, and discovery is unmodified',()=>{
  const{owner,current,now}=live(),before=JSON.stringify(current.edges),chosen=flowEdges(current,current.edges,owner,now,{});
  assert.deepEqual(chosen.map(e=>e.id).sort(),['pub','sub']);const pub=chosen.find(e=>e.id==='pub'),sub=chosen.find(e=>e.id==='sub');
  assert.equal(pub.topicId,'topic');assert.equal(pub.sourceKey,'topic::publisher:aa01');assert.equal(pub.hollow,false);
  assert.equal(sub.topicId,'topic');assert.equal(sub.sourceKey,'topic');assert.equal(sub.hollow,true);
  assert.equal(JSON.stringify(current.edges),before);assert.notEqual(pub,current.edges[0]);assert.deepEqual(flowEdges(current,[],owner,now,{}),[]);
});
test('single publisher without GID evidence is an explicitly hollow discovered route',()=>{
  const{owner,current,now}=live(new TopicFlowActivity(),1000,{counter:{publisher_counts:[],unknown_publisher_messages:11}});
  const pub=flowEdges(current,current.edges,owner,now,{}).find(e=>e.id==='pub');
  assert.ok(pub);assert.equal(pub.hollow,true);assert.equal(pub.attribution,'single_discovered_publisher');assert.equal(pub.sourceKey,'topic');
});
function multiplePublisherGraph(total,a,b){
  const g=graph(total,{counter:{publisher_counts:[{publisher_gid:'aa01',messages:a,last_message_age_ms:0},{publisher_gid:'aa02',messages:b,last_message_age_ms:0}]},
    extraEdges:[{id:'pub2',kind:'publish',from:'publisher',to:'topic',endpoint_id:'publisher:aa02'}]});
  g.topics[0].publishers.push({id:'publisher:aa02',gid:'aa02',node_id:'publisher'});return g;
}
test('multiple endpoints on one publisher node animate only GIDs whose counts advanced',()=>{
  const owner=new TopicFlowActivity();owner.observe(multiplePublisherGraph(20,10,10),0);
  const current=multiplePublisherGraph(21,11,10);owner.observe(current,100);const chosen=flowEdges(current,current.edges,owner,100,{});
  assert.deepEqual(chosen.map(e=>e.id).sort(),['pub','sub']);assert.equal(chosen.find(e=>e.id==='pub').hollow,false);
  assert.deepEqual(flowEdges(current,current.edges.filter(e=>e.id==='pub2'||e.id==='sub'),owner,100,{}).map(e=>e.id),['sub']);
});
test('unknown traffic cannot choose an upstream source among multiple discovered publishers',()=>{
  const owner=new TopicFlowActivity(),first=multiplePublisherGraph(20,0,0);first.topics[0].activity.publisher_counts=[];owner.observe(first,0);
  const current=multiplePublisherGraph(21,0,0);current.topics[0].activity.publisher_counts=[];current.topics[0].activity.unknown_publisher_messages=21;owner.observe(current,100);
  assert.deepEqual(flowEdges(current,current.edges.filter(e=>e.id==='pub'||e.id==='sub'),owner,100,{}).map(e=>e.id),['sub']);
});
function actionGraph(statusCount,feedbackCount,{state='watching',statusExtra={},feedbackExtra={}}={}){
  const status=activity(statusCount,{sample_id:'status-epoch',publisher_counts:[{publisher_gid:'cc01',messages:statusCount,last_message_age_ms:0}],...statusExtra});
  const feedback=activity(feedbackCount,{sample_id:'feedback-epoch',publisher_counts:[{publisher_gid:'cc02',messages:feedbackCount,last_message_age_ms:0}],...feedbackExtra});
  return normalizeGraph({schema_version:1,state:'ready',age_ms:0,nodes:[node('server'),node('client')],
    topics:[
      {id:'status-topic',name:'/execute/_action/status',types:['action_msgs/msg/GoalStatusArray'],publishers:[{id:'publisher:cc01',gid:'cc01',node_id:'server'}],activity:status},
      {id:'feedback-topic',name:'/execute/_action/feedback',types:['example/action/Run_FeedbackMessage'],publishers:[{id:'publisher:cc02',gid:'cc02',node_id:'server'}],activity:feedback},
    ],services:[{id:'service',name:'/other_service',types:['example/srv/Other']}],
    actions:[{id:'action',name:'/execute',types:['example/action/Run'],servers:['server'],clients:['client'],activity:{
      state,status_topic:'/execute/_action/status',feedback_topic:'/execute/_action/feedback',status,feedback,
      messages:statusCount+feedbackCount,status_messages:statusCount,feedback_messages:feedbackCount,last_message_age_ms:0,rate_hz:10,
      requests:{state:'unavailable',reason:'Goal requests are not observed'},
    }}],edges:[
      {id:'status-pub',kind:'publish',from:'server',to:'status-topic',endpoint_id:'publisher:cc01'},
      {id:'feedback-pub',kind:'publish',from:'server',to:'feedback-topic',endpoint_id:'publisher:cc02'},
      {id:'action-server',kind:'action_server',from:'action',to:'server'},
      {id:'action-client',kind:'action_client',from:'client',to:'action'},
      {id:'service-client',kind:'service_client',from:'client',to:'service'},
      {id:'service-server',kind:'service_server',from:'service',to:'server'},
    ]});
}
test('action status/feedback animate reverse server responses without inventing goal requests',()=>{
  const owner=new TopicFlowActivity();owner.observe(actionGraph(10,10),0);const current=actionGraph(11,11);owner.observe(current,100);
  assert.equal(owner.active('action',100),true);
  const chosen=flowEdges(current,current.edges.filter(e=>e.kind.startsWith('action')||e.kind.startsWith('service')),owner,100,{focus:'action'});
  assert.ok(chosen.some(e=>e.id==='action-server'));assert.ok(chosen.some(e=>e.id==='action-client'));
  for(const e of chosen){assert.equal(e.topicId,'action');assert.equal(e.reverse,true);assert.ok(e.kind.startsWith('action'));}
  for(const e of chosen.filter(e=>e.id==='action-server')){assert.equal(e.hollow,false);assert.equal(e.sourceKey,'action::server:server');}
  for(const e of chosen.filter(e=>e.id==='action-client')){assert.equal(e.hollow,true);assert.equal(e.sourceKey,'action');}
  assert.equal(current.actions[0].activity.requests.state,'unavailable');
});
test('partial action feedback remains observable without pretending status or request data exists',()=>{
  const owner=new TopicFlowActivity(),options={state:'partial',statusExtra:{state:'unavailable',messages:null,publisher_counts:[],last_message_age_ms:null}};
  owner.observe(actionGraph(0,10,options),0);const current=actionGraph(0,11,options);owner.observe(current,100);
  const chosen=flowEdges(current,current.edges.filter(e=>e.kind.startsWith('action')),owner,100,{});
  assert.ok(chosen.length);assert.equal(chosen.some(e=>e.sourceKey==='status-topic::publisher:cc01'),false);assert.equal(chosen.every(e=>e.reverse===true),true);
});
test('frozen or inactive action evidence expires rather than looping historical status',()=>{
  const owner=new TopicFlowActivity();owner.observe(actionGraph(1,1),0);const current=actionGraph(2,2);owner.observe(current,100);
  for(const now of [1000,2000,3000])owner.observe(current,now);assert.equal(owner.active('action',3601),false);
  owner.observe(actionGraph(3,3),4000);assert.equal(owner.active('action',4000),true);
  const stopped=actionGraph(4,4,{state:'inactive',statusExtra:{state:'inactive'},feedbackExtra:{state:'inactive'}});
  owner.observe(stopped,4100);assert.equal(owner.active('action',4100),false);assert.deepEqual(flowEdges(stopped,stopped.edges,owner,4100,{}),[]);
});
test('action aggregate requires an actual source epoch instead of a placeholder composite identity',()=>{
  const owner=new TopicFlowActivity(),options={statusExtra:{sample_id:null},feedbackExtra:{sample_id:null}};
  owner.observe(actionGraph(1,1,options),0);const current=actionGraph(2,2,options);owner.observe(current,100);
  assert.equal(owner.active('action',100),false);
  assert.deepEqual(flowEdges(current,current.edges.filter(e=>e.kind.startsWith('action')),owner,100,{}),[]);
});
function mixedActiveGraph(count){
  const ordinary=graph(count),responses=actionGraph(count,count);
  return normalizeGraph({schema_version:1,state:'ready',age_ms:0,
    nodes:[...ordinary.nodes,...responses.nodes],topics:[...ordinary.topics,...responses.topics],
    services:responses.services,actions:responses.actions,
    edges:[...ordinary.edges.filter(e=>e.kind==='publish'||e.kind==='subscribe'),...responses.edges]});
}
test('focused-only topic dots exclude other active topics/actions while leaving their evidence intact',()=>{
  const owner=new TopicFlowActivity();owner.observe(mixedActiveGraph(10),0);const current=mixedActiveGraph(11);owner.observe(current,100);
  assert.equal(owner.active('action',100),true);assert.equal(owner.active('feedback-topic',100),true);
  assert.ok(flowEdges(current,current.edges,owner,100,{}).length>2);
  const selected=flowEdges(current,current.edges,owner,100,{focus:'topic',focusedOnly:true});
  assert.deepEqual(selected.map(e=>e.id).sort(),['pub','sub']);
  assert.equal(owner.active('action',100),true);assert.equal(owner.active('feedback-topic',100),true);
  assert.equal(current.edges.length,8);
});
test('focused-only action dots retain response edges without animating its underlying topic edges',()=>{
  const owner=new TopicFlowActivity();owner.observe(mixedActiveGraph(10),0);const current=mixedActiveGraph(11);owner.observe(current,100);
  const selected=flowEdges(current,current.edges,owner,100,{focus:'action',focusedOnly:true});
  assert.deepEqual(selected.map(e=>e.id).sort(),['action-client','action-server']);
  assert.equal(selected.every(e=>e.reverse===true),true);
  assert.equal(owner.active('status-topic',100),true);assert.equal(owner.active('topic',100),true);
});
test('focused-only mode hides all dots without selection and keeps only highlighted routes for a node',()=>{
  const owner=new TopicFlowActivity();owner.observe(mixedActiveGraph(10),0);const current=mixedActiveGraph(11);owner.observe(current,100);
  assert.deepEqual(flowEdges(current,current.edges,owner,100,{focusedOnly:true}),[]);
  const selected=flowEdges(current,current.edges,owner,100,{focus:'server',focusedOnly:true});
  assert.deepEqual(selected.map(e=>e.id).sort(),['action-server','feedback-pub','status-pub']);
  assert.equal(selected.every(e=>e.from==='server'||e.to==='server'),true);
  assert.equal(selected.some(e=>e.kind.startsWith('service')),false);
});
function largeGraph(messages){
  const topics=Array.from({length:160},(_,i)=>({id:`topic${i}`,name:`/topic${i}`,types:['std_msgs/msg/Float32'],
    publishers:[{id:`publisher:${i}`,gid:`gid${i}`,node_id:'publisher'}],
    activity:activity(messages,{sample_id:`counter${i}`,publisher_counts:[{publisher_gid:`gid${i}`,messages,last_message_age_ms:0}]})}));
  return normalizeGraph({schema_version:1,state:'ready',age_ms:0,nodes:[node('publisher'),node('focus-node')],topics,services:[],actions:[],
    edges:[...topics.map((t,i)=>({id:`edge${i}`,kind:'publish',from:'publisher',to:t.id,endpoint_id:`publisher:${i}`})),
      {id:'focus-sub',kind:'subscribe',from:'topic159',to:'focus-node',endpoint_id:'focus-sub-gid'}]});
}
test('128-edge cap prioritizes focused topic routes without changing visible edges',()=>{
  const owner=new TopicFlowActivity();owner.observe(largeGraph(0),0);const current=largeGraph(1);owner.observe(current,100);
  const visible=[...current.edges],chosen=flowEdges(current,visible,owner,100,{focus:'topic159'});
  assert.equal(chosen.length,FLOW_LIMITS.maxEdges);assert.equal(new Set(chosen.map(e=>e.id)).size,chosen.length);
  assert.deepEqual(new Set(chosen.slice(0,2).map(e=>e.id)),new Set(['edge159','focus-sub']));assert.equal(visible.length,161);
});
test('node focus prioritizes its evidenced route at the cap',()=>{
  const owner=new TopicFlowActivity();owner.observe(largeGraph(0),0);const current=largeGraph(1);owner.observe(current,100);
  const chosen=flowEdges(current,current.edges,owner,100,{focus:'focus-node'});assert.equal(chosen.length,FLOW_LIMITS.maxEdges);assert.equal(chosen[0].id,'focus-sub');
});
test('cubic coordinates follow the curve and preserve controls',()=>{
  const points=[[0,0],[0,10],[10,10],[10,0]],before=JSON.stringify(points);
  assert.deepEqual(cubicPoint(points,0),[0,0]);assert.deepEqual(cubicPoint(points,1),[10,0]);assert.deepEqual(cubicPoint(points,.5),[5,7.5]);
  assert.deepEqual(cubicPoint([[2,3],[4,5],[6,7],[8,9]],.25),[3.5,4.5]);assert.equal(JSON.stringify(points),before);
});

const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const watchCapability={enabled:true,lease_s:12,max_topics:64};
function leaseHarness(request){
  let clock=0,nextLease=0;const calls=[];
  const send=async(path,body)=>{
    calls.push({path,body:{...body},at:clock});
    if(request)return request(path,body,calls);
    return{accepted:true,lease_id:body.operation==='acquire'?`lease-${++nextLease}`:body.lease_id,
      expires_in_ms:body.operation==='release'?0:12000,active:body.operation!=='release'};
  };
  const watch=new GraphLiveWatch({request:send,now:()=>clock,clientId:'33333333-3333-4333-8333-333333333333'});
  return{watch,calls,time:value=>{clock=value;}};
}

test('graph-wide lease is default-enabled but inactive pages do not acquire it',async()=>{
  const{watch,calls}=leaseHarness();assert.equal(watch.enabled,true);assert.equal(watch.active,false);
  await watch.sync(watchCapability);assert.deepEqual(calls,[]);
  watch.setActive(true);await watch.sync(watchCapability);
  assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/v1/ros/watch');
  assert.deepEqual(calls[0].body,{operation:'acquire',client_id:'33333333-3333-4333-8333-333333333333'});
  assert.equal(watch.leaseId,'lease-1');assert.equal('topic' in calls[0].body,false);
});
test('renewals retain one lease for24s without stopping or reacquiring counters',async()=>{
  const{watch,calls,time}=leaseHarness();watch.setActive(true);await watch.sync(watchCapability);
  for(let now=2000;now<=24000;now+=2000){time(now);await watch.sync(watchCapability);assert.equal(watch.leaseId,'lease-1');}
  assert.equal(calls.filter(c=>c.body.operation==='acquire').length,1);
  assert.equal(calls.filter(c=>c.body.operation==='renew').length,6);
  assert.equal(calls.some(c=>c.body.operation==='release'||c.path.includes('/sample')),false);
  for(const call of calls.slice(1)){assert.equal(call.body.lease_id,'lease-1');assert.equal(call.at%4000,0);}
});
test('lower granted lease duration shortens renewal cadence instead of waiting past expiry',async()=>{
  const{watch,calls,time}=leaseHarness(async(_path,body)=>({accepted:true,lease_id:body.lease_id||'short',expires_in_ms:4000,active:true}));
  watch.setActive(true);await watch.sync({enabled:true,lease_s:4});time(1999);await watch.sync();assert.equal(calls.length,1);
  time(2000);await watch.sync();assert.equal(calls.length,2);assert.equal(calls[1].body.operation,'renew');
});
test('disabled backend capability never creates a lease or erases the users monitoring intent',async()=>{
  const{watch,calls}=leaseHarness();watch.setActive(true);await watch.sync({enabled:false,lease_s:12});
  assert.equal(calls.length,0);assert.equal(watch.enabled,true);
});
test('hidden/degraded page releases its own lease and resumes when active with intent retained',async()=>{
  const{watch,calls}=leaseHarness();watch.setActive(true);await watch.sync(watchCapability);
  watch.setActive(false);await watch.sync();assert.equal(watch.enabled,true);assert.equal(watch.leaseId,null);
  assert.deepEqual(calls.at(-1).body,{operation:'release',lease_id:'lease-1'});
  watch.setActive(true);await watch.sync();assert.equal(watch.leaseId,'lease-2');
  assert.deepEqual(calls.map(c=>c.body.operation),['acquire','release','acquire']);
});
test('explicit Live flow disable releases and stays disabled until explicitly re-enabled',async()=>{
  const{watch,calls}=leaseHarness();watch.setActive(true);await watch.sync(watchCapability);
  watch.setEnabled(false);await watch.sync();await watch.sync();
  assert.equal(watch.leaseId,null);assert.equal(watch.enabled,false);assert.equal(calls.length,2);
  watch.setEnabled(true);await watch.sync();assert.equal(watch.leaseId,'lease-2');
});
test('transient request failure backs off without clearing live-flow intent',async()=>{
  let fail=true;const{watch,calls,time}=leaseHarness(async(_path,body)=>{
    if(body.operation==='renew'&&fail){fail=false;throw Error('Temporary disconnect');}
    return{accepted:true,lease_id:body.lease_id||'same-lease',expires_in_ms:12000,active:true};
  });
  watch.setActive(true);await watch.sync(watchCapability);time(4000);await watch.sync();
  assert.equal(watch.enabled,true);assert.match(watch.error,/Temporary disconnect/);const failedCount=calls.length;
  time(5000);await watch.sync();assert.equal(calls.length,failedCount);
  time(6001);await watch.sync();assert.ok(calls.length>failedCount);assert.equal(watch.error,'');
});
test('overlapping syncs serialize one acquire instead of creating competing leases',async()=>{
  const response=deferred(),{watch,calls}=leaseHarness(()=>response.promise);
  watch.setActive(true);const first=watch.sync(watchCapability),second=watch.sync(watchCapability);
  await Promise.resolve();
  assert.equal(calls.length,1);response.resolve({accepted:true,lease_id:'one',expires_in_ms:12000,active:true});
  await Promise.all([first,second]);assert.equal(watch.leaseId,'one');assert.equal(calls.length,1);
});
test('late acquire after page hiding releases exactly its returned lease and cannot reactivate',async()=>{
  const response=deferred(),{watch,calls}=leaseHarness(async(_path,body)=>body.operation==='acquire'?response.promise:{accepted:true,lease_id:body.lease_id,expires_in_ms:0,active:false});
  watch.setActive(true);const acquiring=watch.sync(watchCapability);await Promise.resolve();watch.setActive(false);
  response.resolve({accepted:true,lease_id:'late-one',expires_in_ms:12000,active:true});await acquiring;await watch.sync();
  assert.deepEqual(calls.map(c=>c.body.operation),['acquire','release']);assert.equal(calls[1].body.lease_id,'late-one');
  assert.equal(watch.leaseId,null);assert.equal(watch.active,false);assert.equal(watch.enabled,true);
});
test('disable/re-enable during acquisition cleans the retired lease before acquiring replacement',async()=>{
  const response=deferred();let acquisitions=0;
  const{watch,calls}=leaseHarness(async(_path,body)=>{
    if(body.operation==='acquire')return ++acquisitions===1?response.promise:{accepted:true,lease_id:'replacement',expires_in_ms:12000,active:true};
    return{accepted:true,lease_id:body.lease_id,expires_in_ms:0,active:false};
  });
  watch.setActive(true);const acquiring=watch.sync(watchCapability);await Promise.resolve();watch.setEnabled(false);watch.setEnabled(true);
  response.resolve({accepted:true,lease_id:'retired',expires_in_ms:12000,active:true});await acquiring;await watch.sync();
  assert.deepEqual(calls.map(c=>c.body.operation),['acquire','release','acquire']);
  assert.equal(calls[1].body.lease_id,'retired');assert.equal(watch.leaseId,'replacement');
});
test('late renewal after disable cannot resurrect a retired lease',async()=>{
  const response=deferred(),{watch,calls,time}=leaseHarness(async(_path,body)=>body.operation==='renew'?response.promise:{accepted:true,lease_id:body.lease_id||'lease-a',expires_in_ms:body.operation==='release'?0:12000,active:body.operation!=='release'});
  watch.setActive(true);await watch.sync(watchCapability);time(4000);const renewing=watch.sync();await Promise.resolve();watch.setEnabled(false);
  response.resolve({accepted:true,lease_id:'lease-a',expires_in_ms:12000,active:true});await renewing;await watch.sync();
  assert.deepEqual(calls.map(c=>c.body.operation),['acquire','renew','release']);assert.equal(watch.leaseId,null);assert.equal(watch.enabled,false);
});
test('dispose disables monitoring and releases a known lease without a topic-scoped stop',async()=>{
  const{watch,calls}=leaseHarness();watch.setActive(true);await watch.sync(watchCapability);
  await watch.dispose();await watch.sync(watchCapability);
  assert.equal(watch.enabled,false);assert.equal(watch.active,false);assert.equal(watch.leaseId,null);
  assert.deepEqual(calls.map(c=>c.body.operation),['acquire','release']);assert.equal(calls.some(c=>c.body.stop),false);
});

function documentStub(t){
  const previous=Object.getOwnPropertyDescriptor(globalThis,'document');
  const document={hidden:false,getElementById:()=>({textContent:''})};
  Object.defineProperty(globalThis,'document',{configurable:true,writable:true,value:document});
  t.after(()=>{if(previous)Object.defineProperty(globalThis,'document',previous);else delete globalThis.document;});
  return document;
}
test('GraphExplorer preserves graph-wide monitoring across selection and degraded connection recovery',async t=>{
  documentStub(t);const{watch,calls}=leaseHarness(),explorer=Object.create(GraphExplorer.prototype);
  Object.assign(explorer,{disposed:false,visible:true,connected:true,data:graph(0),focus:'topic',liveWatch:watch,flowActivity:new TopicFlowActivity(),
    stopFlow(){},render(){},inspector(){},renderLiveStatus(){}});
  await explorer.syncLiveWatch();assert.equal(watch.leaseId,'lease-1');
  explorer.select('publisher');assert.equal(watch.leaseId,'lease-1');assert.equal(watch.enabled,true);assert.equal(calls.length,1);
  explorer.setConnected(false);await watch.sync();assert.equal(watch.leaseId,null);assert.equal(watch.enabled,true);
  explorer.setConnected(true);await explorer.syncLiveWatch();assert.equal(watch.leaseId,'lease-2');
  assert.deepEqual(calls.map(c=>c.body.operation),['acquire','release','acquire']);
});
test('GraphExplorer page/browser visibility pauses the lease without changing the Live flow toggle',async t=>{
  const document=documentStub(t),{watch,calls}=leaseHarness(),explorer=Object.create(GraphExplorer.prototype);
  Object.assign(explorer,{disposed:false,visible:true,connected:true,data:graph(0),liveWatch:watch,flowActivity:new TopicFlowActivity(),
    stopFlow(){},renderLiveStatus(){}});
  await explorer.syncLiveWatch();document.hidden=true;explorer.updateWatchActivity();await watch.sync();
  assert.equal(watch.leaseId,null);assert.equal(watch.enabled,true);
  document.hidden=false;await explorer.syncLiveWatch();assert.equal(watch.leaseId,'lease-2');
  explorer.setVisible(false);await watch.sync();assert.equal(watch.leaseId,null);assert.equal(watch.enabled,true);
  assert.equal(calls.every(c=>c.path==='/api/v1/ros/watch'),true);
});
test('activity badges keep unmeasured states explicit and distinguish feedback from historical goal status',()=>{
  assert.equal(activityLabel({kind:'topic',activity:{state:'unavailable'}}),'Unknown');
  assert.equal(activityLabel({kind:'topic',activity:{state:'inactive'}}),'Monitoring off');
  assert.equal(activityLabel({kind:'topic',activity:{state:'watching',messages:0,last_message_age_ms:null}}),'No recent data');
  assert.equal(activityLabel({kind:'action',activity:{state:'partial',feedback:{messages:3,last_message_age_ms:20}}}),'Feedback active');
  const historical=activityLabel({kind:'action',activity:{state:'watching',last_message_age_ms:9000,feedback:{last_message_age_ms:null},status:{status_summary:{counts:{executing:2}}}}});
  assert.match(historical,/last reported active/);
});

test('intervals report count deltas and actual snapshot duration for aggregate and matched GID sources',()=>{
  const owner=new TopicFlowActivity();assert.equal(owner.interval('topic'),null);
  owner.observe(graph(100),1000);assert.equal(owner.interval('topic'),null);assert.equal(owner.interval('topic::publisher:aa01'),null);
  owner.observe(graph(130),3500);
  assert.deepEqual(owner.interval('topic'),{messages:30,durationMs:2500});
  assert.deepEqual(owner.interval('topic::publisher:aa01'),{messages:30,durationMs:2500});
  owner.observe(graph(150,{counter:{publisher_counts:[{publisher_gid:'aa01',messages:135,last_message_age_ms:0}],unknown_publisher_messages:15}}),5500);
  assert.deepEqual(owner.interval('topic'),{messages:20,durationMs:2000});
  assert.deepEqual(owner.interval('topic::publisher:aa01'),{messages:5,durationMs:2000});
  assert.deepEqual(owner.interval('topic::unknown'),{messages:15,durationMs:2000});
});

test('zero-count intervals replace previous labels without renewing activity',()=>{
  const owner=new TopicFlowActivity();owner.observe(graph(10),0);owner.observe(graph(15),2000);
  assert.deepEqual(owner.interval('topic'),{messages:5,durationMs:2000});
  owner.observe(graph(15),3000);assert.deepEqual(owner.interval('topic'),{messages:0,durationMs:1000});
  owner.observe(graph(15),5000);assert.deepEqual(owner.interval('topic'),{messages:0,durationMs:2000});
  assert.deepEqual(owner.interval('topic::publisher:aa01'),{messages:0,durationMs:2000});
  assert.equal(owner.active('topic',5501),false);assert.equal(owner.active('topic::publisher:aa01',5501),false);
});

test('counter reset, new epoch and absent epoch leave interval unknown rather than showing a historic count',()=>{
  const owner=new TopicFlowActivity();owner.observe(graph(100),1000);owner.observe(graph(110),2000);
  assert.equal(owner.interval('topic').messages,10);
  owner.observe(graph(3),3000);assert.equal(owner.interval('topic'),null);assert.equal(owner.interval('topic::publisher:aa01'),null);
  owner.observe(graph(5),4000);assert.deepEqual(owner.interval('topic'),{messages:2,durationMs:1000});
  owner.observe(graph(900,{counter:{sample_id:'new-epoch'}}),5000);
  assert.equal(owner.interval('topic'),null);assert.equal(owner.interval('topic::publisher:aa01'),null);
  owner.observe(graph(902,{counter:{sample_id:'new-epoch'}}),6000);
  assert.deepEqual(owner.interval('topic'),{messages:2,durationMs:1000});
  owner.observe(graph(905,{counter:{sample_id:null}}),7000);
  assert.equal(owner.interval('topic'),null);assert.equal(owner.interval('topic::publisher:aa01'),null);
  owner.clear();assert.equal(owner.interval('topic'),null);
});

test('action response labels sum status and feedback on both server source and client aggregate',()=>{
  const owner=new TopicFlowActivity();owner.observe(actionGraph(20,5),1000);
  assert.equal(owner.interval('action'),null);assert.equal(owner.interval('action::server:server'),null);
  const current=actionGraph(25,12);owner.observe(current,3000);
  assert.deepEqual(owner.interval('action'),{messages:12,durationMs:2000});
  assert.deepEqual(owner.interval('action::server:server'),{messages:12,durationMs:2000});
  assert.deepEqual(owner.interval('status-topic::publisher:cc01'),{messages:5,durationMs:2000});
  assert.deepEqual(owner.interval('feedback-topic::publisher:cc02'),{messages:7,durationMs:2000});
  const edges=flowEdges(current,current.edges,owner,3000,{focus:'action',focusedOnly:true});
  const server=edges.find(e=>e.id==='action-server'),client=edges.find(e=>e.id==='action-client');
  assert.equal(server.sourceKey,'action::server:server');assert.equal(server.hollow,false);
  assert.equal(client.sourceKey,'action');assert.equal(client.hollow,true);
  assert.deepEqual(owner.interval(server.sourceKey),owner.interval(client.sourceKey));
});

function multiServerActionGraph(statusA,statusB,feedbackA,feedbackB){
  const g=actionGraph(statusA+statusB,feedbackA+feedbackB,{
    statusExtra:{publisher_counts:[{publisher_gid:'cc01',messages:statusA,last_message_age_ms:0},{publisher_gid:'dd01',messages:statusB,last_message_age_ms:0}]},
    feedbackExtra:{publisher_counts:[{publisher_gid:'cc02',messages:feedbackA,last_message_age_ms:0},{publisher_gid:'dd02',messages:feedbackB,last_message_age_ms:0}]},
  });
  g.nodes.push(node('second-server'));
  g.topics[0].publishers.push({id:'publisher:dd01',gid:'dd01',node_id:'second-server'});
  g.topics[1].publishers.push({id:'publisher:dd02',gid:'dd02',node_id:'second-server'});
  g.actions[0].servers.push('second-server');
  g.edges.push({id:'status-pub-second',kind:'publish',from:'second-server',to:'status-topic',endpoint_id:'publisher:dd01'},
    {id:'feedback-pub-second',kind:'publish',from:'second-server',to:'feedback-topic',endpoint_id:'publisher:dd02'},
    {id:'action-server-second',kind:'action_server',from:'action',to:'second-server'});
  return normalizeGraph(g);
}

test('each action-server interval contains only its own matched status/feedback publisher deltas',()=>{
  const owner=new TopicFlowActivity();owner.observe(multiServerActionGraph(10,40,5,15),1000);
  const current=multiServerActionGraph(13,44,9,20);owner.observe(current,3000);
  assert.deepEqual(owner.interval('action::server:server'),{messages:7,durationMs:2000});
  assert.deepEqual(owner.interval('action::server:second-server'),{messages:9,durationMs:2000});
  assert.deepEqual(owner.interval('action'),{messages:16,durationMs:2000});
  const edges=flowEdges(current,current.edges,owner,3000,{focus:'action',focusedOnly:true});
  assert.equal(edges.find(e=>e.id==='action-server').sourceKey,'action::server:server');
  assert.equal(edges.find(e=>e.id==='action-server-second').sourceKey,'action::server:second-server');
});

test('partial action source intervals sum only available real counters',()=>{
  const options={state:'partial',statusExtra:{state:'unavailable',messages:null,publisher_counts:[],last_message_age_ms:null}};
  const owner=new TopicFlowActivity();owner.observe(actionGraph(0,10,options),1000);owner.observe(actionGraph(0,13,options),3000);
  assert.deepEqual(owner.interval('action'),{messages:3,durationMs:2000});
  assert.deepEqual(owner.interval('action::server:server'),{messages:3,durationMs:2000});
  assert.equal(owner.interval('status-topic::publisher:cc01'),null);
});

test('a sole logical publisher with transport-adapted GID keeps an explicitly inferred route',()=>{
  const owner=new TopicFlowActivity();
  const make=count=>graph(count,{counter:{publisher_counts:[{publisher_gid:'transport-gid',messages:count,last_message_age_ms:0}],unknown_publisher_messages:0}});
  owner.observe(make(10),0);const current=make(12);owner.observe(current,2000);
  const pub=flowEdges(current,current.edges,owner,2000,{focus:'topic',focusedOnly:true}).find(e=>e.kind==='publish');
  assert.equal(pub.hollow,true);assert.equal(pub.sourceKey,'topic');
  assert.equal(pub.attribution,'single_discovered_publisher');assert.equal(owner.interval(pub.sourceKey).messages,2);
});

test('a different action server cannot inherit the sole publisher of another server',()=>{
  const owner=new TopicFlowActivity();
  const make=count=>{const g=actionGraph(count,count);return normalizeGraph({...g,
    nodes:[...g.nodes,node('other-server')],
    actions:g.actions.map(a=>({...a,servers:['server','other-server']})),
    edges:[...g.edges,{id:'other-action-server',kind:'action_server',from:'action',to:'other-server'}]});};
  owner.observe(make(10),0);const current=make(12);owner.observe(current,2000);
  const edges=flowEdges(current,current.edges,owner,2000,{focus:'action',focusedOnly:true});
  assert.ok(edges.some(e=>e.id==='action-server'));assert.equal(edges.some(e=>e.id==='other-action-server'),false);
});

test('graph update selector accepts only its declared intervals and defaults invalid values to2s',()=>{
  assert.deepEqual(GRAPH_UPDATE_INTERVALS,[500,1000,2000,5000,10000]);
  for(const interval of GRAPH_UPDATE_INTERVALS){
    assert.equal(normalizeUpdateInterval(interval),interval);
    assert.equal(normalizeUpdateInterval(String(interval)),interval);
  }
  for(const invalid of [undefined,null,'',0,-500,750,2001,10000.5,NaN,Infinity,'1s','automatic'])assert.equal(normalizeUpdateInterval(invalid),2000);
});

test('5s and10s display intervals sustain advancing evidence but still expire a frozen source',()=>{
  for(const interval of [5000,10000]){
    const owner=new TopicFlowActivity({updateIntervalMs:interval});owner.observe(graph(0),0);
    let last=0;
    for(let now=interval;now<=interval*5;now+=interval){
      if(last)assert.equal(owner.active('topic',now-1),true,`${interval}ms gap`);
      owner.observe(graph(now/10),now);last=now;
      assert.equal(owner.active('topic',now+interval-1),true);
      assert.equal(owner.active('topic::publisher:aa01',now+interval-1),true);
    }
    owner.observe(graph(last/10),last+interval/2);
    assert.equal(owner.active('topic',last+interval+1499),true);
    assert.equal(owner.active('topic',last+interval+1501),false);
  }
});

test('changing update interval alone or replaying a frozen counter cannot create fresh evidence',()=>{
  const{owner,now}=live();owner.setUpdateInterval(10000);
  assert.equal(owner.active('topic',now+3501),false);
  owner.observe(graph(11),now+4000);assert.equal(owner.active('topic',now+4000),false);
  owner.observe(graph(12),now+5000);assert.equal(owner.active('topic',now+15000),true);
  assert.equal(owner.active('topic',now+16501),false);
});

const CLOCK_A='11111111111141118111111111111111',CLOCK_B='22222222222242228222222222222222';
const clockFields=(ns,id=CLOCK_A,type='observer_monotonic')=>({activity_clock_id:id,activity_clock_type:type,activity_captured_at_ns:String(ns)});
const withClock=(g,ns,id=CLOCK_A,type='observer_monotonic')=>({...g,...clockFields(ns,id,type)});
const unknownWindow=value=>assert.ok(value===null||value?.durationMs===null,`expected unknown observation window, got ${JSON.stringify(value)}`);

test('server monotonic BigInt subtraction measures the true interval despite browser response jitter',()=>{
  const owner=new TopicFlowActivity(),base=9007199254740993n;
  owner.observe(withClock(graph(100),base),1000);
  owner.observe(withClock(graph(120),base+2000000001n),9500);
  assert.equal(owner.interval('topic').messages,20);
  near(owner.interval('topic').durationMs,2000.000001);
  near(owner.interval('topic::publisher:aa01').durationMs,2000.000001);
  assert.notEqual(owner.interval('topic').durationMs,8500);
});

test('action aggregate and server-source count labels share the observer clock window',()=>{
  const owner=new TopicFlowActivity(),base=9007199254740993n;
  owner.observe(withClock(actionGraph(20,5),base),10);
  owner.observe(withClock(actionGraph(25,12),base+2500000000n),10010);
  assert.deepEqual(owner.interval('action'),{messages:12,durationMs:2500});
  assert.deepEqual(owner.interval('action::server:server'),{messages:12,durationMs:2500});
});

test('server clock restart and independent counter epoch changes re-prime the observation window',()=>{
  const owner=new TopicFlowActivity();owner.observe(withClock(graph(10),1000000000n),0);
  owner.observe(withClock(graph(12),3000000000n),3000);assert.equal(owner.interval('topic').durationMs,2000);
  owner.observe(withClock(graph(15),1000000000n,CLOCK_B),4000);unknownWindow(owner.interval('topic'));
  owner.observe(withClock(graph(17),2000000000n,CLOCK_B),6000);assert.deepEqual(owner.interval('topic'),{messages:2,durationMs:1000});
  owner.observe(withClock(graph(300,{counter:{sample_id:'restarted-counter'}}),4000000000n,CLOCK_B),8000);
  unknownWindow(owner.interval('topic'));unknownWindow(owner.interval('topic::publisher:aa01'));
});

test('invalid or partially present server timing never falls back to a fabricated browser window',()=>{
  const invalidFields=[
    {activity_captured_at_ns:null},{activity_captured_at_ns:1000000000},
    {activity_captured_at_ns:'1e9'},{activity_captured_at_ns:'-1'},{activity_captured_at_ns:''},{activity_captured_at_ns:'NaN'},
    {activity_clock_id:null},{activity_clock_id:''},{activity_clock_type:undefined},
    {activity_clock_type:'wall'},{activity_clock_type:'unix'},{activity_clock_type:'system_wall'},
    {activity_clock_id:null,activity_clock_type:null,activity_captured_at_ns:null},
  ];
  for(const invalid of invalidFields){
    const owner=new TopicFlowActivity();owner.observe(withClock(graph(10),1000000000n),1000);
    const next={...withClock(graph(12),3000000000n),...invalid};owner.observe(next,3500);
    unknownWindow(owner.interval('topic'));unknownWindow(owner.interval('topic::publisher:aa01'));
  }
});

test('equal or backwards observer timestamps produce no fabricated nonnegative interval',()=>{
  for(const timestamp of [2000000000n,1000000000n]){
    const owner=new TopicFlowActivity();owner.observe(withClock(graph(10),2000000000n),1000);
    owner.observe(withClock(graph(12),timestamp),4000);unknownWindow(owner.interval('topic'));
  }
});

test('legacy snapshots lacking every observer clock field retain the client-monotonic fallback',()=>{
  const owner=new TopicFlowActivity();owner.observe(graph(10),1000);owner.observe(graph(13),2750);
  assert.deepEqual(owner.interval('topic'),{messages:3,durationMs:1750});
  owner.observe(withClock(graph(16),9000000000n),4000);unknownWindow(owner.interval('topic'));
});

test('structural signature ignores new measurements/clocks and discovery ordering, but detects endpoint changes',()=>{
  const initial=graph(10),updated=withClock(graph(20),9007199254740993n);
  updated.age_ms=2000;updated.watch.lease_count=4;updated.nodes.reverse();updated.edges.reverse();
  updated.topics[0].traffic={state:'complete',messages:500};
  assert.equal(graphStructureSignature(initial),graphStructureSignature(updated));
  updated.topics[0].publishers[0].gid='changed-gid';
  assert.notEqual(graphStructureSignature(initial),graphStructureSignature(updated));
});

test('counter-only refresh preserves graph/inspector controls while topology or view changes rebuild them',async t=>{
  documentStub(t);const initial=graph(10);let next=graph(11),key='initial-view';
  const owner=Object.create(GraphExplorer.prototype),svgToken={},buttonToken={};
  Object.assign(owner,{disposed:false,pending:false,connected:true,visible:true,data:initial,focus:'topic',initialized:true,
    liveWatch:{enabled:true},flowActivity:new TopicFlowActivity(),renderedSignature:graphStructureSignature(initial),renderedViewKey:key,
    fullRenders:0,inspectorRenders:0,measurements:0,svgToken,buttonToken,
    request:async()=>next,viewKey:()=>key,syncLiveWatch:async()=>{},
    updateMeasurements(){this.measurements++;},
    render(){this.fullRenders++;this.renderedSignature=graphStructureSignature(this.data);this.renderedViewKey=key;this.svgToken={};},
    inspector(){this.inspectorRenders++;this.buttonToken={};},
    stopFlow(){},message(){},
  });
  await owner.refresh();assert.equal(owner.measurements,1);assert.equal(owner.fullRenders,0);assert.equal(owner.inspectorRenders,0);
  assert.equal(owner.svgToken,svgToken);assert.equal(owner.buttonToken,buttonToken);
  next=graph(12);next.topics[0].name='/renamed';await owner.refresh();
  assert.equal(owner.fullRenders,1);assert.equal(owner.inspectorRenders,1);
  key='different-focus';await owner.refresh();assert.equal(owner.fullRenders,2);assert.equal(owner.inspectorRenders,2);
});

function schedulerBrowser(t){
  const elements=new Map(),stored=new Map(),intervals=new Map();let nextId=0;
  const element=()=>({children:[],attrs:{},clientWidth:1000,clientHeight:700,
    append(...children){this.children.push(...children);},setAttribute(key,value){this.attrs[key]=value;},
    replaceChildren(...children){this.children=children;},querySelector(){return null;},remove(){}});
  const document={hidden:false,getElementById:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);},
    createElementNS:()=>element(),addEventListener(){},removeEventListener(){}};
  for(const [name,value]of Object.entries({document,window:{matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}})},
    localStorage:{getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,value)},
    ResizeObserver:class{observe(){}disconnect(){}},
  })){
    const previous=Object.getOwnPropertyDescriptor(globalThis,name);Object.defineProperty(globalThis,name,{value,writable:true,configurable:true});
    t.after(()=>{if(previous)Object.defineProperty(globalThis,name,previous);else delete globalThis[name];});
  }
  t.mock.method(globalThis,'setInterval',(callback,ms)=>{const id=++nextId;intervals.set(id,{callback,ms});return id;});
  t.mock.method(globalThis,'clearInterval',id=>intervals.delete(id));
  return{document,elements,stored,intervals};
}

test('10s display polling keeps independent2s lease heartbeats and changing interval replaces only display timer',async t=>{
  const browser=schedulerBrowser(t);let now=0;const calls=[],displayPolls=[];
  class SchedulerExplorer extends GraphExplorer{bind(){}}
  const owner=new SchedulerExplorer({request:async(path,body)=>{
    calls.push({path,body:{...body},at:now});return{accepted:true,lease_id:body.lease_id||'stable',expires_in_ms:body.operation==='release'?0:12000,active:body.operation!=='release'};
  },onError(){}});
  const heartbeat=owner.watchTimer,originalDisplay=owner.timer,model=owner.flowActivity;
  assert.equal(browser.intervals.get(heartbeat).ms,2000);
  owner.setUpdateInterval('10000');assert.equal(owner.flowActivity,model);
  assert.equal(browser.intervals.has(originalDisplay),false);assert.equal(owner.watchTimer,heartbeat);
  assert.equal(browser.intervals.get(owner.timer).ms,10000);assert.equal(browser.intervals.get(heartbeat).ms,2000);
  assert.equal(browser.stored.get('ros-graph-update-ms'),'10000');assert.equal(browser.elements.get('graph-update-interval').value,'10000');
  owner.visible=true;owner.connected=true;owner.data=graph(0);owner.liveWatch.now=()=>now;
  owner.refresh=async()=>{displayPolls.push(now);};await owner.syncLiveWatch();
  for(now=2000;now<=20000;now+=2000){
    browser.intervals.get(heartbeat).callback();await owner.liveWatch.pending;
    if(now%10000===0)browser.intervals.get(owner.timer).callback();
  }
  assert.deepEqual(displayPolls,[10000,20000]);assert.equal(calls.filter(c=>c.body.operation==='acquire').length,1);
  assert.deepEqual(calls.filter(c=>c.body.operation==='renew').map(c=>c.at),[4000,8000,12000,16000,20000]);
  owner.dispose();await owner.liveWatch.sync();assert.equal(browser.intervals.size,0);
  assert.equal(calls.at(-1).body.operation,'release');
});
