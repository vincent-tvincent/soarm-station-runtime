import test from 'node:test';
import assert from 'node:assert/strict';
import {FLOW_LIMITS, TopicFlowActivity, flowEdges, normalizeGraph, selectGraphView} from '../web/graph.js';

const phases=['request_sent','request_received','response_sent','response_received'];
const node=id=>({id,name:id,fully_qualified_name:`/${id}`,namespace:'/',instance_count:1});
const metric=(messages,epoch,extra={})=>({state:'watching',source:'ros_lttng',sample_id:epoch,messages,
  last_message_age_ms:messages?0:null,rate_hz:messages?1:0,...extra});
const endpoint=(id,nodeId,role,counts,extra={})=>({id,node_id:nodeId,role,
  channels:Object.fromEntries(Object.entries(counts).map(([phase,count])=>
    [phase,metric(count,`${id}:${phase}`,extra[phase]||{})]))});
function rpcActivity(endpoints,{epoch='service-epoch',...extra}={}) {
  const channels=Object.fromEntries(phases.map(phase=>{
    const values=endpoints.map(e=>e.channels[phase]).filter(Boolean);
    return[phase,metric(values.reduce((sum,value)=>sum+value.messages,0),`${epoch}:${phase}`)];
  }));
  return{...metric(channels.request_received.messages+channels.response_sent.messages,epoch),
    channels,endpoints,measurement_scope:'traced_service_endpoints',...extra};
}
function serviceGraph(count=10,{endpoints,activity={},snapshot={}}={}) {
  const measured=endpoints??[
    endpoint('client-handle','client','client',{request_sent:count,response_received:count}),
    endpoint('server-handle','server','server',{request_received:count,response_sent:count}),
  ];
  return normalizeGraph({schema_version:1,state:'ready',age_ms:0,nodes:['client','server','other-client','other-server'].map(node),
    topics:[],actions:[],services:[{id:'service',name:'/inspect',types:['example/srv/Inspect'],
      clients:['client','other-client'],servers:['server','other-server'],activity:rpcActivity(measured,activity)}],
    edges:[{id:'client-edge',from:'client',to:'service',kind:'service_client'},
      {id:'server-edge',from:'service',to:'server',kind:'service_server'},
      {id:'other-client-edge',from:'other-client',to:'service',kind:'service_client'},
      {id:'other-server-edge',from:'service',to:'other-server',kind:'service_server'}],...snapshot});
}
const baseEdge=marker=>marker.edgeId??marker.id;
const directions=markers=>markers.map(m=>`${baseEdge(m)}:${m.phase}:${m.reverse}`).sort();
function advance(make=serviceGraph,first=10,next=11) {
  const owner=new TopicFlowActivity();owner.observe(make(first),0);
  const graph=make(next);owner.observe(graph,1000);
  return{owner,graph,markers:flowEdges(graph,graph.edges,owner,1000,{})};
}

test('service request/reply markers follow observed endpoint direction with unique IDs on each base edge',()=>{
  const{owner,graph,markers}=advance();
  assert.deepEqual(directions(markers),[
    'client-edge:request_sent:false','client-edge:response_received:true',
    'server-edge:request_received:false','server-edge:response_sent:true',
  ]);
  assert.equal(new Set(markers.map(m=>m.id)).size,4);
  assert.equal(graph.edges.length,4);
  for(const marker of markers) {
    assert.equal(marker.attribution,'traced_rpc_endpoint');assert.equal(marker.hollow,false);
    assert.ok(marker.edgeId);assert.deepEqual(owner.interval(marker.sourceKey),{messages:1,durationMs:1000});
  }
  assert.deepEqual(owner.interval('service'),{messages:2,durationMs:1000});
});

test('service headline counts server handling while directional markers retain their own measured counts',()=>{
  const make=step=>serviceGraph(0,{endpoints:[
    endpoint('client-handle','client','client',{request_sent:10+20*step,response_received:10+15*step}),
    endpoint('server-handle','server','server',{request_received:10+step,response_sent:10+2*step}),
  ]});
  const{owner,markers}=advance(make,0,1);
  assert.equal(owner.interval('service').messages,3);
  const expected={request_sent:20,response_received:15,request_received:1,response_sent:2};
  for(const marker of markers)assert.equal(owner.interval(marker.sourceKey).messages,expected[marker.phase]);
});

test('aggregate service activity cannot be assigned to unmeasured clients or servers',()=>{
  const make=count=>serviceGraph(count,{endpoints:[],activity:{...metric(count*2,'aggregate'),
    channels:Object.fromEntries(phases.map(phase=>[phase,metric(count,`aggregate:${phase}`)]))}});
  const{owner,graph,markers}=advance(make);
  assert.equal(owner.active('service',1000),true);assert.deepEqual(markers,[]);
  assert.equal(graph.services[0].activity.messages,22);
});

test('only the endpoint whose counter advances receives a marker, even when peers share the interface',()=>{
  const make=count=>serviceGraph(0,{endpoints:[
    endpoint('client-handle','client','client',{request_sent:count,response_received:10}),
    endpoint('other-client-handle','other-client','client',{request_sent:10,response_received:10}),
    endpoint('server-handle','server','server',{request_received:10,response_sent:10}),
    endpoint('other-server-handle','other-server','server',{request_received:10,response_sent:10}),
  ]});
  const{markers}=advance(make);
  assert.deepEqual(directions(markers),['client-edge:request_sent:false']);
});

test('multiple handles for one named node combine counts without endpoint ordering changing the epoch',()=>{
  const make=step=>{
    const endpoints=[endpoint('handle-a','client','client',{request_sent:10+2*step}),
      endpoint('handle-b','client','client',{request_sent:20+3*step})];
    if(step)endpoints.reverse();
    return serviceGraph(0,{endpoints});
  };
  const{owner,markers}=advance(make,0,1);
  assert.deepEqual(directions(markers),['client-edge:request_sent:false']);
  assert.deepEqual(owner.interval(markers[0].sourceKey),{messages:5,durationMs:1000});
});

test('replacement of one handle epoch primes the combined node count rather than replaying history',()=>{
  const make=(count,epoch='first')=>serviceGraph(0,{endpoints:[
    endpoint('handle-a','client','client',{request_sent:count},{request_sent:{sample_id:epoch}}),
    endpoint('handle-b','client','client',{request_sent:20}),
  ]});
  const owner=new TopicFlowActivity();owner.observe(make(10),0);
  let current=make(11);owner.observe(current,1000);
  assert.equal(flowEdges(current,current.edges,owner,1000,{}).length,1);
  current=make(900,'restarted');owner.observe(current,1100);
  assert.deepEqual(flowEdges(current,current.edges,owner,1100,{}),[]);
  current=make(901,'restarted');owner.observe(current,2100);
  const markers=flowEdges(current,current.edges,owner,2100,{});
  assert.equal(markers.length,1);assert.equal(owner.interval(markers[0].sourceKey).messages,1);
});

test('missing phases are unknown; aggregate channels never fill an endpoint phase that was not measured',()=>{
  const make=count=>serviceGraph(0,{endpoints:[endpoint('client-handle','client','client',{response_received:count})],
    activity:{channels:Object.fromEntries(phases.map(phase=>[phase,metric(count,`aggregate:${phase}`)]))}});
  const{markers}=advance(make);
  assert.deepEqual(directions(markers),['client-edge:response_received:true']);
});

test('historical counts, zero deltas, stale snapshots, and stale endpoint receipts do not invent RPC flow',()=>{
  const owner=new TopicFlowActivity();let graph=serviceGraph(900);
  owner.observe(graph,0);assert.deepEqual(flowEdges(graph,graph.edges,owner,0,{}),[]);
  graph=serviceGraph(901);owner.observe(graph,1000);assert.equal(flowEdges(graph,graph.edges,owner,1000,{}).length,4);
  for(const now of [2000,3000,4000,5000])owner.observe(graph,now);
  assert.deepEqual(flowEdges(graph,graph.edges,owner,5000,{}),[]);
  graph=serviceGraph(902,{snapshot:{age_ms:FLOW_LIMITS.staleMs+1}});owner.observe(graph,5100);
  assert.deepEqual(flowEdges(graph,graph.edges,owner,5100,{}),[]);
  for(const extra of [{last_message_age_ms:FLOW_LIMITS.evidenceMs+1},{state:'unavailable'},
    {sample_id:null},{messages:-1},{messages:NaN},{messages:null}]) {
    const local=new TopicFlowActivity(),make=count=>serviceGraph(0,{endpoints:[
      endpoint('client-handle','client','client',{request_sent:count},{request_sent:count===11?extra:{}}),
    ]});
    local.observe(make(10),0);const invalid=make(11);local.observe(invalid,1000);
    assert.deepEqual(flowEdges(invalid,invalid.edges,local,1000,{}),[],JSON.stringify(extra));
  }
});

function actionGraph(step,{feedback=true,parts=['goal'],withService=false}={}) {
  const aName='/execute',topicMetric=(name,gid)=>({...metric(10+(feedback?step:0),name),source:'ros_subscription',
    publisher_counts:[{publisher_gid:gid,messages:10+(feedback?step:0),last_message_age_ms:0}],unknown_publisher_messages:0});
  const status=topicMetric('status-epoch','status-gid'),feedbackActivity=topicMetric('feedback-epoch','feedback-gid');
  const rpc=Object.fromEntries(['goal','cancel','result'].map(part=>[part,rpcActivity([
    endpoint(`${part}-client`,'client','client',{request_sent:10+(parts.includes(part)?step:0),response_received:10+(parts.includes(part)?step:0)}),
    endpoint(`${part}-server`,'server','server',{request_received:10+(parts.includes(part)?step:0),response_sent:10+(parts.includes(part)?step:0)}),
  ],{epoch:`${part}-epoch`})]));
  const underlying={goal:'send_goal',cancel:'cancel_goal',result:'get_result'};
  const extra=withService?serviceGraph(10+step):null;
  return normalizeGraph({schema_version:1,state:'ready',age_ms:0,
    nodes:['client','server','other-client','other-server'].map(node),
    topics:[{id:'status',name:aName+'/_action/status',types:['action_msgs/msg/GoalStatusArray'],
      publishers:[{id:'status-publisher',node_id:'server',gid:'status-gid'}],activity:status},
    {id:'feedback',name:aName+'/_action/feedback',types:['example/action/Run_FeedbackMessage'],
      publishers:[{id:'feedback-publisher',node_id:'server',gid:'feedback-gid'}],activity:feedbackActivity}],
    services:[...Object.entries(rpc).map(([part,activity])=>({id:`rpc-${part}`,name:`${aName}/_action/${underlying[part]}`,
      types:[`example/srv/${part}`],clients:['client'],servers:['server'],activity})),...(extra?.services||[])],
    actions:[{id:'action',name:aName,types:['example/action/Run'],clients:['client','other-client'],servers:['server'],activity:{
      state:'watching',status_topic:aName+'/_action/status',feedback_topic:aName+'/_action/feedback',status,feedback:feedbackActivity,
      messages:status.messages+feedbackActivity.messages,status_messages:status.messages,feedback_messages:feedbackActivity.messages,
      last_message_age_ms:0,rate_hz:1,rpc,display:metric(status.messages+feedbackActivity.messages+
        Object.values(rpc).reduce((sum,r)=>sum+r.messages,0),'action-display'),
    }}],
    edges:[{id:'status-pub',from:'server',to:'status',kind:'publish',endpoint_id:'status-publisher'},
      {id:'feedback-pub',from:'server',to:'feedback',kind:'publish',endpoint_id:'feedback-publisher'},
      {id:'action-client',from:'client',to:'action',kind:'action_client'},
      {id:'action-other-client',from:'other-client',to:'action',kind:'action_client'},
      {id:'action-server',from:'action',to:'server',kind:'action_server'},...(extra?.edges||[])],
  });
}

test('action goal/cancel/result request and reply markers coexist with observed status/feedback routes',()=>{
  const{owner,graph}=advance(step=>actionGraph(step,{parts:['goal','cancel','result']}),0,1);
  const edges=graph.edges.filter(e=>e.kind.startsWith('action'));
  const markers=flowEdges(graph,edges,owner,1000,{focus:'action',focusedOnly:true});
  const rpc=markers.filter(m=>m.attribution==='traced_rpc_endpoint');
  assert.equal(rpc.length,12);
  for(const part of ['goal','cancel','result'])assert.deepEqual(directions(rpc.filter(m=>m.rpc===part)),[
    'action-client:request_sent:false','action-client:response_received:true',
    'action-server:request_received:false','action-server:response_sent:true',
  ]);
  const existing=markers.filter(m=>m.attribution!=='traced_rpc_endpoint');
  assert.ok(existing.some(m=>baseEdge(m)==='action-server' && m.reverse));
  assert.ok(existing.some(m=>baseEdge(m)==='action-client' && m.reverse));
  assert.equal(new Set(markers.map(m=>m.id)).size,markers.length);
  assert.equal(rpc.some(m=>baseEdge(m)==='action-other-client'),false);
});

test('action RPC works without feedback increments or a separately discovered underlying service entity',()=>{
  const make=step=>{const graph=actionGraph(step,{feedback:false});graph.services=[];return normalizeGraph(graph);};
  const{owner,graph}=advance(make,0,1);
  const markers=flowEdges(graph,graph.edges.filter(e=>e.kind.startsWith('action')),owner,1000,{});
  assert.equal(markers.length,4);assert.ok(markers.every(m=>m.rpc==='goal' && m.attribution==='traced_rpc_endpoint'));
});

test('focused RPC markers honor selected interface and type visibility without suppressing observation',()=>{
  const{owner,graph}=advance(step=>actionGraph(step,{withService:true}),0,1);
  let view=selectGraphView(graph,{interfaceVisibility:{topics:false,services:false,actions:true}});
  let markers=flowEdges(graph,view.edges,owner,1000,{focus:'action',focusedOnly:true});
  assert.ok(markers.some(m=>m.attribution==='traced_rpc_endpoint'));
  assert.ok(markers.every(m=>m.kind.startsWith('action')));
  view=selectGraphView(graph,{interfaceVisibility:{topics:false,services:true,actions:false}});
  markers=flowEdges(graph,view.edges,owner,1000,{focus:'service',focusedOnly:true});
  assert.equal(markers.length,4);assert.ok(markers.every(m=>m.kind.startsWith('service')));
  assert.deepEqual(flowEdges(graph,view.edges,owner,1000,{focus:null,focusedOnly:true}),[]);
  assert.equal(owner.active('service',1000),true);
  const hidden=selectGraphView(graph,{interfaceVisibility:{topics:false,services:false,actions:false}});
  assert.deepEqual(flowEdges(graph,hidden.edges,owner,1000,{focus:'client',focusedOnly:true}),[]);
  assert.equal(owner.active('service',1000),true);
});
