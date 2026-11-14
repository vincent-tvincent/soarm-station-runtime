import test from 'node:test';
import assert from 'node:assert/strict';
import { PolicyManagement } from '../web/policy-management.js';
import { ProfileDraft } from '../web/policy-draft.js';

const pending=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const text=(id='one',name='Original')=>`id: ${id}\nname: ${name}\nexecution: {kind: local, device: cpu}\nbindings: {arm: arm1, cameras: {}}\n`;
const profile=(id='one',name='Original',revision='r1')=>({id,text:text(id,name),revision});
const draft=(id='one',name='Original',revision='r1')=>new ProfileDraft(profile(id,name,revision));
function manager(overrides={}){
  const m=Object.create(PolicyManagement.prototype);
  Object.assign(m,{
    visible:false,view:'editor',connected:true,resources:{},robotState:{},profiles:[],models:[],sources:{roots:[]},runtime:{},uploads:[],
    loaded:true,loading:false,mutating:false,error:'',info:'',draft:draft(),editorTab:'form',editorError:'',validation:null,conflictRemote:null,
    search:'',selectedModel:null,picker:false,importText:'',importDirty:false,importResult:null,importMode:'new',importNewId:'',
    importFilename:'No profile selected',importVersion:0,importPreviewVersion:0,importReadVersion:0,importPreviewSnapshot:null,
    navigationVersion:0,validationVersion:0,inspectedRemote:null,inspectionVersion:0,inspectionJob:null,_inspectionContext:null,_inspectionStatus:null,runtimeVersion:0,catalogVersion:0,profileListVersion:0,
    scanJobs:new Map(),preparationJobs:new Map(),preparationOutcomes:new Map(),selectedProfile:null,activeTransfers:new Map(),runGeneration:0,runInstruction:'Pick the cup',runDuration:'',runMode:'STREAM',
    startedRun:null,operation:null,instructionPending:false,renders:0,notices:[],
    client:{currentOperation:null,refresh:async()=>{},enable:async()=>{}},
    api:{profiles:async()=>[],models:async()=>[],sources:async()=>({roots:[]}),runtime:async()=>({})},
    q:()=>null,render(){this.renders+=1;},renderContent(){this.renders+=1;},renderActive(){},renderUploadJobs(){},updateDraftIndicators(){},
    show(value,error=false){this.notices.push({value,error});},onError(){},schedulePoll(){},dialog:async()=> 'cancel',
    ...overrides,
  });
  return m;
}
const modelOp=(requestId='request1',operationId='operation1',extra={})=>({type:'MODEL',mode:'STREAM',armId:'arm1',requestId,operationId,state:'active',ready:true,...extra});
const owned=(m,op=modelOp())=>{m.client.currentOperation=op;m.startedRun={tracking:true,requestId:op.requestId,operationId:op.operationId,armId:op.armId,instruction:'Old',mode:op.mode};return m.startedRun;};

test('discarding a new draft on navigation removes it rather than leaving a dirty phantom',async()=>{
  const m=manager({draft:draft('new_profile','New',null),dialog:async()=> 'discard'});
  assert.equal(await m.navigate('profiles'),true);
  assert.equal(m.draft,null);assert.equal(m.hasUnsavedChanges(),false);
});

test('later profile navigation wins; an older response cannot replace it',async()=>{
  const a=pending(),b=pending(),m=manager();m.api.profile=id=>id==='a'?a.promise:b.promise;
  const first=m.openProfile('a');await tick();const second=m.openProfile('b');await tick();
  b.resolve(profile('b'));assert.equal(await second,true);
  a.resolve(profile('a'));assert.equal(await first,false);
  assert.equal(m.draft.id,'b');
});

test('typing while a profile read is pending preserves the edited current document',async()=>{
  const load=pending(),m=manager();m.api.profile=()=>load.promise;
  const request=m.openProfile('other');await tick();m.setField('name','New local edit');
  load.resolve(profile('other'));assert.equal(await request,false);
  assert.equal(m.draft.id,'one');assert.equal(m.draft.getField('name'),'New local edit');
});

test('save acknowledges its captured instance without corrupting a newly opened draft',async()=>{
  const save=pending(),m=manager();m.draft.setField('name','Submitted');const old=m.draft;
  m.api.saveProfile=()=>save.promise;const result=m.save();m.installDraft(profile('two'));
  save.resolve(profile('one','Submitted','r2'));assert.equal(await result,false);
  assert.equal(m.draft.id,'two');assert.equal(m.draft.revision,'r1');assert.equal(old.revision,'r2');
  assert.equal(m.draft.conflict,null);
});

test('successful save keeps edits made while pending, and a list refresh failure does not undo the save',async()=>{
  const save=pending(),m=manager();m.draft.setField('name','Submitted');m.api.saveProfile=()=>save.promise;
  m.api.profiles=async()=>{throw Error('List offline');};const result=m.save();m.setField('name','Later');
  save.resolve(profile('one','Submitted','r2'));assert.equal(await result,false);
  assert.equal(m.draft.revision,'r2');assert.equal(m.draft.getField('name'),'Later');assert.equal(m.draft.dirty,true);
  assert.match(m.notices.at(-1).value,/Saved one.*list refresh failed/i);
  assert.equal(m.draft.conflict,null);
});

test('a conflict fetch returning after document replacement cannot mark the new draft',async()=>{
  const remote=pending(),m=manager();m.draft.setField('name','Local');
  m.api.saveProfile=async()=>{throw Object.assign(Error('Conflict'),{status:409});};m.api.profile=()=>remote.promise;
  const saving=m.save();await tick();m.installDraft(profile('two'));remote.resolve(profile('one','Remote','r2'));
  assert.equal(await saving,false);assert.equal(m.draft.conflict,null);assert.equal(m.conflictRemote,null);
});

test('reload does not discard edits made after its confirmation/request',async()=>{
  const response=pending(),m=manager({dialog:async()=> 'reload'});m.draft.setField('name','Before');m.api.profile=()=>response.promise;
  const reloading=m.reloadDraft();await tick();m.setField('name','Typed during reload');
  response.resolve(profile('one','Server','r2'));assert.equal(await reloading,false);
  assert.equal(m.draft.getField('name'),'Typed during reload');
});

test('stale validation cannot label a newer raw draft valid',async()=>{
  const response=pending(),m=manager();m.api.validate=()=>response.promise;
  const validation=m.validate();m.draft.setRaw('id: [invalid');m.invalidateValidation();
  response.resolve({valid:true});assert.equal(await validation,false);assert.equal(m.validation,null);
});

test('remote descriptor lookup uses host, deployment, instance and artifact rather than name alone',()=>{
  const m=manager();m.runtime.descriptors=[
    {endpoint:'tcp/host-a:7447',deployment:'serve',instance_id:'a',artifact_identity:'sha256:a'},
    {endpoint:'tcp/host-b:7447',deployment:'serve',instance_id:'b',artifact_identity:'sha256:b'},
  ];
  const p={execution:{kind:'remote',endpoint:'tcp/host-b:7447',deployment:'serve',instance:'b',expected_artifact:'sha256:b'}};
  assert.equal(m.modelFor(p).instance_id,'b');
  p.execution.expected_artifact='sha256:a';assert.equal(m.modelFor(p),null);
});

test('unsaved remote inspection supplies input metadata only for the exact inspected draft version',async()=>{
  const m=manager();m.draft=new ProfileDraft({text:'id: one\nexecution: {kind: remote, endpoint: "tcp/gpu:7447", deployment: serve, expected_artifact: "sha256:a"}\n',revision:'r1'});
  const model={camera_inputs:[{key:'observation.images.front'}]};
  m.api.inspectDraft=async()=>({valid:true,resolved_model:model,descriptor:{deployment:'serve',artifact_identity:'sha256:a'}});
  assert.equal(await m.inspectRemote(),true);assert.equal(m.modelFor(m.value()),model);
  m.setField(['bindings','cameras','observation.images.front'],'forward');
  assert.equal(m.modelFor(m.value()),model);assert.equal(m.validation,null);
  m.setField(['execution','endpoint'],'tcp/other:7447');assert.equal(m.modelFor(m.value()),null);
});

test('import preview cannot certify text changed during its request',async()=>{
  const response=pending(),m=manager({view:'import'});m.setImportText(text('first'));m.api.importPreview=()=>response.promise;
  const preview=m.importPreview();m.setImportText(text('second'));
  response.resolve({valid:true,profile:{id:'first'}});assert.equal(await preview,false);assert.equal(m.importResult,null);
});

test('import pins its validated request and retains newer import edits while pending',async()=>{
  const response=pending(),m=manager({view:'import'});m.setImportText(text('first'));
  m.api.importPreview=async()=>({valid:true,profile:{id:'first',name:'First'}});await m.importPreview();
  let submitted;m.api.importProfile=request=>{submitted=request;return response.promise;};
  const importing=m.commitImport();m.setImportText(text('second'));
  response.resolve(profile('first'));assert.equal(await importing,true);
  assert.equal(submitted.id,'first');assert.match(submitted.text,/id: first/);
  assert.match(m.importText,/id: second/);assert.equal(m.importDirty,true);assert.equal(m.view,'import');
  assert.equal(await m.commitImport(),false); // New text has no matching preview.
});

test('profile file read cannot overwrite text typed while it is pending',async()=>{
  const response=pending(),m=manager({view:'import'});
  const reading=m.loadImportFile({size:100,name:'chosen.yaml',text:()=>response.promise});
  m.setImportText(text('typed'));response.resolve(text('file'));
  assert.equal(await reading,false);assert.match(m.importText,/id: typed/);
});

test('model selection changes known bindings without deleting advanced maps/comments',async()=>{
  const raw=`# advanced profile
id: one
execution: {kind: local, device: cpu, future_option: 9}
model: {source_id: old, relative_path: old-path, artifact_revision: old-rev, vendor: retained}
contract: {action_fps: 30}
settings: {timeout_s: 8}
bindings:
  arm: arm1
  state: {state_feature: joint1}
  actions: {action_feature: joint1}
  cameras: {old_camera: old_cam, shared_camera: wrist, raw_extension: retain_me}
extensions: {vendor: true}
`;
  const m=manager({draft:new ProfileDraft({text:raw,revision:'r1'}),picker:true,view:'models',selectedModel:'new'});
  m.models=[{id:'old',source_id:'old',relative_path:'old-path',artifact_revision:'old-rev',camera_inputs:[{key:'old_camera'},{key:'shared_camera'}]},
    {id:'new',source_id:'new',relative_path:'new-path',artifact_revision:'new-rev',camera_inputs:[{key:'shared_camera'},{key:'new_camera'}]}];
  await m.useModel();const p=m.value();
  assert.equal(p.execution.future_option,9);assert.equal(p.model.vendor,'retained');
  assert.deepEqual(p.contract,{action_fps:30});assert.deepEqual(p.settings,{timeout_s:8});
  assert.deepEqual(p.bindings.state,{state_feature:'joint1'});assert.deepEqual(p.bindings.actions,{action_feature:'joint1'});
  assert.deepEqual(p.bindings.cameras,{shared_camera:'wrist',raw_extension:'retain_me',new_camera:''});
  assert.deepEqual(p.extensions,{vendor:true});assert.match(m.draft.text,/# advanced profile/);
});

test('early anonymous MODEL events cannot bind this panel to a different operation',()=>{
  const m=manager();m.startedRun={tracking:false,pending:true,armId:'arm1',operationId:null,requestId:null};
  m.updateOperation(modelOp('unrelated','other'));assert.equal(m.startedRun.requestId,null);
  m.startedRun={tracking:true,armId:'arm1',requestId:'mine',operationId:null};
  m.updateOperation(modelOp('unrelated','other'));assert.equal(m.startedRun.operationId,null);
  m.updateOperation(modelOp('mine','owned'));assert.equal(m.startedRun.operationId,'owned');
});

test('start uses its submitted settings snapshot and binds only the returned request identity',async()=>{
  const enabled=pending(),submitted=pending(),m=manager();m.runtime.applied={id:'preset',runtime_profile_id:'native-v2',arm_id:'arm1',state:'ready',language_conditioned:true};
  m.client.enable=()=>enabled.promise;let args;m.client.submit=(...a)=>{args=a;return submitted.promise;};
  const starting=m.startPolicy();await tick();m.runInstruction='Unsent later draft';m.runMode='INLINE';m.runDuration='9';
  enabled.resolve();await tick();assert.equal(args[2],'STREAM');assert.deepEqual(args[3],{instruction:'Pick the cup'});
  m.client.currentOperation=modelOp('mine','owned');submitted.resolve(modelOp('mine',null,{state:'preparing'}));
  assert.equal(await starting,true);assert.equal(m.startedRun.instruction,'Pick the cup');assert.equal(m.startedRun.operationId,'owned');
});

test('instruction acknowledgement records submitted text and never alters a replacement run',async()=>{
  const response=pending(),m=manager();const run=owned(m);m.runInstruction='Submitted prompt';
  m.client.updateStream=()=>response.promise;const updating=m.updateInstruction();m.runInstruction='Unsent draft';response.resolve({});
  assert.equal(await updating,true);assert.equal(run.instruction,'Submitted prompt');assert.equal(m.runInstruction,'Unsent draft');
  const second=pending();m.client.updateStream=()=>second.promise;const late=m.updateInstruction();
  const replacement=owned(m,modelOp('other-request','other-operation'));second.resolve({});
  assert.equal(await late,false);assert.equal(replacement.instruction,'Old');
});

test('Update is refused for INLINE or foreign operations, and Stop never ends an unrelated client operation',async()=>{
  const m=manager();let writes=0;m.client.updateStream=async()=>{writes++;};m.client.endOperation=async()=>{writes++;};
  owned(m,modelOp('mine','owned',{mode:'INLINE'}));await assert.rejects(m.updateInstruction(),/continuous stream/);
  m.client.currentOperation=modelOp('other','other');await assert.rejects(m.stopPolicy(),/does not own/);
  assert.equal(writes,0);
});

test('Apply supplies the displayed saved revision and explicitly queues at a current-operation boundary',async()=>{
  const m=manager();m.profiles=[{id:'preset',revision:'etag-saved'}];m.client.currentOperation=modelOp();m.dialog=async()=> 'after_current';
  let args;m.api.prepare=async(...a)=>{args=a;return{job:{id:'prepare1',state:'queued'}};};
  assert.equal(await m.apply('preset'),true);assert.deepEqual(args,['preset',{expected_revision:'etag-saved',mode:'after_current'}]);
});

test('scan completion refreshes matching-search catalog while preserving selection/draft',async()=>{
  const m=manager({view:'models',search:'cup',selectedModel:'keep'}),original=m.draft;
  m.api.scan=async()=>({job:{id:'scan1',kind:'scan',state:'queued'}});
  let queries=[];m.api.models=async query=>{queries.push(query);return[{id:queries.length===1?'before':'after'}];};
  m.api.scanStatus=async()=>({job:{id:'scan1',state:'complete'}});
  await m.scan();assert.equal(m.models[0].id,'before');await m.pollScans();
  assert.equal(m.models[0].id,'after');assert.deepEqual(queries,['cup','cup']);assert.equal(m.scanJobs.size,0);
  assert.equal(m.selectedModel,'keep');assert.equal(m.draft,original);
});

test('older catalog/runtime requests cannot overwrite newer responses',async()=>{
  const m=manager(),first=pending(),second=pending();let calls=0;m.api.models=()=>++calls===1?first.promise:second.promise;
  const a=m.refreshCatalog(),b=m.refreshCatalog();second.resolve([{id:'new'}]);await b;first.resolve([{id:'old'}]);await a;
  assert.equal(m.models[0].id,'new');
  const old=pending(),fresh=pending();calls=0;m.api.runtime=()=>++calls===1?old.promise:fresh.promise;
  const c=m.refreshRuntime(),d=m.refreshRuntime();fresh.resolve({applied:{id:'new'}});await d;old.resolve({applied:{id:'old'}});await c;
  assert.equal(m.applied().id,'new');
});

test('catalog scan completion is processed even when the independent runtime poll fails',async()=>{
  const m=manager();m.scanJobs.set('scan1',{id:'scan1',state:'running'});
  m.api.runtime=async()=>{throw Error('Runtime offline');};
  m.api.scanStatus=async()=>({job:{id:'scan1',state:'complete'}});m.api.models=async()=>[{id:'newly-scanned'}];
  await assert.rejects(m.pollOnce(),/Runtime offline/);
  assert.equal(m.models[0].id,'newly-scanned');assert.equal(m.scanJobs.size,0);
});

test('a newly completed upload refreshes the catalog and releases finished transfer tracking',async()=>{
  const m=manager({uploads:[{id:'upload1',state:'verifying'}]});m.activeTransfers.set('upload1',{running:false});
  m.api.uploads=async()=>[{id:'upload1',state:'ready'}];m.api.models=async()=>[{id:'uploaded-model'}];
  await m.refreshUploads();assert.equal(m.activeTransfers.size,0);assert.equal(m.models[0].id,'uploaded-model');
});

const remoteManager=()=>manager({draft:new ProfileDraft({text:'id: one\nexecution: {kind: remote, endpoint: "tcp/gpu:7447", deployment: serve, expected_artifact: "sha256:a"}\n',revision:'r1'})});
const descriptor=extra=>({deployment:'serve',instance_id:'server1',artifact_identity:'sha256:a',ready:true,
  capabilities:{language:true,features:[
    {name:'observation.state',kind:'tensor',shape:[6],dtype:'float32'},
    {name:'observation.images.global',kind:'rgb',shape:[480,640,3],dtype:'uint8'},
    {name:'observation.images.wrist',kind:'rgb',shape:[720,1280,3],dtype:'uint8'},
  ]},...extra});

test('asynchronous remote inspection exposes pending/progress and normalizes completed native camera metadata',async()=>{
  const m=remoteManager();m.api.inspectDraft=async()=>({valid:true,ready:false,descriptor:null,admitted:false,job:{id:'inspect1',state:'queued'}});
  assert.equal(await m.inspectRemote(),true);assert.equal(m.inspectionStatus.pending,true);assert.equal(m.inspectionStatus.state,'queued');
  assert.equal(m.inspectedRemote,null);assert.equal(m.validation.ready,false);
  m.api.preparationStatus=async()=>({job:{id:'inspect1',state:'running',phase:'discovering'}});
  await m.pollInspections();assert.equal(m.inspectionStatus.message,'discovering');
  m.api.preparationStatus=async()=>({job:{id:'inspect1',state:'complete',result:{descriptor:descriptor(),admitted:false}}});
  assert.equal(await m.pollInspections(),true);const model=m.modelFor(m.value());
  assert.deepEqual(model.camera_inputs.map(c=>[c.key,c.shape]),[
    ['observation.images.global',[480,640,3]],['observation.images.wrist',[720,1280,3]],
  ]);
  assert.equal(model.category,null);assert.equal(model.language_conditioned,null); // text generation is not VLA conditioning
  assert.equal(model.state,'inspected');assert.equal(m.validation.ready,false);assert.equal(m.inspectionStatus.state,'complete');
  assert.equal(m.inspectionStatus.admitted,false);assert.equal(m.inspectionJob,null);
  m.setField(['bindings','cameras','observation.images.global'],'overview');
  assert.equal(m.inspectionStatus.state,'complete');assert.ok(m.modelFor(m.value()));
});

test('a remote inspection completing after an endpoint edit cannot populate the new draft',async()=>{
  const m=remoteManager(),response=pending();m.api.inspectDraft=async()=>({valid:true,job:{id:'inspect1',state:'queued'},admitted:false});
  await m.inspectRemote();m.api.preparationStatus=()=>response.promise;const poll=m.pollInspections();
  m.setField(['execution','endpoint'],'tcp/other:7447');
  response.resolve({job:{id:'inspect1',state:'complete',result:{descriptor:descriptor(),admitted:false}}});
  assert.equal(await poll,false);assert.equal(m.inspectedRemote,null);assert.equal(m.inspectionJob,null);
  assert.equal(m.inspectionStatus.state,'stale');assert.equal(m.inspectionStatus.pending,false);
});

test('superseded inspection completions cannot overwrite a newer queued inspection',async()=>{
  const m=remoteManager(),response=pending();let requests=0;
  m.api.inspectDraft=async()=>({valid:true,job:{id:`inspect${++requests}`,state:'queued'},admitted:false});
  await m.inspectRemote();m.api.preparationStatus=()=>response.promise;const oldPoll=m.pollInspections();
  await m.inspectRemote();response.resolve({job:{id:'inspect1',state:'complete',result:{descriptor:descriptor(),admitted:false}}});
  assert.equal(await oldPoll,false);assert.equal(m.inspectionStatus.jobId,'inspect2');assert.equal(m.inspectionJob.job.id,'inspect2');
  assert.equal(m.inspectedRemote,null);
});

test('terminal inspection failure surfaces its error without marking a model or profile ready',async()=>{
  const m=remoteManager();m.api.inspectDraft=async()=>({valid:true,ready:false,job:{id:'inspect1',state:'queued'},admitted:false});
  await m.inspectRemote();m.api.preparationStatus=async()=>({job:{id:'inspect1',state:'failed',errors:[{message:'GPU endpoint refused discovery'}]}});
  assert.equal(await m.pollInspections(),false);assert.equal(m.inspectionStatus.state,'failed');assert.equal(m.inspectionStatus.pending,false);
  assert.match(m.editorError,/refused discovery/);assert.equal(m.validation.ready,false);assert.equal(m.inspectedRemote,null);assert.equal(m.inspectionJob,null);
});

test('completed inspection jobs must contain a matching descriptor and no admission',async()=>{
  for(const result of [{admitted:false},{descriptor:descriptor({artifact_identity:'sha256:wrong'}),admitted:false},{descriptor:descriptor(),admitted:true}]){
    const m=remoteManager();m.api.inspectDraft=async()=>({valid:true,admitted:false,job:{id:'inspect1',state:'complete',result}});
    assert.equal(await m.inspectRemote(),false);assert.equal(m.inspectionStatus.state,'failed');assert.equal(m.inspectedRemote,null);
    assert.equal(m.validation.ready,false);if(result.admitted)assert.equal(m.inspectionStatus.admitted,true);
  }
});

test('transient status errors remain pending and a retry can complete inspection',async()=>{
  const m=remoteManager();m.api.inspectDraft=async()=>({valid:true,job:{id:'inspect1',state:'queued'},admitted:false});await m.inspectRemote();
  m.api.preparationStatus=async()=>{throw Error('Temporary network failure');};
  assert.equal(await m.pollInspections(),false);assert.equal(m.inspectionStatus.state,'retrying');assert.equal(m.inspectionStatus.pending,true);
  m.api.preparationStatus=async()=>({job:{id:'inspect1',state:'complete',result:{descriptor:descriptor(),admitted:false}}});
  assert.equal(await m.pollInspections(),true);assert.equal(m.inspectionStatus.state,'complete');
});

test('inspection progress is independent from an unavailable runtime poll and does not adopt its applied state',async()=>{
  const m=remoteManager();m.api.inspectDraft=async()=>({valid:true,job:{id:'inspect1',state:'queued'},admitted:false});await m.inspectRemote();
  m.api.runtime=async()=>{throw Error('Runtime snapshot unavailable');};
  m.api.preparationStatus=async()=>({job:{id:'inspect1',state:'complete',result:{descriptor:descriptor(),admitted:false}},applied:{id:'not-an-inspection-result',state:'ready'}});
  await assert.rejects(m.pollOnce(),/Runtime snapshot unavailable/);
  assert.equal(m.inspectionStatus.state,'complete');assert.equal(m.applied(),null);assert.equal(m.validation.ready,false);
});

test('metadata outage keeps Stop for the owned run without enabling a new run or claiming a foreign one',async()=>{
  const m=manager();const run=owned(m);let enables=0;
  run.appliedSnapshot={id:'policy',name:'Running policy',state:'ready',arm_id:'arm1',execution:'remote',language_conditioned:true};
  m.runtime={state:'unavailable',applied:null};m.client.enable=async()=>{enables++;};
  assert.equal(m.displayedApplied().state,'metadata unavailable');
  assert.match(m.runPage(),/data-action="stop-policy"/);
  assert.equal(await m.startPolicy(),false);assert.equal(enables,0);
  m.client.currentOperation=modelOp('foreign-request','foreign-operation');
  assert.equal(m.displayedApplied(),null);assert.doesNotMatch(m.runPage(),/data-action="stop-policy"/);
});

const prepared=(id='preset',revision='r2',arm='arm1')=>({id,name:id,profile_revision:revision,arm_id:arm,state:'ready',runtime_profile_id:`native-${id}-${revision}-${arm}`});
const requestedPreparation=(m,jobId='prep1',extra={})=>{
  const requested={id:'preset',revision:'r2',armId:'arm1',name:'Selected policy',job:{id:jobId,state:'queued'},polling:false,...extra};
  m.preparationJobs.set(jobId,requested);m.selectedProfile={id:requested.id,revision:requested.revision,armId:requested.armId,jobId};
  return requested;
};

test('selected profile resolves exact revision and arm, never a stale last-applied fallback',()=>{
  const m=manager({selectedProfile:{id:'preset',revision:'r2',armId:'arm2'}});
  m.runtime={applied:prepared('preset','r2','arm1'),applied_by_arm:{arm1:prepared('preset','r1','arm1'),arm2:prepared('preset','r2','arm2')}};
  assert.equal(m.applied().arm_id,'arm2');
  m.selectedProfile={id:'preset',revision:'r2',armId:'arm1'};assert.equal(m.applied(),null);
  m.selectedProfile={id:'preset',revision:'r3',armId:'arm2'};assert.equal(m.applied(),null);
});

test('an arm-unbound selection does not arbitrarily choose between ambiguous matching profiles',()=>{
  const m=manager({selectedProfile:{id:'preset',revision:'r2'}});
  m.runtime={applied_by_arm:{arm1:prepared(),arm2:prepared('preset','r2','arm2')}};
  assert.equal(m.applied(),null);
});

test('an owned active run displays its pinned profile while another selected profile is ready',()=>{
  const m=manager(),run=owned(m);run.appliedSnapshot=prepared('active','r1','arm1');
  const active=prepared('active','r1','arm1'),next=prepared('next','r2','arm2');
  m.runtime={applied:next,applied_by_arm:{arm1:active,arm2:next}};
  m.selectedProfile={id:'next',revision:'r2',armId:'arm2'};
  assert.equal(m.applied().id,'next');assert.equal(m.displayedApplied().id,'active');assert.equal(m.displayedApplied().state,'ready');
});

test('queued Apply response cannot overwrite a terminal failure found during refresh',async()=>{
  const m=manager();m.profiles=[{id:'preset',name:'Test policy',revision:'r2'}];
  m.api.prepare=async()=>({job:{id:'prep1',state:'queued'},message:'Older generic queued notice'});
  m.api.runtime=async()=>({jobs:[{id:'prep1',state:'failed',errors:[{message:'Insufficient memory'}]}],applied_by_arm:{arm2:prepared('other','r4','arm2')}});
  assert.equal(await m.apply('preset'),false);
  assert.match(m.notices.at(-1).value,/Preparation failed.*Insufficient memory/);
  assert.equal(m.notices.at(-1).error,true);assert.equal(m.preparationJobs.size,0);assert.equal(m.applied(),null);
});

test('immediate failed and completed prepare responses report their actual terminal result',async()=>{
  for(const success of [false,true]){
    const m=manager();m.profiles=[{id:'preset',name:'Test policy',revision:'r2'}];
    m.api.prepare=async()=>({job:success?{id:'prep1',state:'complete',result:{applied:prepared()}}:{id:'prep1',state:'failed',errors:[{message:'Bad processor'}]}});
    m.api.runtime=async()=>({jobs:[],applied_by_arm:success?{arm1:prepared()}: {}});
    assert.equal(await m.apply('preset'),success);assert.equal(m.preparationJobs.size,0);
    assert.equal(m.preparationOutcomes.get('prep1').state,success?'ready':'failed');
    assert.match(m.notices.at(-1).value,success?/Prepared Test policy/:/Bad processor/);
    if(success){assert.equal(m.selectedProfile.armId,'arm1');assert.equal(m.applied().profile_revision,'r2');}
  }
});

test('preparation polling finds terminal jobs omitted from the recent runtime job window',async()=>{
  const m=manager();requestedPreparation(m);
  m.api.runtime=async()=>({jobs:[],applied_by_arm:{arm1:prepared()}});
  const calls=[];m.api.preparationStatus=async id=>{calls.push(id);return{job:{id,state:'complete',result:{applied:prepared()}}};};
  await m.pollOnce();assert.deepEqual(calls,['prep1']);assert.equal(m.preparationJobs.size,0);
  assert.equal(m.preparationOutcomes.get('prep1').state,'ready');assert.match(m.notices.at(-1).value,/Prepared Selected policy/);
});

test('completed preparation for another revision or arm never reports ready for the selected request',()=>{
  for(const metadata of [prepared('preset','older','arm1'),prepared('preset','r2','arm2'),prepared('other','r2','arm1')]){
    const m=manager();requestedPreparation(m);
    m.observePreparations({jobs:[{id:'prep1',state:'complete',result:{applied:metadata}}],applied:metadata});
    assert.equal(m.preparationOutcomes.get('prep1').state,'unconfirmed');assert.equal(m.notices.at(-1).error,true);
    assert.doesNotMatch(m.notices.at(-1).value,/^Prepared /);
  }
});

test('a previous preparation completion is recorded without replacing the latest selected request notice',()=>{
  const m=manager();requestedPreparation(m,'older');
  requestedPreparation(m,'newer',{id:'next',revision:'r3',name:'New selection'});
  m.show('New selection is preparing.');
  m.observePreparations({jobs:[{id:'older',state:'failed',errors:[{message:'Old failure'}]}]});
  assert.equal(m.preparationOutcomes.get('older').state,'failed');assert.equal(m.selectedProfile.id,'next');
  assert.equal(m.notices.at(-1).value,'New selection is preparing.');assert.equal(m.preparationJobs.has('newer'),true);
});

test('pending preparations keep background polling alive when the management page is hidden',t=>{
  const m=manager({visible:false});requestedPreparation(m);let callback=null;
  t.mock.method(globalThis,'setTimeout',(fn,ms)=>{callback=fn;assert.equal(ms,2000);return 123;});
  t.mock.method(globalThis,'clearTimeout',()=>{});
  PolicyManagement.prototype.schedulePoll.call(m);
  assert.equal(typeof callback,'function');assert.equal(m.pollTimer,123);
});

test('full refresh does not erase a discovered preparation failure with an empty generic notice',async()=>{
  const m=manager();requestedPreparation(m);m.api.uploads=async()=>[];
  m.api.runtime=async()=>({jobs:[{id:'prep1',state:'failed',errors:[{message:'Load failed'}]}]});
  await m.refresh();assert.match(m.notices.at(-1).value,/Preparation failed.*Load failed/);assert.equal(m.notices.at(-1).error,true);
});

test('pending banners follow the displayed active arm, then the selected arm when its operation ends',()=>{
  const m=manager(),run=owned(m);run.appliedSnapshot=prepared('active','r1','arm1');
  const arm1={id:'next_on_arm1',name:'Arm one next',profile_revision:'r3',arm_id:'arm1',state:'after_current'};
  const arm2={id:'next_on_arm2',name:'Arm two next',profile_revision:'r4',arm_id:'arm2',state:'preparing'};
  m.runtime={applied_by_arm:{arm1:run.appliedSnapshot,arm2:prepared('selected','r2','arm2')},pending:arm2,pending_by_arm:{arm1,arm2}};
  m.selectedProfile={id:'selected',revision:'r2',armId:'arm2'};
  assert.equal(m.pendingForDisplay().id,'next_on_arm1');
  const banner={innerHTML:''};m.q=()=>banner;PolicyManagement.prototype.renderActive.call(m);
  assert.match(banner.innerHTML,/Arm one next/);assert.doesNotMatch(banner.innerHTML,/Arm two next/);
  assert.match(m.runPage(),/Arm one next/);assert.doesNotMatch(m.runPage(),/Arm two next/);
  run.tracking=false;m.client.currentOperation=null;
  assert.equal(m.pendingForDisplay().id,'next_on_arm2');
});

test('a requested profile with no known arm uses its exact pending identity instead of another global pending arm',()=>{
  const m=manager({selectedProfile:{id:'requested',revision:'r3',jobId:'job3'}});
  const correct={id:'requested',name:'Correct request',profile_revision:'r3',job_id:'job3',state:'preparing'};
  const unrelated={id:'other',name:'Unrelated global request',profile_revision:'r7',job_id:'job7',arm_id:'arm3',state:'preparing'};
  m.runtime={pending:unrelated,pending_by_arm:{arm1:{...correct,profile_revision:'old',job_id:'old'},arm2:correct,arm3:unrelated}};
  assert.equal(m.pendingForDisplay().arm_id,'arm2');
  delete m.runtime.pending_by_arm.arm2;
  assert.equal(m.pendingForDisplay(),null);
  const banner={innerHTML:''};m.q=()=>banner;PolicyManagement.prototype.renderActive.call(m);
  assert.doesNotMatch(banner.innerHTML,/Unrelated global request/);
});
