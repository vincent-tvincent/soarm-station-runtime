import { ProfileDraft } from './policy-draft.js';
import { stringify } from './yaml-vendor.js';
import { PolicyAPI } from './policy-api.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = value => JSON.parse(JSON.stringify(value));
const bytes = n => !Number.isFinite(n) ? '—' : n >= 1e9 ? `${(n/1e9).toFixed(2)} GB` : n >= 1e6 ? `${(n/1e6).toFixed(1)} MB` : `${Math.ceil(n/1024)} KB`;
const field = (label, input) => `<label><span>${label}</span>${input}</label>`;
const option = (id,label,current) => `<option value="${escape(id)}" ${id===current?'selected':''}>${escape(label)}</option>`;
const terminal = operation => !operation || ['done','ended','error','idle','result','closed','interrupted'].includes(operation.state);
const message = error => error?.message || String(error);
const completedJob = job => ['complete','failed','canceled','ready','cataloged'].includes(job?.state);
const descriptorMatches = (descriptor, execution) => {
  if (!descriptor || descriptor.endpoint !== execution.endpoint || descriptor.deployment !== execution.deployment) return false;
  if (execution.instance && (descriptor.instance ?? descriptor.instance_id) !== execution.instance) return false;
  if (execution.expected_artifact && (descriptor.artifact_revision ?? descriptor.artifact_identity ?? descriptor.expected_artifact) !== execution.expected_artifact) return false;
  return true;
};
const remoteInputModel = (descriptor, resolved) => {
  if(resolved&&Array.isArray(resolved.camera_inputs))return resolved;
  const capabilities=descriptor.capabilities||{};
  const features=Array.isArray(capabilities.features)?capabilities.features:[];
  const cameras=features.filter(f=>f&&typeof f.name==='string'&&(f.kind==='rgb'||f.kind==='image'||f.name.startsWith('observation.images.'))).map(f=>({
    key:f.name,role:typeof f.role==='string'?f.role:null,masked:f.masked===true,
    shape:Array.isArray(f.shape)?[...f.shape]:null,dtype:f.dtype??null,kind:f.kind??null,
  }));
  // Native `capabilities.language` describes text-generation support, not
  // whether a policy accepts a language instruction. Neither category nor
  // language conditioning can be inferred from a deployment's arbitrary name.
  return {
    ...(resolved||{}),name:resolved?.name??descriptor.deployment??'Remote deployment',
    category:resolved?.category??descriptor.category??descriptor.policy_type??null,
    artifact_revision:resolved?.artifact_revision??descriptor.artifact_identity??descriptor.artifact_revision??null,
    camera_inputs:cameras,
    language_conditioned:typeof resolved?.language_conditioned==='boolean'?resolved.language_conditioned:
      typeof descriptor.language_conditioned==='boolean'?descriptor.language_conditioned:null,
    state:'inspected',
  };
};

/** Third workspace page. It shares the application's StationClient; it never owns hardware. */
export class PolicyManagement {
  constructor({element, client, api = new PolicyAPI(), onError=()=>{}, onNotice=()=>{}}) {
    this.element=element; this.client=client; this.api=api; this.onError=onError; this.onNotice=onNotice;
    this.visible=false; this.view='profiles'; this.connected=false; this.resources={}; this.robotState={};
    this.profiles=[]; this.models=[]; this.modelCache=new Map(); this.sources={roots:[]}; this.runtime={}; this.uploads=[];
    this.loaded=false; this.loading=false; this.mutating=false; this.error=''; this.info='';
    this.draft=null; this.editorTab='form'; this.editorError=''; this.validation=null; this.conflictRemote=null;
    this.search=''; this.selectedModel=null; this.picker=false; this.importText=''; this.importDirty=false;
    this.importResult=null; this.importMode='new'; this.importNewId=''; this.importFilename='No profile selected';
    this.uploadKind='folder'; this.uploadSource=''; this.uploadName=''; this.uploadFiles=[]; this.uploadLabel=''; this.resumeTarget=null;
    this.activeTransfers=new Map(); this.runInstruction=''; this.runDuration=''; this.runMode='STREAM'; this.startedRun=null;
    this.operation=null; this.lastStateRender=0; this.navigation=Promise.resolve(); this.modalResolve=null;
    this.navigationVersion=0; this.validationVersion=0; this.importVersion=0; this.importPreviewVersion=0;
    this.importReadVersion=0; this.importPreviewSnapshot=null; this.inspectedRemote=null;
    this.inspectionVersion=0; this.inspectionJob=null; this._inspectionContext=null; this._inspectionStatus=null;
    this.runtimeVersion=0; this.catalogVersion=0; this.profileListVersion=0; this.catalogRevision=null;
    this.scanJobs=new Map(); this.runGeneration=0; this.instructionPending=false;
    this.element.innerHTML=`<div id="policy-manager"><nav class="pm-nav" aria-label="Policy management"><button data-view="profiles">Profiles</button><button data-view="models">Model library</button><button data-view="uploads">Uploads</button><button data-view="sources">Sources</button><button data-view="run">Run policy</button></nav><div class="pm-heading"><h2 data-title>Profiles</h2><span class="small" data-subtitle></span></div><div data-active class="active-strip"></div><div data-message class="notice" role="status" hidden></div><div data-content></div><dialog data-dialog aria-labelledby="policy-dialog-title"><h3 id="policy-dialog-title"></h3><p data-dialog-body></p><div data-dialog-extra></div><div data-dialog-actions class="actions"></div></dialog></div>`;
    this.root=this.element.querySelector('#policy-manager');
    this.root.addEventListener('click',event=>this.click(event));
    this.root.addEventListener('input',event=>this.input(event));
    this.root.addEventListener('change',event=>this.change(event));
    this.beforeUnload = event => {
      if(this.hasUnsavedChanges() || [...this.activeTransfers.values()].some(t=>t.running)) {
        event.preventDefault(); event.returnValue='';
      }
    };
    window.addEventListener('beforeunload',this.beforeUnload);
    this.keydown=event=>{
      if(this.visible && this.view==='editor' && (event.ctrlKey||event.metaKey) && event.key.toLowerCase()==='s') {
        event.preventDefault(); if(!this.mutating&&!this.modalResolve) void this.safe(()=>this.save());
      }
    };
    window.addEventListener('keydown',this.keydown);
    this.q('[data-dialog]').addEventListener('cancel',event=>{event.preventDefault();this.closeDialog('cancel');});
    this.render();
  }
  q(selector){return this.root.querySelector(selector);}
  async safe(fn){try{return await fn();}catch(error){this.show(message(error),true);this.onError(error);return null;}}
  show(text,error=false){this.error=error?text:'';this.info=error?'':text;const el=this.q('[data-message]');el.textContent=text;el.hidden=!text;el.classList.toggle('error-box',error);}
  hasUnsavedChanges(){return Boolean(this.draft?.dirty || this.importDirty);}
  setConnected(connected){this.connected=Boolean(connected);if(this.visible)this.renderActive();}
  updateState(state){this.robotState=state||{};this.resources=state?.resources||this.resources; if(this.visible && performance.now()-this.lastStateRender>300){this.lastStateRender=performance.now();this.renderActive();this.updateRunState();}}
  ownsOperation(operation=this.client.currentOperation, run=this.startedRun){
    if (!run?.tracking || !operation || operation.type!=='MODEL' || operation.armId!==run.armId) return false;
    if (run.requestId && operation.requestId && run.requestId!==operation.requestId) return false;
    if (run.operationId && operation.operationId && run.operationId!==operation.operationId) return false;
    return Boolean((run.requestId && run.requestId===operation.requestId) || (run.operationId && run.operationId===operation.operationId));
  }
  updateOperation(operation){
    this.operation=operation;
    if (this.ownsOperation(operation)) {
      this.startedRun.operation=operation;
      if(operation.operationId)this.startedRun.operationId=operation.operationId;
      if(operation.requestId)this.startedRun.requestId=operation.requestId;
      if(terminal(operation))this.startedRun.tracking=false;
    }
    if(this.visible){this.renderActive();this.updateRunState();}
  }
  setVisible(visible){this.visible=visible;if(visible){this.render();if(!this.loaded)void this.safe(()=>this.refresh());else void this.safe(()=>this.refreshRuntime());}this.schedulePoll();}
  schedulePoll(){
    clearTimeout(this.pollTimer);
    if(this.visible||this.activeTransfers.size||this.scanJobs.size||this.inspectionJob)this.pollTimer=setTimeout(async()=>{
      await this.safe(()=>this.pollOnce());this.schedulePoll();
    },2000);
  }
  async pollOnce(){
    const work=[this.refreshRuntime(),this.pollScans(),this.pollInspections()];
    if(this.uploads.length||this.activeTransfers.size)work.push(this.refreshUploads());
    const results=await Promise.allSettled(work),failure=results.find(r=>r.status==='rejected');
    if(failure)throw failure.reason;
  }
  trackScan(result){
    const job=result?.job??result?.scan_job??(result?.kind==='scan'?result:null);
    if(job?.id&&!completedJob(job))this.scanJobs.set(job.id,job);
    return job;
  }
  async pollScans(){
    let changed=false;
    for(const [id,previous]of [...this.scanJobs]){
      const response=await this.api.scanStatus(id),job=response?.job??response;
      if(!this.scanJobs.has(id))continue;
      if(completedJob(job)){
        this.scanJobs.delete(id);changed=true;
        if(job.state==='failed')this.show((job.errors||[]).map(e=>e.message||e).join('; ')||'Model scan failed.',true);
      }else this.scanJobs.set(id,job||previous);
    }
    if(changed)await this.refreshCatalog();
  }
  async refresh(){
    if(this.loading)return;this.loading=true;this.render();
    const search=this.search,catalogVersion=++this.catalogVersion,runtimeVersion=++this.runtimeVersion,profileListVersion=++this.profileListVersion;
    try{
      const [profiles,models,sources,runtime,uploads]=await Promise.all([this.api.profiles(),this.api.models(search),this.api.sources(),this.api.runtime(),this.api.uploads()]);
      if(profileListVersion===this.profileListVersion)this.profiles=profiles;
      if(catalogVersion===this.catalogVersion&&search===this.search){this.models=models;this.rememberModels(models);this.sources=sources;}
      if(runtimeVersion===this.runtimeVersion)this.runtime=runtime;
      this.uploads=uploads;this.loaded=true;if(!this.selectedModel&&this.models.length)this.selectedModel=this.models[0].id;
      if(!this.uploadSource)this.uploadSource=this.sources.roots.find(s=>s.enabled&&s.allow_upload&&s.writable!==false)?.id||'';
      this.show('');
    }finally{this.loading=false;this.render();}
  }
  async refreshRuntime(){
    const version=++this.runtimeVersion,result=await this.api.runtime();
    if(version!==this.runtimeVersion)return false;
    this.runtime=result;for(const job of result.jobs||[])if(job.kind==='scan')this.trackScan({job});
    this.renderActive();if(this.view==='run'&&!this.q('textarea:focus,input:focus,select:focus'))this.renderContent();
    return true;
  }
  applied(){return this.runtime.applied||null;}
  arms(){const v=this.resources.arms||[];return Array.isArray(v)?v:Object.entries(v).map(([id,a])=>({id,...a}));}
  cameras(){const v=this.resources.cameras||[];return (Array.isArray(v)?v:Object.values(v)).map(c=>typeof c==='string'?{id:c,label:c}:c);}
  cameraName(id){return this.cameras().find(c=>c.id===id)?.label||id;}
  rememberModels(models){this.modelCache??=new Map();for(const model of models||[])if(model?.id)this.modelCache.set(model.id,model);}
  model(id){return this.models.find(m=>m.id===id)||this.modelCache?.get(id);}
  value(){return this.draft?.parse().value||null;}
  modelFor(profile){
    if(!profile)return null;
    if(profile.execution?.kind==='remote'){
      const inspected=this.inspectedRemote;
      if(inspected?.draft===this.draft&&inspected.editVersion===this.draft?.editVersion&&descriptorMatches(inspected.descriptor,profile.execution))return inspected.model||inspected.descriptor;
      const candidates=(this.runtime.descriptors||[]).filter(d=>descriptorMatches(d,profile.execution));
      return candidates.length===1?candidates[0]:null;
    }
    return [...(this.modelCache?.values()||[]),...this.models].find(m=>m.source_id===profile.model?.source_id&&m.relative_path===profile.model?.relative_path&&(!profile.model.artifact_revision||m.artifact_revision===profile.model.artifact_revision));
  }
  profileRecord(id){return this.profiles.find(p=>p.id===id);}
  async dialog(title,body,choices,extra='') {
    if(this.modalResolve)return 'cancel';
    const el=this.q('[data-dialog]');this.focusBeforeDialog=document.activeElement;
    this.q('#policy-dialog-title').textContent=title;this.q('[data-dialog-body]').textContent=body;
    this.q('[data-dialog-extra]').innerHTML=extra;
    this.q('[data-dialog-actions]').innerHTML=choices.map(c=>`<button data-dialog-choice="${escape(c.value)}" class="${c.primary?'primary':''}">${escape(c.label)}</button>`).join('');
    const promise=new Promise(resolve=>this.modalResolve=resolve);el.showModal();return promise;
  }
  closeDialog(value){const resolve=this.modalResolve;this.modalResolve=null;this.q('[data-dialog]').close();if(this.focusBeforeDialog?.isConnected)this.focusBeforeDialog.focus();resolve?.(value);}
  async beforeLeave(){
    const draft=this.draft;
    if(draft?.dirty){
      const editVersion=draft.editVersion;
      const choice=await this.dialog('Save profile changes?',`Your edits to ${draft.id||'this profile'} have not been saved.`,[{value:'cancel',label:'Cancel'},{value:'discard',label:'Discard changes'},{value:'save',label:'Save profile',primary:true}]);
      if(choice==='cancel'||this.draft!==draft||draft.editVersion!==editVersion)return false;
      if(choice==='save'){if(!await this.save())return false;}
      else{if(draft.isNew)this.draft=null;else draft.discard();this.invalidateValidation();this.editorError='';}
    }
    if(this.importDirty){
      const version=this.importVersion;
      const choice=await this.dialog('Leave this profile import?','The selected or edited profile has not been imported.',[{value:'cancel',label:'Keep editing',primary:true},{value:'discard',label:'Discard import'}]);
      if(choice!=='discard'||version!==this.importVersion)return false;this.clearImport();
    }
    return true;
  }
  invalidateValidation({preserveInspection=false}={}){
    this.validationVersion+=1;this.validation=null;
    const inspected=this.inspectedRemote;
    if(preserveInspection&&inspected?.draft===this.draft&&descriptorMatches(inspected.descriptor,this.value()?.execution||{})){
      // A camera/arm binding or label edit does not change the server's input
      // schema. Retain that schema for the next form row, but invalidate the
      // validation of the bound profile itself.
      this.inspectedRemote={...inspected,editVersion:this.draft.editVersion};
    }else this.inspectedRemote=null;
  }
  draftToken(){return{draft:this.draft,editVersion:this.draft?.editVersion,navigationVersion:this.navigationVersion};}
  currentDraft(token,{allowEdits=false,allowNavigation=false}={}){
    return Boolean(token.draft&&this.draft===token.draft&&(allowEdits||this.draft.editVersion===token.editVersion)&&(allowNavigation||this.navigationVersion===token.navigationVersion));
  }
  installDraft(profile){
    this.draft=new ProfileDraft({id:profile.id,text:profile.text,revision:profile.revision});
    this.editorTab='form';this.invalidateValidation();this.validation=profile.validation||null;
    this.conflictRemote=null;this.editorError='';this.picker=false;if(profile.validation?.resolved_model)this.rememberModels([profile.validation.resolved_model]);
  }
  async navigate(view,{guard=true}={}){
    const version=++this.navigationVersion;
    if(view!==this.view&&guard&&!await this.beforeLeave())return false;
    if(version!==this.navigationVersion)return false;
    this.view=view;if(view!=='models')this.picker=false;this.error='';this.info='';this.render();return true;
  }
  async openProfile(id){
    const version=++this.navigationVersion;
    if(!await this.beforeLeave()||version!==this.navigationVersion)return false;
    const token=this.draftToken(),importVersion=this.importVersion;
    const p=await this.api.profile(id);
    if(version!==this.navigationVersion||this.draft!==token.draft||this.draft?.editVersion!==token.editVersion||importVersion!==this.importVersion)return false;
    this.installDraft(p);this.view='editor';this.render();return true;
  }
  async newProfile(model=null){
    const version=++this.navigationVersion;
    if(!await this.beforeLeave()||version!==this.navigationVersion)return false;
    const id=`profile_${crypto.randomUUID().slice(0,8)}`;
    const cameras=Object.fromEntries((model?.camera_inputs||[]).filter(s=>!s.masked).map(s=>[s.key,'']));
    const v={schema_version:1,id,name:'New policy profile',execution:{kind:'local',device:'cpu'},model:model?{source_id:model.source_id,relative_path:model.relative_path,artifact_revision:model.artifact_revision}:{source_id:'',relative_path:'',artifact_revision:''},bindings:{arm:'',cameras},extensions:{}};
    this.installDraft({id,text:stringify(v),revision:null});this.view='editor';this.render();return true;
  }
  setField(path,value){try{this.draft.setField(path,value);const key=Array.isArray(path)?path[0]:path;this.invalidateValidation({preserveInspection:key==='bindings'||key==='name'});this.editorError='';this.updateDraftIndicators();return true;}catch(e){this.editorError=message(e);this.show(this.editorError,true);return false;}}
  updateDraftIndicators(){
    const badge=this.q('[data-draft-state]');if(badge)badge.textContent=this.draft?.dirty?'UNSAVED':'SAVED';
    const apply=this.q('[data-apply-saved]');if(apply)apply.disabled=Boolean(this.draft?.dirty||this.draft?.isNew||this.mutating);
    const save=this.q('[data-action="save"]');if(save){save.disabled=this.mutating;save.textContent=this.mutating?'Saving…':'Save profile';}
  }
  async validate(){
    if(!this.draft)return false;const token=this.draftToken(),version=++this.validationVersion,parse=this.draft.parse();
    if(parse.error){this.editorError=parse.error;this.editorTab='raw';this.renderContent();return false;}
    const result=await this.api.validate(token.draft.text);
    if(!this.currentDraft(token)||version!==this.validationVersion)return false;
    this.validation=result;this.editorError=result.errors?.map(e=>typeof e==='string'?e:e.message).join('\n')||'';
    this.renderContent();return result.valid!==false;
  }
  async inspectRemote(){
    if(!this.draft)return false;
    const execution=this.value()?.execution;
    if(execution?.kind!=='remote')throw Error('Enter a valid remote execution profile before inspecting it.');
    const context={token:this.draftToken(),validationVersion:++this.validationVersion,version:++this.inspectionVersion,
      execution:{...execution},validation:null,job:null,polling:false,admitted:null};
    this._inspectionContext=context;this.inspectionJob=null;this.inspectedRemote=null;this.editorError='';
    this._inspectionStatus={state:'requesting',pending:true,jobId:null,message:'Requesting remote descriptor…',error:null,admitted:null};
    this.renderContent();
    let result;
    try{result=await this.api.inspectDraft(context.token.draft.text);}
    catch(error){return this.failInspection(context,message(error));}
    if(!this.currentInspection(context))return false;
    context.validation=result.validation??result;
    context.admitted=typeof result.admitted==='boolean'?result.admitted:null;
    this.validation={...context.validation,ready:false};
    if(result.admitted===true)return this.failInspection(context,'Inspection unexpectedly admitted a server session.');
    if(result.job){
      const job=result.job;
      if(!job.id)return this.failInspection(context,'Remote inspection returned a job without an identity.');
      context.job=job;this.inspectionJob=context;
      if(completedJob(job))return this.finishInspectionJob(context,job);
      this._inspectionStatus={state:job.state||'queued',pending:true,jobId:job.id,
        message:job.phase||'Reading remote descriptor…',error:null,admitted:result.admitted===false?false:null};
      this.renderContent();this.schedulePoll();return true;
    }
    return this.acceptInspection(context,result);
  }
  currentInspection(context){
    if(!context||context!==this._inspectionContext||context.version!==this.inspectionVersion||context.validationVersion!==this.validationVersion||!this.currentDraft(context.token))return false;
    const execution=this.value()?.execution;
    return execution?.kind==='remote'&&['endpoint','deployment','instance','expected_artifact','connection_profile'].every(key=>execution[key]===context.execution[key]);
  }
  get inspectionStatus(){
    if(!this._inspectionStatus||this._inspectionContext?.token.draft!==this.draft)return null;
    const cached=this.inspectedRemote;
    if(this._inspectionStatus.state==='complete'&&cached?.draft===this.draft&&cached.editVersion===this.draft.editVersion&&descriptorMatches(cached.descriptor,this.value()?.execution||{}))return{...this._inspectionStatus};
    if(!this.currentInspection(this._inspectionContext))return{state:'stale',pending:false,jobId:this._inspectionStatus.jobId,
      message:'Draft changed; inspect the remote descriptor again.',error:null,admitted:null};
    return{...this._inspectionStatus};
  }
  failInspection(context,error,state='failed'){
    if(!this.currentInspection(context))return false;
    this.inspectionJob=null;this.inspectedRemote=null;this.editorError=error;
    this.validation={...(context.validation||this.validation||{}),ready:false};
    this._inspectionStatus={state,pending:false,jobId:context.job?.id??null,message:error,error,admitted:context.admitted};
    this.renderContent();this.show(error,true);return false;
  }
  acceptInspection(context,result){
    if(!this.currentInspection(context))return false;
    if(typeof result?.admitted==='boolean')context.admitted=result.admitted;
    if(result?.admitted===true)return this.failInspection(context,'Inspection unexpectedly admitted a server session.');
    const raw=result?.descriptor;
    if(!raw||typeof raw!=='object'||Array.isArray(raw))return this.failInspection(context,'Remote inspection completed without a descriptor.');
    // Native DESCRIBE does not normally repeat its transport endpoint. A
    // supplied endpoint must match; only an absent one is filled from the exact
    // request whose completion we are accepting.
    const descriptor={...raw,endpoint:raw.endpoint??context.execution.endpoint,deployment:raw.deployment??context.execution.deployment};
    if(!descriptorMatches(descriptor,context.execution))return this.failInspection(context,'Remote descriptor differs from the requested endpoint, deployment, instance, or artifact.');
    const validation=(result.validation??(typeof result.valid==='boolean'?result:context.validation))||{};
    const model=remoteInputModel(descriptor,validation.resolved_model??result.resolved_model);
    this.inspectedRemote={draft:context.token.draft,editVersion:context.token.editVersion,descriptor,model};
    this.validation={...validation,ready:false};this.editorError=(validation.errors||[]).map(e=>e.message||e).join('\n');
    this.inspectionJob=null;
    this._inspectionStatus={state:'complete',pending:false,jobId:context.job?.id??null,
      message:'Remote descriptor inspected. No policy was prepared or started.',error:null,admitted:context.admitted};
    this.renderContent();return true;
  }
  finishInspectionJob(context,job){
    if(!this.currentInspection(context))return false;
    context.job=job;
    if(job.state!=='complete'){
      const errors=(job.errors||[]).map(e=>e?.message||String(e)).filter(Boolean);
      const error=errors.join('; ')||job.error?.message||job.error||job.reason||job.message||`Remote inspection ${job.state||'failed'}.`;
      return this.failInspection(context,String(error),job.state==='canceled'?'canceled':'failed');
    }
    return this.acceptInspection(context,job.result||{});
  }
  async pollInspections(){
    const context=this.inspectionJob;if(!context)return false;
    if(!this.currentInspection(context)){if(this.inspectionJob===context)this.inspectionJob=null;return false;}
    if(context.polling)return false;context.polling=true;
    try{
      const response=await this.api.preparationStatus(context.job.id);
      if(!this.currentInspection(context)||this.inspectionJob!==context){if(this.inspectionJob===context)this.inspectionJob=null;return false;}
      const job=response?.job??response;
      if(!job||job.id!==context.job.id)return this.failInspection(context,'Remote inspection status returned a different job identity.');
      if(completedJob(job))return this.finishInspectionJob(context,job);
      context.job=job;
      this._inspectionStatus={state:job.state||'running',pending:true,jobId:job.id,
        message:job.phase||'Reading remote descriptor…',error:null,admitted:context.admitted};
      this.renderContent();return true;
    }catch(error){
      if(!this.currentInspection(context)||this.inspectionJob!==context){if(this.inspectionJob===context)this.inspectionJob=null;return false;}
      if(error.status===404||error.status===410)return this.failInspection(context,`Remote inspection job is unavailable: ${message(error)}`);
      this._inspectionStatus={state:'retrying',pending:true,jobId:context.job.id,
        message:'Could not read inspection status; retrying.',error:message(error),admitted:context.admitted};
      this.renderContent();return false;
    }finally{context.polling=false;}
  }
  async refreshProfiles(){
    const version=++this.profileListVersion,profiles=await this.api.profiles();
    if(version===this.profileListVersion)this.profiles=profiles;return profiles;
  }
  async save(){
    if(!this.draft||this.mutating)return false;
    const token=this.draftToken(),draft=token.draft;let snapshot;
    try{snapshot=draft.prepareSave();}catch(e){this.editorError=e.code==='ID_CHANGED'?'The profile ID was changed. Use Save as… to create another profile, or restore the original ID.':message(e);this.editorTab='raw';this.renderContent();return false;}
    this.mutating=true;this.updateDraftIndicators();
    try{
      let saved;
      try{saved=await this.api.saveProfile(snapshot.id,snapshot.text,snapshot.revision);}
      catch(error){
        if(!this.currentDraft(token,{allowEdits:true}))return false;
        if(error.status===409||error.status===412||error.code==='REVISION_CONFLICT'){
          const remote=await this.api.profile(snapshot.id).catch(()=>null);
          if(!this.currentDraft(token,{allowEdits:true}))return false;
          const marked=draft.markConflict({...((remote||error.details)||{}),reason:message(error)},snapshot);
          if(marked.reason==='stale')return false;this.conflictRemote=remote;
        }
        this.editorError=message(error);this.renderContent();this.show(this.editorError,true);return false;
      }
      const acknowledged=draft.acknowledgeSave(snapshot,saved);
      // A list refresh failure must not turn an already committed save into an
      // apparent failed save or invite an accidental repeated create request.
      let listError='';try{await this.refreshProfiles();}catch(error){listError=` Profile list refresh failed: ${message(error)}`;}
      if(!acknowledged.acknowledged||!this.currentDraft(token,{allowEdits:true}))return false;
      this.editorError='';this.conflictRemote=null;this.show(`Saved ${saved.id}.yaml. The applied model is unchanged.${listError}`);
      this.renderContent();return !draft.dirty;
    }finally{this.mutating=false;this.updateDraftIndicators();}
  }
  async reloadDraft(){
    if(!this.draft)return false;const token=this.draftToken();
    if(token.draft.dirty){
      const choice=await this.dialog('Reload the saved profile?','This discards your current draft and loads the latest saved file.',[{value:'cancel',label:'Cancel',primary:true},{value:'reload',label:'Discard & reload'}]);
      if(choice!=='reload'||!this.currentDraft(token))return false;
    }
    const p=await this.api.profile(token.draft.id);
    if(!this.currentDraft(token))return false;
    token.draft.reset(p);this.invalidateValidation();this.conflictRemote=null;this.editorError='';this.validation=p.validation||null;this.renderContent();return true;
  }
  async copyDraft(){
    if(!this.draft||this.mutating)return false;const token=this.draftToken(),out=token.draft.parse();
    if(out.error){this.editorError=out.error;this.renderContent();return false;}
    const choice=await this.dialog('Save as a new profile','The existing saved profile will remain unchanged.',[{value:'cancel',label:'Cancel'},{value:'copy',label:'Save copy',primary:true}],field('New profile ID',`<input data-copy-id value="${escape(token.draft.id+'_copy')}" pattern="[a-z][a-z0-9_]*">`));
    if(choice!=='copy'||!this.currentDraft(token))return false;
    const id=this.q('[data-copy-id]').value.trim();
    if(!/^[a-z][a-z0-9_]{0,63}$/.test(id)){this.show('Use a lowercase profile ID with letters, numbers and underscores.',true);return false;}
    const copied=token.draft.cloneAs(id);this.mutating=true;
    try{
      const saved=await this.api.saveProfile(id,copied.text,null);
      await this.refreshProfiles().catch(()=>{});
      if(!this.currentDraft(token)){this.show(`Saved ${id}.yaml. Your newer draft was retained.`);return false;}
      this.installDraft(saved);this.renderContent();this.show(`Saved ${id}.yaml.`);return true;
    }finally{this.mutating=false;this.updateDraftIndicators();}
  }
  async apply(id,{mode}={}){
    if(this.mutating)return false;
    const record=this.draft?.id===id&&!this.draft.dirty?{revision:this.draft.revision}:this.profileRecord(id);
    const expectedRevision=record?.revision;if(!expectedRevision)throw Error('Refresh the saved profile before applying it.');
    if(!mode&&!terminal(this.client.currentOperation)){
      const choice=await this.dialog('Apply after the current operation?','The current operation keeps its existing model. Preparation can be queued at its completion.',[{value:'cancel',label:'Cancel'},{value:'after_current',label:'Apply after current',primary:true}]);
      if(choice!=='after_current')return false;mode=choice;
    }
    mode=mode||'idle';this.mutating=true;
    try{const result=await this.api.prepare(id,{expected_revision:expectedRevision,mode});await this.refreshRuntime();await this.client.refresh().catch(()=>{});this.show(result.message||'Profile preparation requested. Starting a policy remains a separate action.');this.render();return true;}
    finally{this.mutating=false;this.updateDraftIndicators();this.updateRunState();}
  }
  async scan(){const result=await this.api.scan();this.trackScan(result);this.show(result.message||'Model scan started.');await this.refreshCatalog();this.schedulePoll();}
  async refreshCatalog(){
    const version=++this.catalogVersion,search=this.search;
    const [models,sources]=await Promise.all([this.api.models(search),this.api.sources()]);
    if(version!==this.catalogVersion||search!==this.search)return false;
    this.models=models;this.rememberModels(models);this.sources=sources;if(this.view==='models'||this.view==='sources')this.renderContent();return true;
  }
  async reloadSources(){const result=await this.api.reloadSources();this.trackScan(result);this.show(result.message||'Source configuration reloaded; changed-source scan started.');await this.refreshCatalog();this.schedulePoll();}
  async chooseModel(){
    const token=this.draftToken();if(!token.draft)return false;
    this.picker=true;this.selectedModel=this.modelFor(this.value())?.id||null;this.view='models';this.search='';
    const version=++this.catalogVersion,models=await this.api.models();
    if(!this.currentDraft(token)||this.view!=='models'||!this.picker||version!==this.catalogVersion)return false;
    this.models=models;this.rememberModels(models);this.render();return true;
  }
  editExecution(kind){
    if(!this.draft)return false;
    const next=new ProfileDraft({id:this.draft.id,text:this.draft.text,revision:this.draft.revision});
    next.setField(['execution','kind'],kind);
    if(kind==='local'){
      if(!next.getField(['execution','device']))next.setField(['execution','device'],'cpu');
      for(const key of ['endpoint','deployment','expected_artifact','host_label','connection_profile','instance'])next.deleteField(['execution',key]);
    }else{
      next.deleteField(['execution','device']);
      for(const key of ['endpoint','deployment','expected_artifact'])if(next.getField(['execution',key])===undefined)next.setField(['execution',key],'');
      next.deleteField(['model']);
    }
    this.draft.setRaw(next.text);this.invalidateValidation();this.updateDraftIndicators();return true;
  }
  async useModel(){
    const model=this.model(this.selectedModel);if(!model||model.selectable===false)return false;
    if(!this.picker)return this.newProfile(model);
    const draft=this.draft;if(!draft)return false;const old=this.modelFor(this.value());
    const next=new ProfileDraft({id:draft.id,text:draft.text,revision:draft.revision});
    next.setField(['execution','kind'],'local');if(!next.getField(['execution','device']))next.setField(['execution','device'],'cpu');
    for(const key of ['endpoint','deployment','expected_artifact','host_label','connection_profile','instance'])next.deleteField(['execution',key]);
    for(const key of ['source_id','relative_path','artifact_revision'])next.setField(['model',key],model[key]);
    const required=new Set((model.camera_inputs||[]).filter(s=>!s.masked).map(s=>s.key));
    // Remove only inputs whose meaning was explicitly owned by a known previous
    // contract or is explicitly masked by the new checkpoint. Retain raw-only keys.
    for(const input of old?.camera_inputs||[])if(!required.has(input.key))next.deleteField(['bindings','cameras',input.key]);
    for(const input of model.camera_inputs||[]){
      if(input.masked)next.deleteField(['bindings','cameras',input.key]);
      else if(next.getField(['bindings','cameras',input.key])===undefined)next.setField(['bindings','cameras',input.key],'');
    }
    draft.setRaw(next.text);this.invalidateValidation();this.editorError='';this.picker=false;this.view='editor';this.editorTab='form';this.render();return true;
  }
  setImportText(text,{filename}={}){
    this.importText=text;this.importDirty=Boolean(text);this.importVersion+=1;this.importResult=null;this.importPreviewSnapshot=null;
    if(filename!==undefined)this.importFilename=filename;
  }
  clearImport(){this.setImportText('',{filename:'No profile selected'});this.importDirty=false;}
  async loadImportFile(file){
    if(!file)return false;const limit=this.sources?.limits?.profile_bytes||65536;if(file.size>limit)throw Error(`Profile exceeds ${Math.round(limit/1024)} KiB.`);
    const read=++this.importReadVersion,version=this.importVersion,navigation=this.navigationVersion;
    const text=await file.text();
    if(read!==this.importReadVersion||version!==this.importVersion||navigation!==this.navigationVersion||this.view!=='import')return false;
    this.setImportText(text,{filename:file.name});this.importDirty=true;this.renderContent();return true;
  }
  async importPreview(){
    const snapshot={text:this.importText,version:this.importVersion,navigation:this.navigationVersion,mode:this.importMode,newId:this.importNewId},request=++this.importPreviewVersion;
    const result=await this.api.importPreview(snapshot.text);
    if(snapshot.text!==this.importText||snapshot.version!==this.importVersion||snapshot.navigation!==this.navigationVersion||request!==this.importPreviewVersion)return false;
    this.importResult=result;this.importPreviewSnapshot=snapshot;
    if(this.importMode===snapshot.mode)this.importMode='new';
    if(this.importNewId===snapshot.newId)this.importNewId=(result.profile?.id||'imported_profile')+'_copy';
    this.renderContent();return result.valid;
  }
  async commitImport(){
    const preview=this.importPreviewSnapshot;
    if(this.mutating||!this.importResult?.valid||!preview||preview.text!==this.importText||preview.version!==this.importVersion)return false;
    const request={text:preview.text,mode:this.importResult.conflict?this.importMode:'new',id:this.importResult.conflict&&this.importMode==='new'?this.importNewId:this.importResult.profile.id,revision:this.importResult.conflict?.revision??null};
    const navigation=this.navigationVersion,mode=this.importMode,newId=this.importNewId;this.mutating=true;
    try{
      const result=await this.api.importProfile(request);await this.refreshProfiles().catch(()=>{});
      const current=preview.version===this.importVersion&&preview.text===this.importText&&mode===this.importMode&&newId===this.importNewId&&navigation===this.navigationVersion;
      if(current){this.clearImport();await this.navigate('profiles',{guard:false});}
      this.show(`Imported ${result.id}. No model was applied.${current?'':' Your newer import draft was retained.'}`);return true;
    }finally{this.mutating=false;}
  }
  render(){const labels={profiles:'Profiles',editor:'Edit profile',models:this.picker?'Choose model':'Model library',import:'Import profile',uploads:'Model uploads',sources:'Model sources',run:'Policy execution'};this.q('[data-title]').textContent=labels[this.view];this.root.querySelectorAll('.pm-nav [data-view]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.view===(['editor','import'].includes(this.view)?'profiles':this.view))));this.renderActive();this.renderContent();this.show(this.error||this.info,Boolean(this.error));}
  renderActive(){const a=this.applied(),op=this.client.currentOperation;const html=`<span class="eyebrow">Applied profile</span><div class="summary"><strong>${escape(a?.name||a?.id||'No managed profile applied')}</strong><div class="meta">${a?escape(a.state||'Unknown')+' · '+escape(a.execution||''):'Choose a saved profile to prepare its model.'}${op&&!terminal(op)?' · operation '+escape(op.state):''}</div>${this.runtime.pending?`<div class="pending">Pending: ${escape(this.runtime.pending.name||this.runtime.pending.id)}</div>`:''}</div><button data-view="run">Run policy</button>`;const active=this.q('[data-active]');if(this.lastActiveHTML!==html){active.innerHTML=html;this.lastActiveHTML=html;}}
  captureFocus(){const el=document.activeElement;if(!el||!this.root.contains(el))return null;const attributes=['data-raw','data-import-text','data-search','data-field','data-instruction','data-run-duration','data-upload-name','data-import-id'];const attr=attributes.find(a=>el.hasAttribute(a));if(!attr)return null;const value=el.getAttribute(attr);return {selector:value?`[${attr}="${CSS.escape(value)}"]`:`[${attr}]`,start:el.selectionStart,end:el.selectionEnd,scroll:el.scrollTop};}
  restoreFocus(saved){if(!saved)return;const el=this.q(saved.selector);if(!el)return;el.focus({preventScroll:true});if(typeof saved.start==='number'){try{el.setSelectionRange(saved.start,saved.end);}catch{}}el.scrollTop=saved.scroll||0;}
  renderContent(){const focus=this.captureFocus();const content=this.q('[data-content]');if(this.loading&&!this.loaded){content.innerHTML='<div class="empty">Loading policy management…</div>';return;}if(!this.loaded){content.innerHTML='<div class="empty"><p>Policy management is not available yet.</p><div class="actions"><button data-action="refresh">Retry connection</button></div></div>';return;}content.innerHTML=({profiles:()=>this.profilesPage(),editor:()=>this.editorPage(),models:()=>this.modelsPage(),import:()=>this.importPage(),uploads:()=>this.uploadsPage(),sources:()=>this.sourcesPage(),run:()=>this.runPage()}[this.view]||(()=>''))();this.restoreFocus(focus);}
  profilesPage(){return `<div class="toolbar"><div><span class="eyebrow">${this.profiles.length} saved profiles</span><div class="profile-location">${escape(this.sources.profile_store||'Profile store')}</div></div><div class="buttons"><button data-action="refresh-profiles">Refresh profiles</button><button data-view="import">Import profile</button><button data-action="new-profile" class="primary">New profile</button></div></div>${!this.profiles.length?'<div class="empty">No profiles yet. Create one from a model or import a written profile.</div>':`<div class="cards-grid">${this.profiles.map(p=>`<article class="preset-card"><h3>${escape(p.name||p.id)}</h3><div class="badge-row">${p.applied?'<span class="tag black">APPLIED</span>':''}<span class="tag">${escape(p.state||'Saved')}</span></div><div class="meta">${escape(p.category||'Model not resolved')} / ${escape(p.execution||p.document?.execution?.kind||'Unknown')}</div><div class="path">${escape(p.model_reference||p.id)}</div><div class="bindings">${escape(p.camera_summary||'')}</div>${p.reason?`<div class="small">${escape(p.reason)}</div>`:''}<div class="actions"><button data-edit="${escape(p.id)}">Edit profile</button><button data-apply="${escape(p.id)}" class="primary" ${p.can_apply===false?'disabled':''}>Apply model</button></div></article>`).join('')}</div>`}`;}
  editorPage(){if(!this.draft)return '<div class="empty">Open or create a profile.</div>';const parsed=this.draft.parse(),p=parsed.value,m=this.modelFor(p);return `<div class="toolbar"><button data-view="profiles" class="link">Back to profiles</button><div class="buttons"><span class="tag" data-draft-state>${this.draft.dirty?'UNSAVED':'SAVED'}</span><button data-action="validate">Validate</button><button data-action="save" class="primary" ${this.mutating?'disabled':''}>${this.mutating?'Saving…':'Save profile'}</button><button data-action="apply-saved" data-apply-saved ${this.draft.dirty||this.draft.isNew||this.mutating?'disabled':''}>Apply saved</button></div></div><div class="duo"><section class="panel"><div class="pane-tabs" aria-label="Profile editor"><button data-editor-tab="form" aria-selected="${this.editorTab==='form'}">Form</button><button data-editor-tab="raw" aria-selected="${this.editorTab==='raw'}">Raw YAML / JSON</button></div>${this.editorTab==='raw'?field('Raw profile',`<textarea class="raw-editor" data-raw spellcheck="false">${escape(this.draft.text)}</textarea>`):parsed.error?`<div class="error-box">${escape(parsed.error)}</div>`:this.profileForm(p,m)}${this.editorError?`<div class="error-box" role="alert">${escape(this.editorError)}</div>`:''}${this.validation?`<div class="${this.validation.valid?'ok-box':'error-box'}">${escape(this.validation.message|| (this.validation.valid?'Profile format accepted.':'Validation failed.'))}${this.validation.warnings?.length?'<br>'+this.validation.warnings.map(w=>escape(typeof w==='string'?w:w.message)).join('<br>'):''}</div>`:''}${this.draft.conflict?`<section class="pm-conflict"><strong>The saved profile changed outside this editor.</strong><p>Your draft has been retained. Compare with the saved version before choosing how to continue.</p>${this.conflictRemote?`<details><summary>Latest saved revision</summary><pre>${escape(this.conflictRemote.text)}</pre></details>`:''}<div class="actions"><button data-action="reload-draft">Discard & reload saved</button><button data-action="copy-draft">Save as new profile</button><button data-action="rebase-draft">Keep draft with latest base</button></div></section>`:''}</section><aside class="panel"><h3>Profile context</h3><div class="fact"><span>File</span><strong class="mono">${escape(this.draft.id)}.yaml</strong></div><div class="fact"><span>Model category</span><strong>${escape(m?.category||this.validation?.category||'Not resolved')}</strong></div><div class="fact"><span>Revision</span><strong class="mono">${escape(this.draft.revision||'New file')}</strong></div><div class="schema-note mono">${escape(p?.execution?.kind==='remote'?`${p.execution.endpoint||''}\n${p.execution.deployment||''}`:p?.model?`${p.model.source_id||''} / ${p.model.relative_path||''}`:'No model selected')}</div><p class="note">Form and Raw share one draft. Saving does not replace the currently applied model.</p><div class="actions"><button data-action="export">Export draft</button><button data-action="copy-draft">Save as…</button><button data-action="reload-draft" ${this.draft.isNew?'disabled':''}>Reload saved</button></div></aside></div>`;}
  roleTemplate(m){const slots=m?.camera_inputs||[];return m?.binding_template==='soarm'||(String(m?.category||'').toLowerCase()==='smolvla'&&slots.length<=3&&slots.some(s=>s.key==='observation.images.camera1')&&slots.some(s=>s.key==='observation.images.camera2')&&slots.every(s=>s.masked||/^observation\.images\.camera[123]$/.test(s.key)));}
  cameraRows(m){return (m?.camera_inputs||[]).map(s=>({...s,role:s.role||(this.roleTemplate(m)?(s.masked?'Additional':({'observation.images.camera1':'Global','observation.images.camera2':'Wrist','observation.images.camera3':'Additional'}[s.key])):null)}));}
  profileForm(p,m){return `<div class="fields">${field('Profile name',`<input data-field="name" value="${escape(p.name)}">`)}${field('Execution',`<select data-execution>${option('local','Local on robot host',p.execution?.kind)}${option('remote','Remote inference server',p.execution?.kind)}</select>`)}${field('Arm',`<select data-arm><option value="">Select an arm</option>${this.arms().map(a=>option(a.id,a.label||a.id,p.bindings?.arm)).join('')}</select>`)}</div>${p.execution?.kind==='remote'?`<div class="block"><div class="fields">${field('Server endpoint',`<input data-field="endpoint" value="${escape(p.execution.endpoint||'')}" placeholder="Configured inference endpoint">`)}${field('Deployment',`<input data-field="deployment" value="${escape(p.execution.deployment||'')}">`)}</div><div class="actions"><button data-action="inspect-remote" ${this.inspectionStatus?.pending?'disabled':''}>${this.inspectionStatus?.pending?'Inspecting server…':'Read server descriptor'}</button></div><p class="note" data-inspection-status>${escape(this.inspectionStatus?.message||'Weights are managed on the inference host. No Pi checkpoint path is required.')}</p></div>`:`<div class="block"><span class="eyebrow">Selected checkpoint</span><div class="model-choice"><strong>${escape(m?.name||'No catalog model selected')}</strong><div class="mono">${escape(p.model?.source_id||'—')} / ${escape(p.model?.relative_path||'—')}</div></div><button data-action="choose-model">Choose from model library</button></div>`}<div class="block"><div class="block-title"><span class="eyebrow">Camera bindings</span>${m?`<span class="tag">${this.roleTemplate(m)?'SO-ARM TEMPLATE':'CHECKPOINT-DEFINED'}</span>`:''}</div>${m?.camera_inputs?.length?this.cameraRows(m).map(s=>`<div class="binding"><div><strong>${escape(s.role||s.key.split('.').at(-1))}</strong><div class="key">${escape(s.key)}</div></div>${s.masked?'<div class="masked">Masked by checkpoint</div>':`<select data-camera="${escape(s.key)}" aria-label="${escape(s.role||s.key)} camera"><option value="">Unbound — required</option>${this.cameras().map(c=>option(c.id,c.label||c.id,p.bindings?.cameras?.[s.key])).join('')}</select>`}</div>`).join(''):'<p class="note">Choose or inspect a model to read its input contract. Existing advanced bindings remain in Raw.</p>'}<p class="note">Use the views that match training. A masked input is defined by the checkpoint, not by a disconnected camera.</p></div>`;}
  modelsPage(){const groups=new Map();for(const m of this.models){if(!groups.has(m.source_id))groups.set(m.source_id,[]);groups.get(m.source_id).push(m);}const m=this.model(this.selectedModel);return `<div class="toolbar"><span class="eyebrow">${this.picker?'Select a model for this draft':'Checkpoint catalog'}</span><div class="buttons"><button data-action="scan">Refresh models</button><button data-view="uploads" class="primary">Upload model</button></div></div><div class="duo"><section>${field('Search all configured model folders',`<input type="search" data-search placeholder="Search name, category or path…" value="${escape(this.search)}">`)}<div class="small" style="margin:10px 0">${this.models.length} models · grouped by source</div><div class="model-scroll" aria-label="Model results grouped by folder">${[...groups].map(([id,list])=>{const s=this.sources.roots.find(s=>s.id===id);return `<section class="model-group"><h3>${escape(s?.label||id)}<small>${escape(s?.resolved_path||s?.path||'Source unavailable')}</small></h3>${list.map(m=>`<button class="model-row" data-model="${escape(m.id)}" aria-pressed="${m.id===this.selectedModel}"><span><strong>${escape(m.name)}</strong><span class="small" style="display:block">${escape(m.category||'Unknown')} · ${bytes(m.size_bytes)}</span><span class="mono">${escape(m.relative_path)}</span></span><span class="tag">${escape(m.state||'Unknown')}</span></button>`).join('')}</section>`;}).join('')||'<div class="empty">No models found. Refresh after copying a checkpoint into a configured source, or upload a model bundle.</div>'}</div></section><aside class="panel">${m?`<span class="eyebrow">Checkpoint details</span><h2 style="margin:9px 0 14px">${escape(m.name)}</h2><div class="fact"><span>Category</span><strong>${escape(m.category||'Unknown')}</strong></div><div class="fact"><span>Source</span><strong>${escape(m.source_id)}</strong></div><div class="fact"><span>State</span><strong>${escape(m.state)}</strong></div><div class="schema-note mono">${escape(m.relative_path)}<br>${escape(m.artifact_revision||'Identity not yet available')}</div>${m.reason?`<p class="note">${escape(m.reason)}</p>`:''}<div class="file-list">${(m.files||[]).slice(0,12).map(f=>`<div class="file-row"><span class="mono">${escape(typeof f==='string'?f:f.path)}</span></div>`).join('')}</div><div class="actions"><button data-action="use-model" class="primary" ${m.selectable===false?'disabled':''}>${this.picker?'Use this model':'Create profile'}</button>${this.picker?'<button data-action="cancel-picker">Cancel selection</button>':''}</div>`:'<h3>Select a model</h3><p class="note">Inspect its files, input contract and readiness.</p>'}</aside></div>`;}
  importPage(){const r=this.importResult;return `<div class="toolbar"><button data-view="profiles" class="link">Back to profiles</button><span class="small">${escape(this.sources.profile_store||'Profile store')}</span></div><div class="duo"><section class="panel"><h3>Import a written profile</h3><input type="file" data-profile-file accept=".yaml,.yml,.json" class="hidden-input"><button data-action="pick-profile">Choose profile file</button><p class="small">${escape(this.importFilename)}</p>${field('Review or paste the profile',`<textarea data-import-text class="raw-editor" spellcheck="false">${escape(this.importText)}</textarea>`)}<div class="actions"><button data-action="validate-import" class="primary">Validate import</button></div></section><aside class="panel"><h3>Import preview</h3>${!r?'<p class="note">Import accepts a Station profile. Model files are uploaded separately.</p>':r.valid?`<div class="fact"><span>Profile</span><strong>${escape(r.profile.name)}</strong></div><div class="fact"><span>ID</span><strong>${escape(r.profile.id)}</strong></div><div class="ok-box">Profile format accepted.</div>${r.warnings?.length?`<p class="note">${r.warnings.map(w=>escape(w.message||w)).join('<br>')}</p>`:''}${r.conflict?`<div class="schema-note">A profile with this ID already exists. Active runs keep their current revision.</div><label class="radio-line"><input type="radio" name="policy-import-mode" value="new" ${this.importMode==='new'?'checked':''}><span>Import as a new profile</span></label>${field('New profile ID',`<input data-import-id value="${escape(this.importNewId)}">`)}<label class="radio-line"><input type="radio" name="policy-import-mode" value="replace" ${this.importMode==='replace'?'checked':''}><span>Replace the saved revision</span></label>`:''}<div class="actions"><button data-action="commit-import" class="primary">Import profile</button></div>`:`<div class="error-box">${escape((r.errors||[]).map(e=>e.message||e).join('\n')||r.message||'Invalid profile')}</div>`}</aside></div>`;}
  sourcesPage(){return `<div class="toolbar"><div><span class="eyebrow">Effective model sources</span><div class="profile-location">${escape(this.sources.config_path||'')}</div></div><div class="buttons"><button data-action="reload-sources" class="primary">Reload source config</button><button data-action="scan">Refresh models</button></div></div><div class="duo"><section class="panel"><h3>Configured sources</h3>${this.sources.roots.map(s=>`<article class="source-item"><div class="row spread"><strong>${escape(s.label||s.id)}</strong><span class="tag">${escape(s.state||(s.enabled?'Enabled':'Disabled'))}</span></div><div class="mono">${escape(s.resolved_path||s.path)}</div><p class="note">${escape(s.id)} · uploads ${s.allow_upload&&s.writable!==false?'allowed':'not allowed'}${s.reason?' · '+escape(s.reason):''}</p></article>`).join('')||'<div class="empty">No model sources configured.</div>'}<div class="source-item"><strong>Profile store</strong><div class="mono">${escape(this.sources.profile_store||'')}</div></div><p class="note">Edit source paths with VS Code SSH or another host editor, then reload here. This page is read-only.</p></section><aside class="panel"><h3>Source configuration</h3><pre class="config-view">${escape(this.sources.text||'Configuration text is unavailable.')}</pre><p class="note">Reload validates the source file and scans changed folders. Refresh models discovers files copied into existing folders. Neither replaces an active policy.</p></aside></div>`;}
  uploadsPage(){const roots=this.sources.roots.filter(s=>s.enabled&&s.allow_upload&&s.writable!==false);return `<div class="duo"><section class="panel"><h3>${this.resumeTarget?'Resume model upload':'Upload a model bundle'}</h3><div class="fields">${field('Bundle format',`<select data-upload-kind ${this.resumeTarget?'disabled':''}>${option('folder','Checkpoint folder',this.uploadKind)}${option('archive','ZIP archive',this.uploadKind)}</select>`)}${field('Destination source',`<select data-upload-source ${this.resumeTarget?'disabled':''}><option value="">Select a writable source</option>${roots.map(s=>option(s.id,s.label||s.id,this.uploadSource)).join('')}</select>`)}</div><input type="file" data-model-files class="hidden-input" ${this.uploadKind==='folder'?'webkitdirectory multiple':'accept=".zip"'}><div class="actions"><button data-action="pick-model">${this.uploadKind==='folder'?'Choose checkpoint folder':'Choose ZIP archive'}</button></div><p class="note">Include the weights, configuration and saved processors.</p>${this.uploadFiles.length?`<div class="model-choice"><strong>${escape(this.uploadLabel)}</strong><span class="small">${this.uploadFiles.length} files · ${bytes(this.uploadFiles.reduce((n,f)=>n+f.size,0))}</span></div>`:'<div class="empty">No bundle selected.</div>'}${field('New model directory name',`<input data-upload-name ${this.resumeTarget?'readonly':''} value="${escape(this.uploadName)}" placeholder="my-policy-v2">`)}<p class="note mono" data-upload-destination>${escape(roots.find(s=>s.id===this.uploadSource)?.resolved_path||roots.find(s=>s.id===this.uploadSource)?.path||'')} / ${escape(this.uploadName)}</p><div class="actions"><button data-action="upload" class="primary" ${!this.uploadFiles.length||!this.uploadSource?'disabled':''}>${this.resumeTarget?'Resume upload':'Upload model'}</button>${this.resumeTarget?'<button data-action="cancel-resume">Cancel file reselection</button>':''}</div><p class="note">Transfers are staged, verified and then indexed. Existing model directories are not overwritten.</p></section><aside class="panel"><h3>Upload activity</h3><div data-upload-jobs>${this.uploadJobs()}</div><p class="note">Uploads go to the configured folders on this backend host. Remote inference hosts keep their own model installations.</p></aside></div>`;}
  uploadJobs(){return this.uploads.map(j=>`<article class="job" data-upload-job="${escape(j.id)}"><div class="job-head"><strong>${escape(j.name||j.id)}</strong><span class="job-state">${escape(j.state)}</span></div><div class="mono">${escape(j.source_id||'')} / ${escape(j.destination_name||j.name||'')}</div><div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(100,Math.round(100*(j.received_bytes||0)/(j.total_bytes||1)))}"><div class="progress-fill" style="width:${Math.min(100,100*(j.received_bytes||0)/(j.total_bytes||1))}%"></div></div><div class="small" data-transfer-bytes>${bytes(j.received_bytes||0)} / ${bytes(j.total_bytes)} transferred</div>${j.reason?`<p class="note">${escape(j.reason)}</p>`:''}<div class="actions">${this.activeTransfers.get(j.id)?.running?`<button data-pause-upload="${escape(j.id)}">Pause</button>`:!['complete','ready','canceled','failed'].includes(j.state)?`<button data-resume-upload="${escape(j.id)}">Resume / reselect files</button>`:''}${!['complete','ready','canceled'].includes(j.state)?`<button data-cancel-upload="${escape(j.id)}">Cancel upload</button>`:''}${['complete','ready'].includes(j.state)?'<button data-view="models">View model library</button>':''}</div></article>`).join('')||'<div class="empty">No upload jobs yet.</div>';}
  renderUploadJobs(){
    const box=this.q('[data-upload-jobs]');if(!box)return;
    const key=JSON.stringify(this.uploads.map(j=>[j.id,j.state,j.reason||'',j.name,j.source_id,j.destination_name,Boolean(this.activeTransfers.get(j.id)?.running)]));
    if(box.dataset.signature!==key){box.innerHTML=this.uploadJobs();box.dataset.signature=key;return;}
    for(const j of this.uploads){const row=box.querySelector(`[data-upload-job="${CSS.escape(j.id)}"]`);if(!row)continue;const progress=Math.min(100,100*(j.received_bytes||0)/(j.total_bytes||1));const bar=row.querySelector('.progress-track');bar?.setAttribute('aria-valuenow',String(Math.round(progress)));const fill=row.querySelector('.progress-fill');if(fill)fill.style.width=progress+'%';const text=row.querySelector('[data-transfer-bytes]');if(text)text.textContent=`${bytes(j.received_bytes||0)} / ${bytes(j.total_bytes)} transferred`;}
  }
  runPage(){const a=this.applied();if(!a)return '<div class="empty">Apply a saved profile first. A prepared model will appear here.</div>';const language=a.language_conditioned!==false,op=this.client.currentOperation,own=this.ownsOperation(op),active=own&&!terminal(op);return `<div class="duo"><section class="panel"><div class="row spread"><h2>${escape(a.name||a.id)}</h2><span class="tag" data-run-phase>${escape(op?.state||a.state||'Unknown')}</span></div><div class="fact"><span>Arm</span><strong>${escape(a.arm_id||'Unbound')}</strong></div><div class="fact"><span>Execution</span><strong>${escape(a.execution||'')}</strong></div>${language?`${this.startedRun?.instruction?`<span class="eyebrow" style="display:block;margin-top:14px">Current instruction</span><div class="current-instruction">${escape(this.startedRun.instruction)}</div>`:''}${field(active?'New instruction draft':'Instruction',`<textarea data-instruction>${escape(this.runInstruction)}</textarea>`)}`:'<p class="note">This policy has no language input.</p>'}${!active?`<div class="fields">${field('Delivery',`<select data-run-mode>${option('STREAM','Continuous until stopped',this.runMode)}${option('INLINE','Bounded run',this.runMode)}</select>`)}${this.runMode==='INLINE'?field('Duration · seconds',`<input data-run-duration type="number" min="0.1" step="0.1" value="${escape(this.runDuration)}">`):''}</div>`:''}<div class="actions">${active?`${language&&op.mode==='STREAM'?`<button data-action="update-instruction" class="primary" ${this.canUpdateInstruction()?'':'disabled'}>Update instruction</button>`:''}<button data-action="stop-policy" class="stop">Stop policy</button>`:`<button data-action="start-policy" class="primary" ${!this.connected||a.state!=='ready'||!terminal(op)||this.mutating?'disabled':''}>Start policy</button>`}<button data-view="profiles">Choose profile</button></div><p class="note" data-run-note>${escape(a.reason||'Start is explicit. Preparing a model does not start motion.')}</p></section><aside class="panel"><h3>Execution identity</h3><div class="mono">${escape(a.runtime_profile_id||a.id)}</div><p class="note">${escape(a.artifact_revision||'')}</p>${this.runtime.pending?`<div class="schema-note">Pending profile: ${escape(this.runtime.pending.name||this.runtime.pending.id)}</div>`:''}</aside></div>`;}
  updateRunState(){
    const op=this.client.currentOperation,owned=this.ownsOperation(op);
    const phase=this.q('[data-run-phase]');if(phase)phase.textContent=(owned?op.state:this.applied()?.state)||'Unknown';
    const update=this.q('[data-action="update-instruction"]');if(update)update.disabled=!this.canUpdateInstruction();
    const stop=this.q('[data-action="stop-policy"]');if(stop)stop.disabled=!owned||terminal(op);
    const start=this.q('[data-action="start-policy"]');if(start)start.disabled=!this.connected||this.applied()?.state!=='ready'||!terminal(op)||this.mutating;
  }
  async startPolicy(){
    const applied=this.applied();if(!applied||this.mutating)return false;
    const a={...applied},instruction=this.runInstruction,mode=this.runMode,duration=Number(this.runDuration);
    if(a.state!=='ready')throw Error('Wait for the applied policy to finish preparation.');
    if(a.language_conditioned!==false&&!instruction.trim())throw Error('Enter an instruction.');
    if(mode==='INLINE'&&(!Number.isFinite(duration)||duration<=0))throw Error('Enter a positive run duration.');
    const generation=++this.runGeneration;this.mutating=true;
    let run=null;
    try{
      await this.client.refresh();if(!terminal(this.client.currentOperation))throw Error('End the current operation before starting another.');
      await this.client.enable(a.arm_id);
      run={armId:a.arm_id,profileId:a.id,instruction,mode,generation,tracking:false,operationId:null,requestId:null,pending:true};
      this.startedRun=run;
      const result=await this.client.submit(a.arm_id,'MODEL',mode,{instruction},a.runtime_profile_id||a.id,mode==='INLINE'?{duration_s:duration}:{});
      if(this.startedRun!==run||generation!==this.runGeneration)return false;
      // Only the response to our own submit establishes ownership. Early global
      // callbacks with no IDs can describe another operation and cannot bind it.
      run.requestId=result?.requestId||null;run.operationId=result?.operationId||null;run.pending=false;
      run.tracking=Boolean(run.requestId||run.operationId);
      if(!run.tracking)throw Error('The policy start returned no operation identity.');
      const current=this.client.currentOperation;
      if(this.ownsOperation(current,run))this.updateOperation(current);
      else run.tracking=false;
      this.renderContent();return true;
    }catch(error){if(this.startedRun===run&&run){run.pending=false;run.tracking=false;}throw error;}
    finally{this.mutating=false;this.updateRunState();}
  }
  canUpdateInstruction(){
    const op=this.client.currentOperation;
    return this.ownsOperation(op)&&!terminal(op)&&op.mode==='STREAM'&&op.ready===true&&!this.instructionPending;
  }
  async updateInstruction(){
    const instruction=this.runInstruction,run=this.startedRun;
    if(!instruction.trim())throw Error('Enter an instruction.');
    if(!this.canUpdateInstruction())throw Error('Wait for this policy’s owned continuous stream to become ready.');
    this.instructionPending=true;this.updateRunState();
    try{
      await this.client.updateStream({instruction});
      if(this.startedRun!==run||!this.ownsOperation(this.client.currentOperation,run)||terminal(this.client.currentOperation))return false;
      run.instruction=instruction;this.renderContent();this.show('Instruction acknowledged.');return true;
    }finally{this.instructionPending=false;this.updateRunState();}
  }
  async stopPolicy(){
    const run=this.startedRun,operation=this.client.currentOperation;
    if(!this.ownsOperation(operation,run)||terminal(operation))throw Error('This panel does not own the current policy operation.');
    await this.client.endOperation();
    if(this.startedRun===run&&this.visible)this.renderContent();return true;
  }
  async click(event){const b=event.target.closest('button');if(!b||b.disabled||!this.root.contains(b))return;if(b.dataset.dialogChoice){this.closeDialog(b.dataset.dialogChoice);return;}void this.safe(async()=>{
    if(b.dataset.view){await this.navigate(b.dataset.view);return;}if(b.dataset.edit){await this.openProfile(b.dataset.edit);return;}if(b.dataset.apply){await this.apply(b.dataset.apply);return;}
    if(b.dataset.editorTab){if(b.dataset.editorTab==='form'&&this.draft.parse().error){this.editorError=this.draft.parse().error;this.renderContent();return;}this.editorTab=b.dataset.editorTab;this.renderContent();return;}
    if(b.dataset.model){this.selectedModel=b.dataset.model;this.renderContent();return;}
    if(b.dataset.pauseUpload){await this.pauseUpload(b.dataset.pauseUpload);return;}if(b.dataset.resumeUpload){await this.resumeUpload(b.dataset.resumeUpload);return;}if(b.dataset.cancelUpload){await this.cancelUpload(b.dataset.cancelUpload);return;}
    switch(b.dataset.action){case'refresh':await this.refresh();break;case'refresh-profiles':await this.api.refreshProfiles();await this.refreshProfiles();this.renderContent();break;case'new-profile':await this.newProfile();break;case'validate':await this.validate();break;case'save':await this.save();break;case'apply-saved':await this.apply(this.draft.id);break;case'reload-draft':await this.reloadDraft();break;case'copy-draft':await this.copyDraft();break;case'rebase-draft':if(this.conflictRemote){this.draft.rebase(this.conflictRemote);this.conflictRemote=null;this.editorError='';this.invalidateValidation();this.renderContent();this.show('Latest saved revision is now the base. Review the retained draft, then Save.');}break;case'choose-model':await this.chooseModel();break;case'use-model':await this.useModel();break;case'cancel-picker':this.picker=false;this.view='editor';this.navigationVersion+=1;this.render();break;case'scan':await this.scan();break;case'reload-sources':await this.reloadSources();break;case'inspect-remote':await this.inspectRemote();break;case'pick-profile':this.q('[data-profile-file]').click();break;case'validate-import':await this.importPreview();break;case'commit-import':await this.commitImport();break;case'pick-model':this.q('[data-model-files]').click();break;case'upload':await this.startUpload();break;case'cancel-resume':this.resumeTarget=null;this.uploadFiles=[];this.uploadLabel='';this.renderContent();break;case'start-policy':await this.startPolicy();break;case'update-instruction':await this.updateInstruction();break;case'stop-policy':await this.stopPolicy();break;case'export':{const parsed=this.draft.parse();if(parsed.error)throw Error(parsed.error);const url=URL.createObjectURL(new Blob([this.draft.text],{type:'application/yaml'}));const a=document.createElement('a');a.href=url;a.download=this.draft.id+'.yaml';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);break;}
    }
  });}
  input(event){const t=event.target;if(t.matches('[data-raw]')){this.draft.setRaw(t.value);this.invalidateValidation();this.editorError='';this.updateDraftIndicators();}else if(t.dataset.field){this.setField(t.dataset.field==='name'?['name']:['execution',t.dataset.field],t.value);}else if(t.matches('[data-import-text]')){this.setImportText(t.value);const b=this.q('[data-action="commit-import"]');if(b)b.disabled=true;}else if(t.matches('[data-import-id]'))this.importNewId=t.value;else if(t.matches('[data-search]')){this.search=t.value;clearTimeout(this.searchTimer);this.searchTimer=setTimeout(()=>void this.safe(()=>this.refreshCatalog()),250);}else if(t.matches('[data-upload-name]'))this.uploadName=t.value;else if(t.matches('[data-instruction]'))this.runInstruction=t.value;else if(t.matches('[data-run-duration]'))this.runDuration=t.value;}
  change(event){const t=event.target;void this.safe(async()=>{if(t.matches('[data-arm]')){this.setField(['bindings','arm'],t.value);this.renderContent();}else if(t.dataset.camera){this.setField(['bindings','cameras',t.dataset.camera],t.value);this.renderContent();}else if(t.matches('[data-execution]')){this.editExecution(t.value);this.renderContent();}else if(t.name==='policy-import-mode')this.importMode=t.value;else if(t.matches('[data-profile-file]')){await this.loadImportFile(t.files?.[0]);}else if(t.matches('[data-upload-kind]')){this.uploadKind=t.value;this.uploadFiles=[];this.renderContent();}else if(t.matches('[data-upload-source]')){this.uploadSource=t.value;this.renderContent();}else if(t.matches('[data-model-files]')){this.uploadFiles=Array.from(t.files||[]);this.uploadLabel=this.uploadFiles[0]?.webkitRelativePath?.split('/')[0]||this.uploadFiles[0]?.name||'';if(!this.uploadName)this.uploadName=this.uploadLabel.replace(/\.zip$/i,'').replace(/[^A-Za-z0-9_.-]/g,'_');this.renderContent();}else if(t.matches('[data-run-mode]')){this.runMode=t.value;this.renderContent();}});}
  // Upload transport is provided by PolicyAPI; jobs outlive view navigation.
  async startUpload(){
    if(this.resumeTarget){const id=this.resumeTarget.id;const job=await this.api.resumeUpload(id,this.uploadFiles);if(job.running!==undefined)this.activeTransfers.set(id,job);this.resumeTarget=null;await this.refreshUploads();this.renderContent();this.schedulePoll();return;}
    if(!this.uploadFiles.length||!this.uploadSource)throw Error('Choose a bundle and destination.');
    const config={source_id:this.uploadSource,destination_name:this.uploadName,kind:this.uploadKind,files:[...this.uploadFiles]};
    const onProgress=progress=>{const previous=this.uploads.find(j=>j.id===progress.id);const record={...previous,...progress,source_id:config.source_id,destination_name:config.destination_name,name:config.destination_name};if(previous)Object.assign(previous,record);else this.uploads.unshift(record);this.renderUploadJobs();};
    const job=await this.api.startUpload({...config,onProgress});this.activeTransfers.set(job.id,job);await this.refreshUploads();this.schedulePoll();
  }
  async refreshUploads(){
    const prior=new Map(this.uploads.map(j=>[j.id,j.state]));this.uploads=await this.api.uploads();let changed=false;
    for(const job of this.uploads){
      if(completedJob(job)){this.activeTransfers.delete(job.id);this.trackScan(job);}
      if(['ready','cataloged','complete'].includes(job.state)&&prior.get(job.id)!==job.state)changed=true;
    }
    this.renderUploadJobs();if(changed)await this.refreshCatalog();
  }
  async pauseUpload(id){await this.api.pauseUpload(id);await this.refreshUploads();}
  async resumeUpload(id){
    const handle=this.activeTransfers.get(id)||this.api.transfers?.get(id);
    if(handle?.files?.length){const resumed=await this.api.resumeUpload(id);if(resumed.running!==undefined)this.activeTransfers.set(id,resumed);await this.refreshUploads();this.schedulePoll();return;}
    const record=await this.api.upload(id);if(completedJob(record)||['verifying','publishing','indexing'].includes(record.state)){await this.refreshUploads();return;}
    if(!['folder','archive'].includes(record.kind))throw Error('The server did not return the original bundle kind. Refresh before resuming.');
    this.resumeTarget=record;this.uploadKind=record.kind;this.uploadSource=record.source_id;this.uploadName=record.destination_name;this.uploadFiles=[];this.uploadLabel='';this.view='uploads';this.render();this.show('Reselect the original checkpoint files. Their recorded chunk hashes are checked before transfer resumes.');
  }
  async cancelUpload(id){const choice=await this.dialog('Cancel this upload?','Only this upload’s staging data will be canceled. Existing model files remain unchanged.',[{value:'keep',label:'Keep upload',primary:true},{value:'cancel-upload',label:'Cancel upload'}]);if(choice!=='cancel-upload')return;await this.api.cancelUpload(id);this.activeTransfers.delete(id);await this.refreshUploads();}
}
