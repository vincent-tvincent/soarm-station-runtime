"""Native policy preparation/registry; no permission grant or motor dispatch."""
import copy
import gc
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .policy_common import PolicyError,atomic_write,revision
from .policy_bindings import compile_profile


def execution_key(profile):
    # Match the existing supervisor fingerprint, including JSON formatting.
    return hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()


class PolicyRuntime:
    def __init__(self,runtime,loader=None,inspector=None):
        self.runtime=runtime;self.loader=loader;self.inspector=inspector
        self.lock=threading.RLock();self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='policy-management')
        self.jobs={};self.requests={};self.applied={};self.pending={};self.registered={};self.descriptors={};self.recent_runs=[]
        self.stop=threading.Event();self.thread=None;self.closed=False

    def _compact(self,profile):
        result=copy.deepcopy(profile);artifact=result.get('_artifact')
        if artifact and artifact.get('files'):
            root=Path(self.runtime.config.get('policy_management',{}).get('cache_db',
                Path(self.runtime.config.get('_path','.')).parent/'policy-management.sqlite3')).parent/'policy-artifact-pins'
            pin=root/(revision(artifact).removeprefix('sha256:')+'.json')
            data=json.dumps(artifact,sort_keys=True,allow_nan=False).encode()
            if not pin.exists():atomic_write(pin,data)
            elif pin.read_bytes()!=data:raise PolicyError('ARTIFACT_PIN_CONFLICT','Native artifact pin differs',409)
            result['_artifact']={key:value for key,value in artifact.items() if key!='files'}
            result['_artifact']['inventory_path']=str(pin.resolve())
        return result

    def start(self):
        self.thread=threading.Thread(target=self._watch,name='policy-boundary-watcher',daemon=True);self.thread.start()

    def _new(self,kind,request):
        identifier='policy-'+uuid.uuid4().hex
        job={'id':identifier,'kind':kind,'state':'queued','phase':'queued','progress':{'done':0,'total':1},
            'errors':[],'started_at_ns':None,'finished_at_ns':None}
        with self.lock:
            self.jobs[identifier]=job;self.requests[identifier]=copy.deepcopy(request)
            # Retain bounded historical jobs; active ones are never silently evicted.
            completed=[key for key,value in self.jobs.items() if value['state'] in ('complete','failed')]
            for key in completed[:-96]:self.jobs.pop(key,None);self.requests.pop(key,None)
        return identifier

    def _connection(self,execution):
        name=execution.get('connection_profile')
        if not name:return None
        value=self.runtime.config.get('policy_management',{}).get('connections',{}).get(name)
        if not isinstance(value,dict):raise PolicyError('CONNECTION_UNAVAILABLE','Startup connection profile is not configured',503)
        return value

    def inspect(self,execution,settings=None):
        identifier=self._new('inspect',{'execution':execution,'settings':settings or {}})
        self.executor.submit(self._inspect,identifier)
        return self.job(identifier)

    def _inspect(self,identifier):
        request=self.requests[identifier]
        try:
            self._phase(identifier,'inspecting')
            inspector=self.inspector
            if inspector is None:
                from .policy_native import inspect_remote
                inspector=inspect_remote
            result=inspector(request['execution'],request['settings'],self._connection(request['execution']))
            if result.get('admitted'):raise PolicyError('INSPECTION_ADMITTED','Inspection must not admit a session',503)
            with self.lock:self.descriptors[execution_key(request['execution'])]={'descriptor':copy.deepcopy(result['descriptor']),'checked_at_ns':time.time_ns()}
            self._complete(identifier,result)
        except Exception as exc:self._fail(identifier,exc)

    def prepare(self,document,profile_revision,runtime_profile=None,mode='idle',source_revision=None):
        if mode not in ('idle','after_current'):raise PolicyError('UNSUPPORTED_PREPARE_MODE','Only idle/after_current preparation is supported')
        arm_id=document['bindings']['arm']
        if arm_id not in self.runtime.arms:raise PolicyError('ARM_UNRESOLVED','Unknown arm resource',404)
        arm=self.runtime.arms[arm_id]
        with self.lock:
            if arm_id in self.pending:raise PolicyError('PREPARATION_BUSY','A preparation already exists for this arm',409)
            with arm.lock:
                busy=arm.op is not None or arm.hold_pending
                if busy and mode!='after_current':raise PolicyError('ARM_BUSY','Choose after_current to apply at operation completion',409)
                identifier=self._new('prepare',{'document':document,'profile_revision':profile_revision,
                    'runtime_profile':runtime_profile,'mode':mode,'arm_id':arm_id,'source_revision':source_revision})
                self.pending[arm_id]={'job_id':identifier,'id':document['id'],'profile_revision':profile_revision,
                    'state':'after_current' if busy else 'preparing','arm_id':arm_id}
                if not busy:
                    arm.policy_preparation=identifier;self.executor.submit(self._prepare,identifier)
            return self.job(identifier)

    def _watch(self):
        while not self.stop.wait(.1):
            with self.lock:
                for arm_id,pending in list(self.pending.items()):
                    if pending['state']!='after_current':continue
                    arm=self.runtime.arms[arm_id]
                    with arm.lock:
                        if arm.op is None and not arm.hold_pending:
                            pending['state']='preparing';arm.policy_preparation=pending['job_id'];self.executor.submit(self._prepare,pending['job_id'])

    def _phase(self,identifier,phase):
        with self.lock:
            job=self.jobs[identifier];job.update(state='running',phase=phase)
            if job['started_at_ns'] is None:job['started_at_ns']=time.time_ns()

    def _prepare(self,identifier):
        request=self.requests[identifier];document=request['document'];arm=self.runtime.arms[request['arm_id']]
        prepared=None;attached=False;reused=False
        try:
            self._phase(identifier,'validating')
            profile=copy.deepcopy(request.get('runtime_profile'))
            descriptor=None
            if document['execution']['kind']=='remote':
                inspector=self.inspector
                if inspector is None:
                    from .policy_native import inspect_remote
                    inspector=inspect_remote
                inspection=inspector(document['execution'],document.get('settings',{}),self._connection(document['execution']))
                if inspection.get('admitted'):raise PolicyError('INSPECTION_ADMITTED','Preparation inspection cannot take over a remote session',503)
                descriptor=inspection['descriptor'];validation,profile=compile_profile(document,None,self.runtime.config,descriptor)
                if not validation['ready']:raise PolicyError('PROFILE_NOT_READY','Remote contract is not ready',409,validation)
                connection=self._connection(document['execution']) or {}
                for key in ('zenoh_config_path','zenoh_mode'):
                    if key in connection:profile['remote'][key]=connection[key]
            if not isinstance(profile,dict):raise PolicyError('PROFILE_NOT_READY','No resolved execution profile was provided',409)
            self._check_inputs(arm,profile)
            if profile['backend']=='local':
                self._phase(identifier,'verifying_artifact')
                if self.loader is None:
                    from .policy_native import verify_artifact
                    verify_artifact(profile['_artifact'])
                effective=self._compact(profile)
                with arm.lock:
                    from .model_runtime import ModelSupervisor
                    if arm.model is None:arm.model=ModelSupervisor(arm)
                    supervisor=arm.model;old_job=supervisor.job
                if old_job is not None:
                    self._phase(identifier,'draining_previous_work')
                    try:old_job.result(timeout=profile.get('startup_timeout_s',120))
                    except TimeoutError:raise PolicyError('NATIVE_WORK_DRAINING','Previous native work is still draining',409)
                    except Exception:pass # Old errors are retired, not faults of the new profile.
                with arm.lock:
                    if arm.op is not None and arm.op.get('operation_type')=='MODEL':raise PolicyError('ARM_BUSY','A MODEL operation became active',409)
                    key=execution_key(effective)
                    reused=supervisor.session is not None and supervisor.profile_key==key and not getattr(supervisor.session,'terminally_failed',False)
                    old=None if reused else supervisor.session
                    if not reused:supervisor.session=None;supervisor.profile_key=None
                    supervisor.job=None
                if reused:prepared=supervisor.session
                else:
                    if old is not None:
                        self._phase(identifier,'cold_switch_unloading');old.close();old=None;gc.collect()
                    self._check_memory(profile)
                    self._phase(identifier,'loading')
                    loader=self.loader
                    if loader is None:
                        from .policy_native import load_managed_local
                        loader=load_managed_local
                    prepared=loader(profile)
                    if self.closed:raise PolicyError('RUNTIME_STOPPED','Native runtime stopped during preparation',503)
                    with arm.lock:supervisor.install_prepared(effective,prepared);attached=True
                profile=effective
            metadata={'id':document['id'],'name':document['name'],'profile_revision':request['profile_revision'],
                'state':'ready','execution':copy.deepcopy(document['execution']),'arm_id':arm.id,
                'artifact_revision':document.get('model',{}).get('artifact_revision') or document['execution'].get('expected_artifact'),
                'language_conditioned':profile['language_conditioned'],
                'reason':'Weights/processors loaded; preparation performs no inference or physical-task validation' if profile['backend']=='local' else None,
                'runtime_profile_id':'pm_'+execution_key({'id':document['id'],'revision':request['profile_revision'],'profile':profile})[:48],
                'source_revision':request.get('source_revision'),'model_reference':copy.deepcopy(document.get('model'))}
            if descriptor:
                metadata.update(remote_instance=descriptor.get('instance_id'),session_admitted=False,
                    reason='Descriptor verified; the native session is admitted only on Start')
            with self.lock:
                if self.closed:raise PolicyError('RUNTIME_STOPPED','Native runtime stopped',503)
                key=metadata['runtime_profile_id'];self.runtime.config['profiles'][key]=copy.deepcopy(profile)
                self.registered[key]={'profile':copy.deepcopy(profile),'identity':copy.deepcopy(metadata)}
                self.applied[arm.id]=metadata
                with arm.lock:arm.policy_preparation=None
                self.pending.pop(arm.id,None)
                protected={value['runtime_profile_id'] for value in self.applied.values()}
                for item in self.runtime.arms.values():
                    with item.lock:
                        if item.op and item.op.get('operation_type')=='MODEL':protected.add(item.op['profile_id'])
                obsolete=[key for key in self.registered if key not in protected]
                for key in obsolete[:-128]:self.registered.pop(key,None);self.runtime.config['profiles'].pop(key,None)
            details=copy.deepcopy(getattr(prepared,'preparation_details',{})) if prepared else {'descriptor_verified':True,'session_admitted':False}
            self._complete(identifier,{'applied':metadata,'preparation':details,'descriptor':descriptor})
        except Exception as exc:
            if prepared is not None and not reused and not attached:
                try:prepared.close()
                except Exception:pass
            with self.lock:
                with arm.lock:
                    if getattr(arm,'policy_preparation',None)==identifier:arm.policy_preparation=None
                self.pending.pop(arm.id,None)
                if arm.id in self.applied and arm.model is not None and arm.model.session is None:
                    self.applied[arm.id]={**self.applied[arm.id],'state':'failed','reason':'Previous idle model was released; preparation failed'}
            self._fail(identifier,exc)

    def _check_inputs(self,arm,profile):
        with arm.lock:
            if not arm.q:raise PolicyError('FEEDBACK_UNAVAILABLE','Measured joint feedback is not available',409)
        for name in profile.get('cameras',{}).values():
            camera=self.runtime.cameras.get(name)
            snapshot=camera.snapshot() if camera is not None else {}
            if snapshot.get('error') or snapshot.get('age_ms',float('inf'))>profile.get('max_image_age_ms',250):
                raise PolicyError('CAMERA_UNAVAILABLE','A required bound camera is unavailable or stale',409,{'camera_id':name})

    def _check_memory(self,profile):
        if self.loader is not None:return
        import os
        artifact=profile.get('_artifact',{});files=artifact.get('files')
        if files is None and artifact.get('inventory_path'):
            # The pin was already verified; inspect only its bounded metadata here.
            path=Path(artifact['inventory_path'])
            if path.stat().st_size>32*1024*1024:raise PolicyError('ARTIFACT_PIN_LIMIT','Artifact pin is too large',413)
            files=json.loads(path.read_bytes()).get('files',[])
        size=sum(item.get('size',0) for item in files or [])
        try:available=os.sysconf('SC_AVPHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')
        except (ValueError,OSError):return
        reserve=int(self.runtime.config.get('policy_management',{}).get('memory_reserve_bytes',256*1024*1024))
        if size*2+reserve>available:raise PolicyError('MEMORY_BUDGET','Insufficient available memory for local preparation',409,{'available_bytes':available,'estimated_bytes':size*2+reserve})

    def _complete(self,identifier,result):
        with self.lock:
            self.jobs[identifier].update(state='complete',phase='complete',finished_at_ns=time.time_ns(),progress={'done':1,'total':1},result=copy.deepcopy(result))
            self.requests.pop(identifier,None)

    def _fail(self,identifier,error):
        record=error.response() if isinstance(error,PolicyError) else {'code':'PREPARATION_FAILED','message':str(error)[:512]}
        with self.lock:
            self.jobs[identifier].update(state='failed',phase='failed',finished_at_ns=time.time_ns(),errors=[record])
            self.requests.pop(identifier,None)

    def job(self,identifier):
        with self.lock:
            if identifier not in self.jobs:raise PolicyError('JOB_NOT_FOUND','Unknown native policy job',404)
            return copy.deepcopy(self.jobs[identifier])

    def check_begin(self,request):
        if request.get('operation_type')!='MODEL':return None
        with self.lock:
            record=copy.deepcopy(self.registered.get(request.get('profile_id')))
            if record and self.applied.get(record['identity']['arm_id'],{}).get('runtime_profile_id')!=request['profile_id']:
                raise PolicyError('PROFILE_NOT_APPLIED','This managed profile revision is no longer applied; prepare it explicitly',409)
            if record and self.applied.get(record['identity']['arm_id'],{}).get('state')!='ready':
                raise PolicyError('PROFILE_NOT_READY','This managed profile is not prepared',409)
        return record['identity'] if record else None

    def record_begin(self,arm,request,feedback):
        identity=self.check_begin(request)
        if identity:
            with self.lock:
                record={'operation_id':feedback['operation_id'],'arm_id':arm.id,'started_at_ns':time.time_ns(),
                    'runtime_boot_id':self.runtime.boot,'policy':identity}
                selected=self.registered[request['profile_id']]['profile']
                record['effective_profile']={key:copy.deepcopy(selected[key]) for key in
                    ('arm_id','backend','device','state_features','action_features','cameras','action_fps',
                     'calibration_revision','language_conditioned','robot_type','startup_timeout_s','timeout_s',
                     'action_source_max_age_s','max_image_age_ms','_masked_camera_inputs') if key in selected}
                if selected.get('remote'):
                    record['effective_profile']['remote']={key:copy.deepcopy(value) for key,value in selected['remote'].items()
                        if key in ('endpoint','deployment','instance','expected_artifact','semantics','mode','encoding')}
                self.recent_runs.append(record)
                self.recent_runs=self.recent_runs[-128:]
                if self.runtime.config.get('policy_management',{}).get('run_dir'):
                    directory=Path(self.runtime.config['policy_management']['run_dir'])
                    key=hashlib.sha256(feedback['operation_id'].encode()).hexdigest()+'.json'
                    try:atomic_write(directory/key,json.dumps(record,sort_keys=True,allow_nan=False).encode())
                    except OSError as exc:
                        # Never report a definitive admission failure after the owner
                        # already accepted an operation. Surface logging degradation.
                        record['recording_error']=type(exc).__name__+': '+str(exc)[:128]

    def begin(self,arm,request):
        with self.lock:
            self.check_begin(request)
            feedback=arm.begin(request)
            self.record_begin(arm,request,feedback)
            return feedback

    def snapshot(self):
        with self.lock:
            applied=copy.deepcopy(list(self.applied.values()));pending=copy.deepcopy(list(self.pending.values()));active=[]
            for arm in self.runtime.arms.values():
                with arm.lock:
                    if arm.op and arm.op.get('operation_type')=='MODEL':
                        identity=self.registered.get(arm.op['profile_id'],{}).get('identity')
                        active.append({'arm_id':arm.id,'operation_id':arm.op['operation_id'],'policy':copy.deepcopy(identity)})
            applied_ids={record['runtime_profile_id'] for record in self.applied.values() if record.get('state')=='ready'}
            profiles=[{'id':key,'label':record['identity']['name'],'arm_id':record['profile']['arm_id'],
                'backend':record['profile']['backend'],'language_conditioned':record['profile']['language_conditioned'],
                'action_fps':record['profile']['action_fps'],'profile_id':record['identity']['id'],'profile_revision':record['identity']['profile_revision']} for key,record in self.registered.items() if key in applied_ids]
            return {'applied':applied[-1] if applied else None,'pending':pending[-1] if pending else None,
                'applied_by_arm':copy.deepcopy(self.applied),'pending_by_arm':copy.deepcopy(self.pending),
                'active_runs':active,'recent_runs':copy.deepcopy(self.recent_runs),'descriptors':copy.deepcopy(list(self.descriptors.values())),
                'jobs':[copy.deepcopy(job) for job in list(self.jobs.values())[-32:]],'runtime_profiles':profiles,
                'capabilities':{'local_prepare':True,'remote_inspect':True,'session_admitted_on_inspect':False,'boundary':'operation_completion'}}

    def close(self):
        self.closed=True;self.stop.set()
        if self.thread:self.thread.join(timeout=2)
        self.executor.shutdown(wait=False,cancel_futures=True)
