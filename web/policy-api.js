/** Same-origin policy-management transport. No ROS or motor ownership lives here.
 *
 * Endpoint names follow the agreed policy-management backend contract v1.
 * Uploads use file-index / byte-offset chunks, a SHA-256 header and durable
 * chunk receipts. Required resume metadata includes the original bundle kind.
 */

export class PolicyAPIError extends Error {
  constructor(message, {code='POLICY_API_ERROR', status=0, details=null}={}) {
    super(message); this.name='PolicyAPIError'; this.code=code;
    this.status=status; this.details=details;
  }
}

const MAX_CHUNK = 128 * 1024;
const MAX_JSON = 256 * 1024;
const terminal = new Set(['complete','ready','cataloged','canceled','cancelled']);
const serverProcessing = new Set(['verifying','publishing','indexing']);
const own = (v,k) => Object.prototype.hasOwnProperty.call(v,k);
const object = v => Boolean(v) && typeof v==='object' && !Array.isArray(v);
const malformed = text => new PolicyAPIError(text,{code:'INVALID_RESPONSE'});
const encode = value => encodeURIComponent(String(value));
const revisionOf = value => value.revision ?? value.etag ?? null;
const errors = value => Array.isArray(value) ? value.map(v=>typeof v==='string'?{message:v}:v) : [];
const list = (value,key) => {
  if(Array.isArray(value))return value;
  if(Array.isArray(value?.[key]))return value[key];
  throw malformed(`The server returned no ${key} list.`);
};

function detail(value) {
  const p=object(value?.profile) && (own(value.profile,'text')||own(value.profile,'raw')) ? value.profile : value;
  if(!object(p)||typeof p.id!=='string'||typeof (p.text??p.raw)!=='string')
    throw malformed('The server did not return a profile ID and raw text.');
  return {...p,text:p.text??p.raw,revision:revisionOf(p)};
}

function model(value) {
  return {...value,
    name:value.name??value.label??value.id,
    category:value.category??value.model_family??null,
    artifact_revision:value.artifact_revision??value.revision??null,
    size_bytes:value.size_bytes??value.size??null,
    state:value.state??value.status??'unknown',
    // Missing eligibility information must not make a checkpoint selectable.
    selectable:value.selectable===true||value.can_select===true,
    camera_inputs:Array.isArray(value.camera_inputs)?value.camera_inputs:[],
    files:Array.isArray(value.files)?value.files:[],
  };
}

function validation(value) {
  const result=object(value?.validation)?value.validation:value;
  if(!object(result))throw malformed('The server returned no validation result.');
  return {...result,valid:result.valid===true,ready:result.ready===true,
    errors:errors(result.errors),warnings:errors(result.warnings)};
}

function profileSummary(value) {
  const execution=object(value.execution)?value.execution:null;
  const reference=object(value.model_reference)?value.model_reference:null;
  const cameraBindings=Array.isArray(value.camera_summary)?value.camera_summary:null;
  const referenceText=reference
    ? [reference.source_id,reference.relative_path].filter(v=>typeof v==='string'&&v).join(' / ')
    : execution?.kind==='remote'
      ? [execution.endpoint,execution.deployment].filter(v=>typeof v==='string'&&v).join(' · ')
      : typeof value.model_reference==='string'?value.model_reference:'';
  return {...value,revision:revisionOf(value),
    execution:execution?.kind??(typeof value.execution==='string'?value.execution:'unknown'),
    execution_config:execution,
    model_reference:referenceText,model_reference_data:reference,
    camera_summary:cameraBindings
      ? cameraBindings.map(v=>typeof v==='string'?v:v?.camera_id).filter(v=>typeof v==='string').join(' + ')
      : typeof value.camera_summary==='string'?value.camera_summary:'',
    camera_bindings:cameraBindings,
  };
}

function jobRecord(value) {
  const j=object(value?.upload)?value.upload:object(value?.job)?value.job:value;
  if(!object(j)||typeof j.id!=='string')throw malformed('The server returned no upload identity.');
  const serverState=j.state??j.status??'unknown';
  return {...j,name:j.name??j.destination_name??j.id,
    state:serverState==='cataloged'?'ready':serverState,server_state:serverState,
    total_bytes:j.total_bytes??null,received_bytes:j.received_bytes??0,
    files:Array.isArray(j.files)?j.files:[]};
}

function safeRelativePath(path) {
  if(typeof path!=='string'||!path||path.startsWith('/')||path.includes('\\')||path.includes('\0'))
    throw new PolicyAPIError('Upload file paths must be relative.',{code:'INVALID_UPLOAD_PATH'});
  const parts=path.split('/');
  if(parts.some(p=>!p||p==='.'||p==='..'))
    throw new PolicyAPIError('Upload file paths cannot contain empty or traversing segments.',{code:'INVALID_UPLOAD_PATH'});
  return path;
}

/** Preserve relative subdirectories while removing only the selected folder root. */
export function uploadManifest(files,kind='folder') {
  if(!['folder','archive'].includes(kind))throw new PolicyAPIError('Unsupported upload kind.',{code:'INVALID_UPLOAD'});
  const input=Array.from(files||[]);
  if(!input.length)throw new PolicyAPIError('Choose a checkpoint bundle first.',{code:'FILES_REQUIRED'});
  if(kind==='archive'&&(input.length!==1||!String(input[0].name).toLowerCase().endsWith('.zip')))
    throw new PolicyAPIError('Choose one ZIP archive.',{code:'INVALID_UPLOAD'});
  const relative=input.map(file=>String(file.webkitRelativePath||file.name||''));
  const folderRoot=kind==='folder'&&input.every(file=>file.webkitRelativePath)
    ? relative[0].split('/')[0] : null;
  if(folderRoot&&relative.some(path=>path.split('/')[0]!==folderRoot||!path.includes('/')))
    throw new PolicyAPIError('Choose one checkpoint folder.',{code:'INVALID_UPLOAD'});
  const result=input.map((file,i)=>{
    if(!Number.isSafeInteger(file.size)||file.size<0||typeof file.slice!=='function')
      throw new PolicyAPIError('Invalid selected file.',{code:'INVALID_UPLOAD'});
    const path=safeRelativePath(folderRoot?relative[i].slice(folderRoot.length+1):relative[i]);
    return {path,size:file.size,file};
  }).sort((a,b)=>a.path.localeCompare(b.path));
  if(new Set(result.map(f=>f.path)).size!==result.length)
    throw new PolicyAPIError('The bundle contains duplicate relative paths.',{code:'INVALID_UPLOAD'});
  return result;
}

export class PolicyAPI {
  constructor({fetch=(...args)=>globalThis.fetch(...args),origin=globalThis.location?.origin||'http://localhost',
    crypto=globalThis.crypto,getHeaders=()=>({}),timeoutMs=15000,uploadTimeoutMs=30000,
    chunkSize=MAX_CHUNK,now=()=>globalThis.performance?.now()??Date.now(),
    setTimeout=(...args)=>globalThis.setTimeout(...args),clearTimeout=id=>globalThis.clearTimeout(id)}={}) {
    this.fetch=fetch;this.origin=new URL(origin).origin;this.crypto=crypto;this.getHeaders=getHeaders;
    this.timeoutMs=timeoutMs;this.uploadTimeoutMs=uploadTimeoutMs;
    this.chunkSize=this._chunkSize(chunkSize);this.now=now;this.setTimeout=setTimeout;this.clearTimeout=clearTimeout;
    this.transfers=new Map();this.profileRevisions=new Map();
  }

  _chunkSize(value) {
    if(!Number.isSafeInteger(value)||value<1||value>MAX_CHUNK)
      throw new PolicyAPIError(`Upload chunks must be between 1 and ${MAX_CHUNK} bytes.`,{code:'INVALID_CHUNK_SIZE'});
    return value;
  }

  _url(path) {
    if(typeof path!=='string'||!path.startsWith('/api/v1/')||path.startsWith('//'))
      throw new PolicyAPIError('Policy requests must use the local API.',{code:'INVALID_PATH'});
    const url=new URL(path,this.origin);
    if(url.origin!==this.origin||!url.pathname.startsWith('/api/v1/'))
      throw new PolicyAPIError('Policy requests must remain on the station origin.',{code:'INVALID_PATH'});
    return url.href;
  }

  async request(path,{method='GET',json,body,headers={},signal,timeoutMs=this.timeoutMs}={}) {
    if(json!==undefined&&body!==undefined)throw new TypeError('Provide JSON or binary body, not both');
    const requestHeaders={...this.getHeaders(),Accept:'application/json',...headers};
    if(json!==undefined) {
      body=JSON.stringify(json);
      if(new TextEncoder().encode(body).length>MAX_JSON)
        throw new PolicyAPIError('Profile/manifest request exceeds 256 KB.',{code:'REQUEST_TOO_LARGE'});
      requestHeaders['Content-Type']='application/json';
    }
    const controller=new AbortController();let timedOut=false;
    const cancel=()=>controller.abort(signal?.reason);
    if(signal?.aborted)cancel();else signal?.addEventListener('abort',cancel,{once:true});
    const timer=this.setTimeout(()=>{timedOut=true;controller.abort();},timeoutMs);
    try {
      const response=await this.fetch(this._url(path),{method,headers:requestHeaders,body,
        signal:controller.signal,credentials:'same-origin',cache:'no-store',redirect:'error'});
      const raw=await response.text();let value=null;
      if(raw) {try {value=JSON.parse(raw);}catch {if(response.ok)throw malformed('The server returned invalid JSON.');}}
      if(!response.ok) {
        const failure=object(value?.error)?value.error:value;
        throw new PolicyAPIError(String(failure?.message??failure?.detail??(typeof value?.error==='string'?value.error:null)??raw??`HTTP ${response.status}`).slice(0,2048)||`HTTP ${response.status}`,
          {status:response.status,code:failure?.code??(response.status===409?'REVISION_CONFLICT':'HTTP_ERROR'),details:failure?.details??value??null});
      }
      if(object(value)&&value.revision===undefined&&value.etag===undefined) {
        const etag=response.headers?.get?.('ETag');if(etag)value={...value,etag};
      }
      return value;
    } catch(error) {
      if(timedOut)throw new PolicyAPIError('Policy request timed out.',{code:'TIMEOUT'});
      if(signal?.aborted)throw new PolicyAPIError('Policy request canceled.',{code:'ABORTED'});
      if(error instanceof PolicyAPIError)throw error;
      throw new PolicyAPIError(error?.message||'Unable to reach the policy service.',{code:error?.name==='AbortError'?'ABORTED':'NETWORK_ERROR'});
    } finally {
      this.clearTimeout(timer);signal?.removeEventListener('abort',cancel);
    }
  }

  async sources(){const v=await this.request('/api/v1/policy-sources');return {...v,roots:list(v,'roots').map(r=>({...r,writable:r.writable===true}))};}
  reloadSources(){return this.request('/api/v1/policy-sources/reload',{method:'POST',json:{}});}
  async models(search=''){const v=await this.request('/api/v1/policies'+(search?'?q='+encode(search):''));return list(v,'models').map(model);}
  scan(){return this.request('/api/v1/policy-scans',{method:'POST',json:{}});}
  scanStatus(id){return this.request(`/api/v1/policy-scans/${encode(id)}`);}
  async profiles(){const v=await this.request('/api/v1/policy-profiles');const result=list(v,'profiles').map(profileSummary);for(const p of result)this.profileRevisions.set(p.id,p.revision);return result;}
  async profile(id){const p=detail(await this.request(`/api/v1/policy-profiles/${encode(id)}`));this.profileRevisions.set(p.id,p.revision);return p;}
  async saveProfile(id,text,revision=null){const p=detail(await this.request(revision==null?'/api/v1/policy-profiles':`/api/v1/policy-profiles/${encode(id)}`,
    {method:revision==null?'POST':'PUT',json:revision==null?{text}:{text,expected_revision:revision}}));this.profileRevisions.set(p.id,p.revision);return p;}
  refreshProfiles(){return this.request('/api/v1/policy-profiles/refresh',{method:'POST',json:{}});}
  async validate(text){return validation(await this.request('/api/v1/policy-profiles/validate',{method:'POST',json:{text}}));}
  async inspectDraft(text){const v=await this.request('/api/v1/policy-profiles/inspect-draft',{method:'POST',json:{text}});return {...validation(v),descriptor:v.descriptor??null,admitted:v.admitted,job:v.job??null};}
  preparationStatus(id){return this.request(`/api/v1/policy-preparations/${encode(id)}`);}
  async importPreview(text){const v=await this.request('/api/v1/policy-profiles/import-preview',{method:'POST',json:{text}});return {...v,valid:v?.valid===true,errors:errors(v?.errors),warnings:errors(v?.warnings)};}
  async importProfile({text,id,mode='new',revision=null}){const p=detail(await this.request('/api/v1/policy-profiles/import',{method:'POST',json:{text,id,mode,expected_revision:revision}}));this.profileRevisions.set(p.id,p.revision);return p;}
  prepare(id,{expected_revision=this.profileRevisions.get(id)??null,mode='idle'}={}){
    if(expected_revision==null)throw new PolicyAPIError('Refresh this profile before applying it; a saved revision is required.',{code:'REVISION_REQUIRED'});
    if(!['idle','after_current'].includes(mode))throw new PolicyAPIError('Unsupported profile preparation mode.',{code:'INVALID_INPUT'});
    return this.request(`/api/v1/policy-profiles/${encode(id)}/prepare`,{method:'POST',json:{expected_revision,mode}});
  }
  async runtime(){const v=await this.request('/api/v1/policy-runtime');if(!object(v))throw malformed('The server returned no policy-runtime state.');return {...v,applied:v.applied??null,pending:v.pending??null,descriptors:Array.isArray(v.descriptors)?v.descriptors:[]};}

  async upload(id,{signal}={}){return jobRecord(await this.request(`/api/v1/model-uploads/${encode(id)}`,{signal}));}
  async uploads() {
    const v=await this.request('/api/v1/model-uploads');
    return list(v,'uploads').map(value=>{
      const record=jobRecord(value),local=this.transfers.get(record.id);
      if(!local||terminal.has(record.state)||serverProcessing.has(record.state))return record;
      if(local.phase==='paused'||local.phase==='failed'||local.phase==='canceling')
        return {...record,state:local.phase,reason:local.reason||record.reason};
      return record;
    });
  }

  async startUpload({source_id,destination_name,kind='folder',files,onProgress=()=>{}}) {
    if(typeof source_id!=='string'||!source_id)
      throw new PolicyAPIError('Select an upload destination.',{code:'INVALID_UPLOAD'});
    if(typeof destination_name!=='string'||! /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(destination_name))
      throw new PolicyAPIError('Use a simple new model-directory name, without slashes.',{code:'INVALID_UPLOAD'});
    const local=uploadManifest(files,kind);
    const created=jobRecord(await this.request('/api/v1/model-uploads',{method:'POST',json:{source_id,destination_name,kind,files:local.map(({path,size})=>({path,size}))}}));
    const handle=this._handle(created,{local,kind,source_id,destination_name,onProgress});
    this.transfers.set(handle.id,handle);this._launch(handle);return handle;
  }

  _handle(record,{local,kind=record.kind,source_id=record.source_id,destination_name=record.destination_name,onProgress=()=>{}}={}) {
    if(!['folder','archive'].includes(kind))throw malformed('Upload status must declare its bundle kind before it can be resumed.');
    return {id:record.id,kind,source_id,destination_name,files:local||[],
      chunkSize:this._chunkSize(record.chunk_size??this.chunkSize),running:false,
      phase:record.state,reason:'',received_bytes:record.received_bytes,total_bytes:record.total_bytes,
      controller:null,generation:0,completion:null,error:null,onProgress,lastNotify:-Infinity};
  }

  _notify(handle,force=false) {
    const now=this.now();if(!force&&now-handle.lastNotify<250)return;handle.lastNotify=now;
    try {Promise.resolve(handle.onProgress({id:handle.id,state:handle.phase,running:handle.running,
      received_bytes:handle.received_bytes,total_bytes:handle.total_bytes,reason:handle.reason})).catch(()=>{});}catch{}
  }

  _active(handle,generation) {
    if(generation!==handle.generation||handle.controller?.signal.aborted)
      throw new PolicyAPIError('Upload paused or canceled.',{code:'ABORTED'});
  }

  _launch(handle) {
    handle.controller=new AbortController();const generation=++handle.generation;
    handle.running=true;handle.phase='uploading';handle.reason='';handle.error=null;this._notify(handle,true);
    const run=this._transfer(handle,generation).catch(error=>{
      if(generation===handle.generation){
        const retryable=['NETWORK_ERROR','TIMEOUT','RESUME_CONTENT_MISMATCH','UPLOAD_PAUSED','SOURCE_UNAVAILABLE','DESTINATION_UNAVAILABLE'].includes(error.code)||error.status>=500;
        handle.phase=retryable?'paused':'failed';handle.reason=error.message;handle.error=error;
      }
      throw error;
    }).finally(()=>{if(generation===handle.generation){handle.running=false;this._notify(handle,true);}});
    // Callers may observe completion, but a fire-and-monitor UI never leaves an
    // unhandled rejection. Failures remain visible in uploads() and the handle.
    handle.completion=run;run.catch(()=>{});
  }

  async _readChunk(file,offset,size) {
    if(size>MAX_CHUNK)throw new PolicyAPIError('Chunk exceeds the browser memory bound.',{code:'INVALID_CHUNK_SIZE'});
    const buffer=await file.slice(offset,offset+size).arrayBuffer();
    if(buffer.byteLength!==size)throw new PolicyAPIError('Selected file changed or could not be read completely.',{code:'FILE_CHANGED'});
    return buffer;
  }

  async _sha256(buffer) {
    if(!this.crypto?.subtle)throw new PolicyAPIError('This browser needs a secure context for upload integrity checks.',{code:'CRYPTO_UNAVAILABLE'});
    return [...new Uint8Array(await this.crypto.subtle.digest('SHA-256',buffer))].map(v=>v.toString(16).padStart(2,'0')).join('');
  }

  _matchManifest(handle,record) {
    if(record.kind&&record.kind!==handle.kind)throw new PolicyAPIError('The selected bundle kind does not match this upload.',{code:'RESUME_MANIFEST_MISMATCH'});
    if(record.source_id&&record.source_id!==handle.source_id || record.destination_name&&record.destination_name!==handle.destination_name)
      throw new PolicyAPIError('Upload destination identity changed.',{code:'RESUME_MANIFEST_MISMATCH'});
    if(record.files.length!==handle.files.length)
      throw new PolicyAPIError('Reselect the original bundle with exactly the same file inventory.',{code:'RESUME_MANIFEST_MISMATCH'});
    const selected=new Map(handle.files.map(file=>[file.path,file]));const seen=new Set();
    return record.files.map((remote,index)=>{
      safeRelativePath(remote.path);const local=selected.get(remote.path);
      if(seen.has(remote.path)||!local||remote.size!==local.size)
        throw new PolicyAPIError('The selected files do not match this upload’s paths and sizes.',{code:'RESUME_MANIFEST_MISMATCH'});
      seen.add(remote.path);return {remote,index,local};
    });
  }

  _receipts(file,chunkSize) {
    if(file.chunks===undefined) {
      if(file.received_bytes>0)throw malformed('Upload receipts are missing; reselected-file resume cannot be verified.');
      return new Map();
    }
    if(!Array.isArray(file.chunks))throw malformed('Upload chunk receipts must be an array.');
    const receipts=new Map();
    for(const chunk of file.chunks) {
      const offset=chunk.offset,size=chunk.size;
      const sha256=typeof chunk.sha256==='string'?chunk.sha256.replace(/^sha256:/,'').toLowerCase():'';
      if(!Number.isSafeInteger(offset)||offset<0||offset%chunkSize||!Number.isSafeInteger(size)||size<=0||size>MAX_CHUNK||size!==Math.min(chunkSize,file.size-offset)||!/^[0-9a-f]{64}$/.test(sha256)||receipts.has(offset))
        throw malformed('The server returned an invalid upload chunk receipt.');
      receipts.set(offset,{offset,size,sha256});
    }
    return receipts;
  }

  async _transfer(handle,generation) {
    const signal=handle.controller.signal;
    const record=await this.upload(handle.id,{signal});this._active(handle,generation);
    if(terminal.has(record.state)||serverProcessing.has(record.state)){handle.phase=record.state;return record;}
    if(record.chunk_size!==undefined)handle.chunkSize=this._chunkSize(record.chunk_size);
    const entries=this._matchManifest(handle,record);
    let verifiedBytes=0;
    // Verify ALL acknowledged chunks before sending any new bytes. Filenames,
    // sizes and timestamps alone do not prove the user reselected the same data.
    for(const entry of entries) {
      entry.receipts=this._receipts(entry.remote,handle.chunkSize);
      for(const receipt of entry.receipts.values()) {
        this._active(handle,generation);
        const data=await this._readChunk(entry.local.file,receipt.offset,receipt.size);
        const hash=await this._sha256(data);this._active(handle,generation);
        if(hash!==receipt.sha256)throw new PolicyAPIError(`Reselected file differs from the uploaded data: ${entry.local.path}`,{code:'RESUME_CONTENT_MISMATCH'});
        verifiedBytes+=receipt.size;
      }
    }
    if(record.received_bytes>verifiedBytes)
      throw malformed('Upload status reports bytes without verifiable chunk receipts.');
    handle.received_bytes=verifiedBytes;handle.total_bytes=handle.files.reduce((n,f)=>n+f.size,0);this._notify(handle,true);
    for(const entry of entries) {
      for(let offset=0;offset<entry.local.size;offset+=handle.chunkSize) {
        this._active(handle,generation);if(entry.receipts.has(offset))continue;
        const size=Math.min(handle.chunkSize,entry.local.size-offset);
        const data=await this._readChunk(entry.local.file,offset,size);
        const hash=await this._sha256(data);this._active(handle,generation);
        await this.request(`/api/v1/model-uploads/${encode(handle.id)}/files/${entry.index}/chunks/${offset}`,
          {method:'PUT',body:data,headers:{'Content-Type':'application/octet-stream','X-Chunk-SHA256':hash},signal,timeoutMs:this.uploadTimeoutMs});
        this._active(handle,generation);handle.received_bytes+=size;this._notify(handle);
      }
    }
    this._active(handle,generation);handle.phase='verifying';this._notify(handle,true);
    const finalized=jobRecord(await this.request(`/api/v1/model-uploads/${encode(handle.id)}/finalize`,{method:'POST',json:{},signal,timeoutMs:this.uploadTimeoutMs}));
    this._active(handle,generation);handle.phase=finalized.state;handle.received_bytes=finalized.received_bytes;return finalized;
  }

  async pauseUpload(id) {
    const handle=this.transfers.get(id);if(!handle)return this.upload(id);
    if(!handle.running)return handle;
    handle.generation++;handle.controller?.abort();handle.running=false;handle.phase='paused';
    handle.reason='Paused in this browser. Staged chunks remain on the server.';this._notify(handle,true);return handle;
  }

  async resumeUpload(id,files=[]) {
    let handle=this.transfers.get(id);
    if(handle?.running)throw new PolicyAPIError('This upload is already running.',{code:'UPLOAD_RUNNING'});
    // Drain a preceding aborted sender; its generation cannot finalize this run.
    if(handle?.completion)await handle.completion.catch(()=>{});
    const record=await this.upload(id);
    if(terminal.has(record.state)||serverProcessing.has(record.state))return record;
    const kind=handle?.kind??record.kind;
    const local=files?.length?uploadManifest(files,kind):handle?.files;
    if(!local?.length)throw new PolicyAPIError('Reselect the original checkpoint files before resuming.',{code:'FILES_REQUIRED'});
    if(!handle){handle=this._handle(record,{local,kind});this.transfers.set(id,handle);}
    else handle.files=local;
    this._matchManifest(handle,record);this._launch(handle);return handle;
  }

  async cancelUpload(id) {
    const handle=this.transfers.get(id);
    if(handle){handle.generation++;handle.controller?.abort();handle.running=false;handle.phase='canceling';this._notify(handle,true);}
    try {
      const record=jobRecord(await this.request(`/api/v1/model-uploads/${encode(id)}/cancel`,{method:'POST',json:{}}));
      if(handle){handle.phase=record.state;handle.reason=record.reason||'';this._notify(handle,true);}return record;
    } catch(error) {
      if(handle){handle.phase='paused';handle.reason='Cancel was not confirmed: '+error.message;this._notify(handle,true);}throw error;
    }
  }
}
