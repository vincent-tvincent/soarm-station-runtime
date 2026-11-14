import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto, createHash } from 'node:crypto';
import { PolicyAPI, PolicyAPIError, uploadManifest } from '../web/policy-api.js';

const response=(body,status=200,headers={})=>new Response(body===null?'':JSON.stringify(body),{status,headers:{'Content-Type':'application/json',...headers}});
const digest=data=>createHash('sha256').update(data).digest('hex');
const copy=v=>JSON.parse(JSON.stringify(v));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function file(name,data,path='') {
  const buffer=Buffer.from(data),reads=[];
  return {name,size:buffer.length,webkitRelativePath:path,reads,
    arrayBuffer(){throw Error('Whole-file buffering is forbidden');},
    slice(start,end){
      reads.push({start,end});const view=buffer.subarray(start,end);
      return {async arrayBuffer(){return view.buffer.slice(view.byteOffset,view.byteOffset+view.byteLength);}};
    }};
}

class UploadServer {
  constructor({chunkSize=4,gateFirst=false,seed=null}={}){
    this.chunkSize=chunkSize;this.job=seed?copy(seed):null;this.calls=[];this.puts=[];
    this.gateFirst=gateFirst;this.firstPut=deferred();this.finalizes=0;
    this.fetch=this.fetch.bind(this);
  }
  async fetch(url,options={}) {
    const path=new URL(url).pathname,method=options.method||'GET';this.calls.push({path,method,options});
    assert.equal(new URL(url).origin,'https://station.test');
    assert.equal(options.credentials,'same-origin');assert.equal(options.redirect,'error');
    if(path==='/api/v1/model-uploads'&&method==='POST') {
      const value=JSON.parse(options.body);
      this.job={...value,id:'upload_1',state:'uploading',chunk_size:this.chunkSize,
        total_bytes:value.files.reduce((n,f)=>n+f.size,0),received_bytes:0,
        files:value.files.map(f=>({...f,chunks:[]}))};
      return response(copy(this.job));
    }
    if(path==='/api/v1/model-uploads'&&method==='GET')return response({uploads:this.job?[copy(this.job)]:[]});
    if(path==='/api/v1/model-uploads/upload_1'&&method==='GET')return response(copy(this.job));
    const match=path.match(/\/files\/(\d+)\/chunks\/(\d+)$/);
    if(match&&method==='PUT') {
      const index=Number(match[1]),offset=Number(match[2]),bytes=Buffer.from(options.body);
      assert.equal(options.headers['Content-Type'],'application/octet-stream');
      assert.equal(options.headers['X-Chunk-SHA256'],digest(bytes));
      const f=this.job.files[index],old=f.chunks.find(c=>c.offset===offset);
      assert.ok(!old||old.sha256===digest(bytes));
      if(!old){f.chunks.push({offset,size:bytes.length,sha256:digest(bytes)});this.job.received_bytes+=bytes.length;}
      this.puts.push({index,offset,bytes});
      if(this.gateFirst&&this.puts.length===1) {
        this.firstPut.resolve();
        return new Promise((resolve,reject)=>{
          const abort=()=>reject(new DOMException('Aborted','AbortError'));
          if(options.signal.aborted)abort();else options.signal.addEventListener('abort',abort,{once:true});
        });
      }
      return response({accepted:true});
    }
    if(path.endsWith('/finalize')){this.finalizes++;this.job.state='complete';return response(copy(this.job));}
    if(path.endsWith('/cancel')){this.job.state='canceled';return response(copy(this.job));}
    throw Error(`Unexpected ${method} ${path}`);
  }
}
const apiFor=(server,options={})=>new PolicyAPI({origin:'https://station.test',fetch:server.fetch,crypto:webcrypto,...options});

test('uses same-origin methods, preserves revision conflicts and normalizes ETag',async()=>{
  const calls=[];
  const api=new PolicyAPI({origin:'https://station.test',fetch:async(url,options)=>{
    calls.push({url,options});
    if(options.method==='PUT')return response({code:'REVISION_CONFLICT',message:'Profile changed',details:{revision:'v2'}},409);
    return response({id:'cup',text:'id: cup\n'},200,{ETag:'"v1"'});
  }});
  const created=await api.saveProfile('cup','id: cup\n',null);
  assert.equal(created.revision,'"v1"');assert.equal(calls[0].options.method,'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body),{text:'id: cup\n'});
  await assert.rejects(api.saveProfile('cup','id: cup\nname: New','"v1"'),error=>{
    assert.ok(error instanceof PolicyAPIError);assert.equal(error.status,409);
    assert.equal(error.code,'REVISION_CONFLICT');assert.equal(error.details.revision,'v2');return true;
  });
  assert.equal(calls[1].options.method,'PUT');assert.equal(JSON.parse(calls[1].options.body).expected_revision,'"v1"');
  assert.equal(calls[1].options.credentials,'same-origin');
});

test('rejects external URLs and oversized JSON before fetch',async()=>{
  let calls=0;const api=new PolicyAPI({origin:'https://station.test',fetch:async()=>{calls++;return response({});}});
  await assert.rejects(api.request('https://evil.test/api/v1/profiles'),e=>e.code==='INVALID_PATH');
  await assert.rejects(api.request('//evil.test/api/v1/profiles'),e=>e.code==='INVALID_PATH');
  await assert.rejects(api.request('/api/v1/../other'),e=>e.code==='INVALID_PATH');
  await assert.rejects(api.validate('a'.repeat(262144)),e=>e.code==='REQUEST_TOO_LARGE');
  assert.equal(calls,0);
});

test('does not infer writable sources or selectable models from missing fields',async()=>{
  const api=new PolicyAPI({origin:'https://station.test',fetch:async url=>new URL(url).pathname.endsWith('policy-sources')
    ?response({roots:[{id:'ssd',allow_upload:true}]})
    :response({models:[{id:'a',status:'Ready'},{id:'b',selectable:true}]})});
  assert.equal((await api.sources()).roots[0].writable,false);
  const models=await api.models('cup & arm');assert.equal(models[0].selectable,false);assert.equal(models[1].selectable,true);
});

test('prepare pins the last read revision and preserves explicit switch mode',async()=>{
  let body;
  const api=new PolicyAPI({origin:'https://station.test',fetch:async(url,options)=>{
    if(new URL(url).pathname.endsWith('/prepare')){body=JSON.parse(options.body);return response({state:'preparing'});}
    return response({profiles:[{id:'cup',revision:'sha256:a'}]});
  }});
  await api.profiles();await api.prepare('cup',{mode:'after_current'});
  assert.deepEqual(body,{expected_revision:'sha256:a',mode:'after_current'});
  assert.throws(()=>api.prepare('unread'),e=>e.code==='REVISION_REQUIRED');
});

test('normalizes actual ProfileStore summaries without losing structured configuration',async()=>{
  const execution={kind:'local',device:'cpu'},reference={source_id:'ssd',relative_path:'cup/pretrained_model',artifact_revision:'sha256:abc'};
  const bindings=[{key:'observation.images.front',camera_id:'forward'},{key:'observation.images.wrist',camera_id:'wrist'}];
  const api=new PolicyAPI({origin:'https://station.test',fetch:async()=>response({profiles:[
    {id:'cup',name:'Cup policy',revision:'r1',execution,model_reference:reference,camera_summary:bindings},
    {id:'remote',execution:{kind:'remote',endpoint:'tcp/gpu:7447',deployment:'cup-vla'},model_reference:null,camera_summary:[]},
  ]})});
  const [local,remote]=await api.profiles();
  assert.equal(local.execution,'local');assert.equal(local.model_reference,'ssd / cup/pretrained_model');
  assert.equal(local.camera_summary,'forward + wrist');assert.deepEqual(local.execution_config,execution);
  assert.deepEqual(local.model_reference_data,reference);assert.deepEqual(local.camera_bindings,bindings);
  assert.equal(remote.execution,'remote');assert.equal(remote.model_reference,'tcp/gpu:7447 · cup-vla');
  assert.equal(remote.camera_summary,'');
});

test('inspect-draft preserves descriptor and cataloged upload means ready for selection',async()=>{
  const api=new PolicyAPI({origin:'https://station.test',fetch:async url=>new URL(url).pathname.endsWith('inspect-draft')
    ?response({validation:{valid:true,ready:false,errors:[],warnings:[],resolved_model:{category:'pi05'}},descriptor:{deployment:'pi05-tabletop'},admitted:false})
    :response({uploads:[{id:'done',state:'cataloged',kind:'folder',files:[]}]})});
  const inspected=await api.inspectDraft('schema_version: 1');
  assert.equal(inspected.valid,true);assert.equal(inspected.ready,false);assert.equal(inspected.admitted,false);
  assert.equal(inspected.descriptor.deployment,'pi05-tabletop');assert.equal(inspected.resolved_model.category,'pi05');
  const jobs=await api.uploads();assert.equal(jobs[0].state,'ready');assert.equal(jobs[0].server_state,'cataloged');
});

test('remote inspection preserves asynchronous job and exposes preparation polling',async()=>{
  const job={id:'inspect_1',state:'queued'},descriptor={deployment:'pi05-tabletop'};
  const api=new PolicyAPI({origin:'https://station.test',fetch:async url=>new URL(url).pathname.endsWith('inspect-draft')
    ?response({validation:{valid:true,ready:false,errors:[],warnings:[]},descriptor:null,admitted:false,job})
    :response({job:{...job,state:'complete',result:{descriptor,admitted:false}}})});
  const pending=await api.inspectDraft('schema_version: 1');
  assert.deepEqual(pending.job,job);assert.equal(pending.descriptor,null);
  const result=await api.preparationStatus(job.id);
  assert.equal(result.job.state,'complete');assert.deepEqual(result.job.result.descriptor,descriptor);
});

test('malformed lists fail visibly instead of pretending the catalog is empty',async()=>{
  const api=new PolicyAPI({origin:'https://station.test',fetch:async()=>response({unexpected:[]})});
  await assert.rejects(api.models(),e=>e.code==='INVALID_RESPONSE');
  await assert.rejects(api.sources(),e=>e.code==='INVALID_RESPONSE');
});

test('timeouts and caller aborts have separate error codes',async()=>{
  const fetch=async(_url,{signal})=>new Promise((_resolve,reject)=>{
    const abort=()=>reject(new DOMException('Aborted','AbortError'));
    if(signal.aborted)abort();else signal.addEventListener('abort',abort,{once:true});
  });
  const api=new PolicyAPI({origin:'https://station.test',fetch,timeoutMs:8});
  await assert.rejects(api.request('/api/v1/policies'),e=>e.code==='TIMEOUT');
  const controller=new AbortController();controller.abort();
  await assert.rejects(api.request('/api/v1/policies',{signal:controller.signal}),e=>e.code==='ABORTED');
});

test('folder manifest preserves nested paths and rejects duplicate/traversing entries',()=>{
  const files=[file('model.safetensors','data','bundle/model.safetensors'),file('stats.json','{}','bundle/processors/stats.json')];
  assert.deepEqual(uploadManifest(files).map(({path,size})=>({path,size})),[{path:'model.safetensors',size:4},{path:'processors/stats.json',size:2}]);
  assert.throws(()=>uploadManifest([file('a','1','root/../a')]),e=>e.code==='INVALID_UPLOAD_PATH');
  assert.throws(()=>uploadManifest([file('a','1'),file('a','2')]),e=>e.code==='INVALID_UPLOAD');
  assert.throws(()=>uploadManifest([file('weights.pt','data')],'archive'),e=>e.code==='INVALID_UPLOAD');
});

test('streams binary chunks within the memory bound and finalizes only after acknowledgments',async()=>{
  const server=new UploadServer({chunkSize:128*1024}),api=apiFor(server);
  const weights=file('model.safetensors',Buffer.alloc(270000,7),'bundle/model.safetensors');
  const config=file('config.json','{}','bundle/config.json'),empty=file('empty.txt','','bundle/empty.txt');
  const handle=await api.startUpload({source_id:'ssd',destination_name:'cup-v2',files:[weights,config,empty]});
  assert.equal(handle.running,true);await handle.completion;
  assert.equal(handle.running,false);assert.equal(handle.phase,'complete');assert.equal(server.finalizes,1);
  assert.equal(server.job.received_bytes,270002);
  assert.ok(weights.reads.length>=3);assert.ok(weights.reads.every(r=>r.end-r.start<=128*1024));
  assert.ok(server.puts.every(p=>p.bytes.length<=128*1024));
  assert.equal(server.calls.at(-1).path,'/api/v1/model-uploads/upload_1/finalize');
  assert.deepEqual(server.job.files.map(f=>f.path),['config.json','empty.txt','model.safetensors']);
});

test('pause retains committed receipts, then identical reselected files resume without replaying chunks',async()=>{
  const server=new UploadServer({gateFirst:true}),api=apiFor(server);
  const handle=await api.startUpload({source_id:'ssd',destination_name:'cup',files:[file('model.bin','abcdefgh','bundle/model.bin')]});
  await server.firstPut.promise;await api.pauseUpload(handle.id);
  await handle.completion.catch(()=>{});
  assert.equal(handle.running,false);assert.equal(handle.phase,'paused');assert.equal(server.finalizes,0);
  assert.equal((await api.uploads())[0].state,'paused');
  const reselection=file('model.bin','abcdefgh','another-folder/model.bin');
  const resumed=await api.resumeUpload(handle.id,[reselection]);await resumed.completion;
  assert.equal(resumed.phase,'complete');assert.deepEqual(server.puts.map(p=>p.offset),[0,4]);
  assert.ok(reselection.reads.some(r=>r.start===0&&r.end===4),'existing receipt was checked against local content');
});

test('same path and size with different bytes cannot resume and sends no new chunks',async()=>{
  const server=new UploadServer({gateFirst:true}),api=apiFor(server);
  const h=await api.startUpload({source_id:'ssd',destination_name:'cup',files:[file('model.bin','abcdefgh')]});
  await server.firstPut.promise;await api.pauseUpload(h.id);await h.completion.catch(()=>{});
  const resumed=await api.resumeUpload(h.id,[file('model.bin','ABCDzzzz')]);
  await assert.rejects(resumed.completion,e=>e.code==='RESUME_CONTENT_MISMATCH');
  assert.equal(server.puts.length,1);assert.equal(server.finalizes,0);
  const visible=(await api.uploads())[0];assert.match(visible.reason,/differs/);
  assert.equal(visible.state,'paused','operator can reselect the original files and retry');
});

test('network failure retains a resumable paused transfer',async()=>{
  const server=new UploadServer();let fail=true;
  const fetch=async(url,options)=>{
    if(fail&&options.method==='PUT'){fail=false;throw new TypeError('Connection lost');}
    return server.fetch(url,options);
  };
  const api=apiFor(server,{fetch});
  const h=await api.startUpload({source_id:'ssd',destination_name:'cup',files:[file('model.bin','abcdefgh')]});
  await assert.rejects(h.completion,e=>e.code==='NETWORK_ERROR');
  assert.equal((await api.uploads())[0].state,'paused');
  const resumed=await api.resumeUpload(h.id);await resumed.completion;
  assert.equal(resumed.phase,'complete');assert.equal(server.finalizes,1);
});

test('validates every file receipt before sending a missing chunk in an earlier file',async()=>{
  const seed={id:'upload_1',source_id:'ssd',destination_name:'cup',kind:'folder',state:'uploading',chunk_size:4,
    total_bytes:16,received_bytes:8,files:[
      {path:'a.bin',size:8,chunks:[{offset:0,size:4,sha256:digest('aaaa')}]},
      {path:'b.bin',size:8,chunks:[{offset:0,size:4,sha256:digest('bbbb')}]},
    ]};
  const server=new UploadServer({seed}),api=apiFor(server);
  const h=await api.resumeUpload('upload_1',[file('a.bin','aaaa1234'),file('b.bin','XXXX5678')]);
  await assert.rejects(h.completion,e=>e.code==='RESUME_CONTENT_MISMATCH');
  assert.equal(server.puts.length,0);assert.equal(server.finalizes,0);
});

test('resume rejects a changed inventory and unverifiable server receipts',async()=>{
  const seed={id:'upload_1',source_id:'ssd',destination_name:'cup',kind:'folder',state:'uploading',chunk_size:4,
    total_bytes:8,received_bytes:4,files:[{path:'a.bin',size:8,chunks:[{offset:0,size:4,sha256:digest('aaaa')}]}]};
  const server=new UploadServer({seed}),api=apiFor(server);
  await assert.rejects(api.resumeUpload('upload_1',[file('other.bin','aaaa1234')]),e=>e.code==='RESUME_MANIFEST_MISMATCH');
  delete server.job.files[0].chunks;
  const h=await api.resumeUpload('upload_1',[file('a.bin','aaaa1234')]);
  await assert.rejects(h.completion,e=>e.code==='INVALID_RESPONSE');
  assert.equal(server.puts.length,0);
});

test('cancel aborts a sender and server-confirmed terminal state wins over local state',async()=>{
  const server=new UploadServer({gateFirst:true}),api=apiFor(server);
  const h=await api.startUpload({source_id:'ssd',destination_name:'cup',files:[file('model.bin','abcdefgh')]});
  await server.firstPut.promise;await api.cancelUpload(h.id);await h.completion.catch(()=>{});
  assert.equal(h.running,false);assert.equal(server.finalizes,0);assert.equal((await api.uploads())[0].state,'canceled');
  assert.equal(server.puts.length,1);
});

test('resume does not re-finalize a job already being verified on the server',async()=>{
  const seed={id:'upload_1',source_id:'ssd',destination_name:'cup',kind:'folder',state:'verifying',chunk_size:4,
    total_bytes:4,received_bytes:4,files:[{path:'a.bin',size:4,chunks:[{offset:0,size:4,sha256:digest('aaaa')}]}]};
  const server=new UploadServer({seed}),api=apiFor(server);
  const record=await api.resumeUpload('upload_1');
  assert.equal(record.state,'verifying');assert.equal(server.puts.length,0);assert.equal(server.finalizes,0);
  api.transfers.set('upload_1',{phase:'paused',reason:'Lost the finalize response'});
  assert.equal((await api.uploads())[0].state,'verifying','server verification outlives local transport state');
});
