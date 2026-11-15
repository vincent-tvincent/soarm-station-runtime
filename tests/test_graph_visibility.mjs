import test from 'node:test';
import assert from 'node:assert/strict';
import {GRAPH_INTERFACE_VISIBILITY_KEY, normalizeInterfaceVisibility, normalizeGraph,
  selectGraphView, selectionPaths, GraphExplorer} from '../web/graph.js';

const all={topics:true,actions:true,services:true};
const none={topics:false,actions:false,services:false};
const ids=items=>items.map(item=>item.id).sort();
const raw=()=>({schema_version:1,nodes:['producer','consumer','server','idle'].map(id=>({id,name:id,fully_qualified_name:'/'+id})),
  topics:[{id:'topic',name:'/observations',types:['example/msg/Observation'],activity:{messages:42}},
    {id:'log',name:'/rosout',types:['rcl_interfaces/msg/Log']}],
  actions:[{id:'action',name:'/execute',types:['example/action/Execute']}],
  services:[{id:'service',name:'/inspect',types:['example/srv/Inspect']}],
  edges:[{id:'pub',from:'producer',to:'topic',kind:'publish'},
    {id:'sub',from:'topic',to:'consumer',kind:'subscribe'},
    {id:'action-client',from:'consumer',to:'action',kind:'action_client'},
    {id:'action-server',from:'action',to:'server',kind:'action_server'},
    {id:'service-client',from:'idle',to:'service',kind:'service_client'},
    {id:'service-server',from:'service',to:'server',kind:'service_server'},
    {id:'logger',from:'producer',to:'log',kind:'publish'}]});

function browser(t,{storageError=false}={}) {
  const element=()=>({value:'',checked:false,hidden:false,children:[],attributes:{},
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;},
    get lastElementChild(){return this.children.at(-1);},setAttribute(key,value){this.attributes[key]=String(value);}});
  const elements=new Map(),saved=new Map();
  const document={hidden:false,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
    createElement:element};
  const storage={getItem:key=>saved.get(key)??null,setItem(key,value){if(storageError)throw Error('Storage unavailable');saved.set(key,value);}};
  for(const [key,value] of Object.entries({document,localStorage:storage})) {
    const old=Object.getOwnPropertyDescriptor(globalThis,key);
    Object.defineProperty(globalThis,key,{value,configurable:true,writable:true});
    t.after(()=>{if(old)Object.defineProperty(globalThis,key,old);else delete globalThis[key];});
  }
  return{document,elements,saved};
}
function explorer(overrides={}) {
  return Object.assign(Object.create(GraphExplorer.prototype),{
    data:normalizeGraph(raw()),interfaceVisibility:{...all},scope:'all',focus:'topic',isolatedId:null,
    savedOverview:null,didFit:true,disposed:false,interfaceKind:'topics',transform:{x:10,y:20,k:.8},
    renderCalls:0,inspectorCalls:0,
    render(){this.renderCalls++;this.lastVisible=this.visibleData();},inspector(){this.inspectorCalls++;},
    flowActivity:{clear(){throw Error('Visibility must not clear measured counters');}},
    liveWatch:{setActive(){throw Error('Visibility must not change observation leases');},
      setEnabled(){throw Error('Visibility must not change monitoring intent');}},
    ...overrides,
  });
}

test('visibility preferences accept actual booleans and default each missing or invalid value to true',()=>{
  for(const value of [undefined,null,'','broken','null','false','[]',[],2])assert.deepEqual(normalizeInterfaceVisibility(value),all);
  assert.deepEqual(normalizeInterfaceVisibility('{"topics":false,"actions":"false","services":0}'),
    {topics:false,actions:true,services:true});
  assert.deepEqual(normalizeInterfaceVisibility({actions:false,extra:false}),{topics:true,actions:false,services:true});
  assert.deepEqual(normalizeInterfaceVisibility(JSON.stringify(none)),none);
});

test('each type hides independently with its edges, including highlighted peer paths',()=>{
  const graph=normalizeGraph(raw()),unchanged=JSON.stringify(graph);
  for(const [kind,entity,edges] of [
    ['topics','topic',['pub','sub']],['actions','action',['action-client','action-server']],
    ['services','service',['service-client','service-server']],
  ]) {
    const view=selectGraphView(graph,{interfaceVisibility:{[kind]:false}}),visible=new Set(ids(view.entities));
    assert.equal(visible.has(entity),false);
    for(const other of ['topic','action','service'].filter(id=>id!==entity))assert.equal(visible.has(other),true);
    for(const edge of edges)assert.equal(ids(view.edges).includes(edge),false);
    for(const edge of selectionPaths(graph,'consumer',view.edges).highlighted)assert.ok(ids(view.edges).includes(edge));
  }
  assert.equal(JSON.stringify(graph),unchanged);
});

test('all types off retains unique named nodes, including nodes made isolated by the filter',()=>{
  const graph=normalizeGraph(raw());
  const view=selectGraphView(graph,{interfaceVisibility:none,showInfrastructure:true});
  assert.deepEqual(ids(view.entities),['consumer','idle','producer','server']);
  assert.deepEqual(view.edges,[]);assert.equal(new Set(ids(view.entities)).size,4);
  const focused=selectGraphView(graph,{interfaceVisibility:none,focus:'producer',scope:'focus'});
  assert.deepEqual(ids(focused.entities),['producer']);
});

test('unresolved placeholders disappear when their only type is hidden even with infrastructure shown',()=>{
  const source=raw();source.nodes.push({id:'unknown',name:null,discovery_state:'unresolved_endpoint_owner'});
  source.edges.push({id:'unknown-pub',from:'unknown',to:'topic',kind:'publish'});
  const graph=normalizeGraph(source);
  assert.ok(ids(selectGraphView(graph,{showInfrastructure:true}).entities).includes('unknown'));
  for(const showInfrastructure of [false,true]) {
    const view=selectGraphView(graph,{interfaceVisibility:{topics:false},showInfrastructure});
    assert.equal(ids(view.entities).includes('unknown'),false);
    assert.ok(ids(view.entities).includes('producer'));
  }
});

test('hiding a selected isolated interface clears selection and exits isolation without touching monitoring',t=>{
  const ui=browser(t),owner=explorer({focus:'action',scope:'focus',isolatedId:'action',savedOverview:{x:1}});
  const data=owner.data,activity=owner.flowActivity,watch=owner.liveWatch,before=JSON.stringify(data),oldKey=owner.viewKey();
  assert.equal(owner.setInterfaceVisible('actions',false),true);
  assert.equal(owner.focus,null);assert.equal(owner.scope,'all');assert.equal(owner.isolatedId,null);
  assert.equal(owner.savedOverview,null);assert.equal(ui.document.getElementById('graph-exit').hidden,true);
  assert.equal(ui.document.getElementById('graph-show-actions').checked,false);
  assert.equal(ui.document.getElementById('graph-show-topics').checked,true);
  assert.deepEqual(normalizeInterfaceVisibility(ui.saved.get(GRAPH_INTERFACE_VISIBILITY_KEY)),{topics:true,actions:false,services:true});
  assert.notEqual(owner.viewKey(),oldKey);assert.equal(owner.renderCalls,1);assert.equal(owner.inspectorCalls,1);
  assert.equal(owner.data,data);assert.equal(owner.flowActivity,activity);assert.equal(owner.liveWatch,watch);
  assert.equal(JSON.stringify(data),before);
});

test('turning every type off preserves a valid node focus and its isolated view',t=>{
  browser(t);const owner=explorer({focus:'producer',scope:'focus',isolatedId:'producer'});
  for(const kind of ['topics','actions','services'])owner.setInterfaceVisible(kind,false);
  assert.equal(owner.focus,'producer');assert.equal(owner.scope,'focus');assert.equal(owner.isolatedId,'producer');
  assert.deepEqual(ids(owner.visibleData().entities),['producer']);
});

test('list tabs still browse hidden types and selecting a row enables its type before rendering',t=>{
  const ui=browser(t),owner=explorer({interfaceVisibility:{...none},focus:null});
  for(const [kind,id] of [['topics','topic'],['actions','action'],['services','service']]) {
    owner.interfaceKind=kind;owner.list();
    const rows=ui.document.getElementById('interface-list').children;
    assert.equal(rows.length,1,kind);
    assert.equal(owner.interfaceVisibility[kind],false);
    rows[0].onclick();
    assert.equal(owner.interfaceVisibility[kind],true);assert.equal(owner.focus,id);
    assert.ok(ids(owner.lastVisible.entities).includes(id));
  }
});

test('selecting a hidden type outside the isolated view reveals it and exits the old isolation',t=>{
  browser(t);const owner=explorer({interfaceVisibility:{...all,services:false},scope:'focus',isolatedId:'topic'});
  owner.select('service');
  assert.equal(owner.interfaceVisibility.services,true);assert.equal(owner.focus,'service');
  assert.equal(owner.scope,'all');assert.equal(owner.isolatedId,null);
  assert.ok(ids(owner.lastVisible.entities).includes('service'));
});

test('explicit hidden-kind navigation also removes conflicting search/infrastructure filters',t=>{
  const ui=browser(t),owner=explorer({interfaceVisibility:{...all,topics:false},focus:'service'});
  ui.document.getElementById('graph-search').value='not-a-matching-object';
  owner.select('log');
  assert.equal(owner.interfaceVisibility.topics,true);
  assert.equal(ui.document.getElementById('show-infrastructure').checked,true);
  assert.equal(ui.document.getElementById('graph-search').value,'');
  assert.ok(ids(owner.lastVisible.entities).includes('log'));
});

test('isolating a hidden type enables it and uses that object as the new isolation center',t=>{
  browser(t);const owner=explorer({interfaceVisibility:{...all,actions:false}}),original={...owner.transform};
  owner.isolate('action');
  assert.equal(owner.interfaceVisibility.actions,true);assert.equal(owner.focus,'action');
  assert.equal(owner.scope,'focus');assert.equal(owner.isolatedId,'action');
  assert.deepEqual(owner.savedOverview,original);assert.ok(ids(owner.lastVisible.entities).includes('action'));
});

test('blocked storage and malformed setter requests do not prevent valid visibility changes',t=>{
  browser(t,{storageError:true});const owner=explorer();
  assert.equal(owner.setInterfaceVisible('unknown',false),false);
  assert.equal(owner.setInterfaceVisible('topics','false'),false);
  assert.equal(owner.renderCalls,0);
  assert.equal(owner.setInterfaceVisible('topics',false),true);assert.equal(owner.interfaceVisibility.topics,false);
  assert.equal(owner.setInterfaceVisible('topics',false),false);assert.equal(owner.renderCalls,1);
});

test('initial discovery never auto-selects a hidden type and still observes the complete graph',async t=>{
  browser(t);let observed;
  const owner=explorer({data:null,focus:null,initialized:false,connected:true,visible:true,pending:false,
    interfaceVisibility:{...none},liveWatch:{enabled:true},flowActivity:{observe(data){observed=data;},clear(){}},
    request:async()=>raw(),restartPolling(){},syncLiveWatch:async()=>{},stopFlow(){},message(){}});
  await owner.refresh();
  assert.equal(owner.focus,'producer');assert.deepEqual(ids(owner.lastVisible.entities),['consumer','idle','producer','server']);
  assert.equal(observed.topics.length,2);assert.equal(observed.actions.length,1);assert.equal(observed.services.length,1);
});
