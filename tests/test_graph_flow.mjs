import test from 'node:test';
import assert from 'node:assert/strict';
import {
  FLOW_LIMITS, TopicFlowActivity, cubicPoint, flowEdges, normalizeGraph, GraphLiveWatch, GraphExplorer, activityLabel,
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
  for(const e of chosen.filter(e=>e.id==='action-server')){assert.equal(e.hollow,false);assert.ok(['status-topic::publisher:cc01','feedback-topic::publisher:cc02'].includes(e.sourceKey));}
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
