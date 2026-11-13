"""Management orchestration; profile/source files remain the authorities."""
import copy
import os
import time
import threading
from pathlib import Path
from .policy_catalog import PolicyCatalog
from .policy_profiles import ProfileStore,parse_profile,validate_document
from .policy_uploads import UploadStore
from .policy_bindings import compile_profile
from .policy_common import PolicyError,atomic_write,revision


class PolicyManager:
    def __init__(self,config,resources=None,bridge=None):
        self.config=config;self.resource_provider=resources;self.bridge=bridge;self.closed=False
        self._lock=threading.RLock();self._inspection_drafts={};self._descriptors={};self._runtime_cache=None
        root=Path(config.get('_root') or os.environ.get('SOARM_STATION_ROOT') or Path(__file__).resolve().parents[2])
        settings=config.get('policy_management',{})
        base=Path(config.get('_path',root/'config/runtime.yaml')).expanduser().absolute().parent
        def location(value):
            path=Path(value).expanduser()
            return (path if path.is_absolute() else base/path).resolve()
        self.source_config=location(settings.get('source_config',root/'config/model-sources.yaml'))
        self.profiles_dir=location(settings.get('profiles_dir',settings.get('profile_store',root/'local/policy-profiles')))
        self.cache_db=location(settings.get('cache_db',root/'local/policy-management.sqlite3'))
        self.catalog=PolicyCatalog(self.source_config,self.cache_db)
        self.store=ProfileStore(self.profiles_dir,validator=self._validate_refs)
        self.uploads=UploadStore(self.cache_db,source_lookup=self.catalog.source,
            inspect_package=self.catalog.inspect_package,on_publish=lambda source:self.catalog.scan([source]))

    def resource_config(self):
        if self.config.get('arms') is not None:return self.config
        public=self.resource_provider() if callable(self.resource_provider) else self.resource_provider or {}
        return {'arms':public.get('arms',[]),'cameras':public.get('cameras',[])}

    def sources(self):
        value=self.catalog.sources();value['profile_store']=str(self.profiles_dir);return value

    def reload_sources(self,body):
        if set(body)-{'expected_revision'}:raise PolicyError('INVALID_REQUEST','Source locations are edited in the configured host file, not this API')
        value=self.catalog.reload(body.get('expected_revision'));value['profile_store']=str(self.profiles_dir);return value

    def _validate_refs(self,document):
        validation,_=compile_profile(document,self.catalog,self.resource_config(),self._cached_descriptor(document))
        return validation

    @staticmethod
    def _remote_key(document):
        from .policy_common import revision
        return revision({'execution':document['execution'],'settings':document.get('settings',{})})

    def _cached_descriptor(self,document):
        if document['execution']['kind']!='remote':return None
        with self._lock:value=self._descriptors.get(self._remote_key(document))
        return copy.deepcopy(value[1]) if value and time.monotonic()-value[0]<30 else None

    def validate(self,body):return self.store.validate(body)

    def profiles(self):
        result=self.store.list();runtime=self.runtime();applied=runtime.get('applied_by_arm',{})
        ids={v.get('id') for v in applied.values() if v}
        if runtime.get('applied'):ids.add(runtime['applied'].get('id'))
        versions={v['id']:v.get('profile_revision') for v in applied.values() if v}
        if runtime.get('applied'):versions[runtime['applied']['id']]=runtime['applied'].get('profile_revision')
        for p in result['profiles']:
            p['applied_revision']=versions.get(p['id']);p['applied']=versions.get(p['id'])==p['revision']
        return result

    def models(self,q='',source_id=None):
        result=self.catalog.models(q,source_id)
        def compact(model):
            item=copy.deepcopy(model);files=item.get('files') or []
            item['file_count']=len(files);item['files']=files[:32];item['files_truncated']=len(files)>32
            return item
        result['models']=[compact(m) for m in result['models']]
        for group in result['groups']:group['models']=[compact(m) for m in group['models']]
        return result

    def model(self,model_id):
        result=self.catalog.get_model(model_id);result['file_count']=len(result.get('files') or [])
        result['files_truncated']=False;return result

    def _pin(self,profile):
        result=copy.deepcopy(profile);artifact=result.get('_artifact')
        if artifact and artifact.get('files'):
            import json
            path=self.cache_db.parent/'policy-artifact-pins'/(revision(artifact).removeprefix('sha256:')+'.json')
            data=json.dumps(artifact,sort_keys=True,allow_nan=False).encode()
            if not path.exists():atomic_write(path,data)
            elif path.read_bytes()!=data:raise PolicyError('ARTIFACT_PIN_CONFLICT','Artifact inventory pin differs',409)
            result['_artifact']={k:v for k,v in artifact.items() if k!='files'}
            result['_artifact']['inventory_path']=str(path.resolve())
        return result

    def _call(self,op,**kwargs):
        if self.bridge is None:raise PolicyError('RUNTIME_UNAVAILABLE','No native runtime bridge is attached',503)
        try:return self.bridge.call(op,**kwargs)
        except PolicyError:raise
        except Exception as exc:raise PolicyError('RUNTIME_UNAVAILABLE','Native policy-management request failed: '+str(exc)[:256],503) from None

    def runtime(self):
        with self._lock:cached=self._runtime_cache
        if cached and time.monotonic()-cached[0]<.25:return copy.deepcopy(cached[1])
        try:
            value={'schema_version':1,**self._call('policy_runtime')}
            with self._lock:self._runtime_cache=(time.monotonic(),copy.deepcopy(value))
            return value
        except PolicyError as exc:
            return {'schema_version':1,'applied':None,'pending':None,'applied_by_arm':{},'pending_by_arm':{},
                'descriptors':[],'jobs':[],'capabilities':{'local_prepare':False,'remote_inspect':False},
                'state':'unavailable','reason':exc.message}

    def _document(self,body):
        if 'text' in body and 'document' not in body:return parse_profile(body['text'])
        if 'document' in body and 'text' not in body:return copy.deepcopy(body['document'])
        raise PolicyError('INVALID_REQUEST','Provide exactly text or document')

    def inspect_draft(self,body):
        document=self._document(body);structural=validate_document(document)
        if not structural['valid']:return {'schema_version':1,'validation':structural,'descriptor':None,'admitted':False}
        if document['execution']['kind']=='local':
            validation,_=compile_profile(document,self.catalog,self.resource_config(),recheck=False)
            return {'schema_version':1,'validation':validation,'descriptor':validation.get('resolved_model'),'admitted':False}
        job=self._call('policy_inspect',execution=document['execution'],settings=document.get('settings',{}))
        with self._lock:
            self._inspection_drafts[job['id']]=copy.deepcopy(document)
            while len(self._inspection_drafts)>128:self._inspection_drafts.pop(next(iter(self._inspection_drafts)))
        return {'schema_version':1,'validation':self._validate_refs(document),'descriptor':None,'admitted':False,'job':job}

    def job(self,job_id):
        result=self._call('policy_job',job_id=job_id)
        if result.get('state') in ('complete','failed'):
            with self._lock:self._runtime_cache=None
        with self._lock:document=self._inspection_drafts.get(job_id)
        if document and result.get('state')=='complete' and result.get('result',{}).get('descriptor'):
            descriptor=result['result']['descriptor']
            with self._lock:self._descriptors[self._remote_key(document)]=(time.monotonic(),copy.deepcopy(descriptor))
            result=copy.deepcopy(result);result['result']['validation']=self._validate_refs(document)
        return {'schema_version':1,'job':result,**{k:v for k,v in self.runtime().items() if k in ('applied','pending')}}

    def inspect_saved(self,profile_id,body):
        profile=self.store.get(profile_id);self._revision(profile,body)
        return self.inspect_draft({'document':profile['document']})

    @staticmethod
    def _revision(profile,body):
        if body.get('expected_revision')!=profile['revision']:
            raise PolicyError('REVISION_CONFLICT','Saved profile changed; refresh before preparing',409,
                {'revision':profile['revision'],'text':profile['text']})

    def prepare(self,profile_id,body,remote_descriptor=None):
        if set(body)-{'expected_revision','mode'}:raise PolicyError('INVALID_REQUEST','Unsupported prepare fields')
        profile=self.store.get(profile_id);self._revision(profile,body)
        if not profile['validation']['valid']:raise PolicyError('INVALID_PROFILE','Save a schema-valid profile before applying',details=profile['validation'])
        document=profile['document'];mode=body.get('mode','idle')
        if mode not in ('idle','after_current'):raise PolicyError('UNSUPPORTED_PREPARE_MODE','Supported modes are idle and after_current')
        if document['execution']['kind']=='remote' and remote_descriptor is None:
            # The worker performs discovery and contract validation before registering.
            execution=document['execution']
            request={'document':document,'profile_revision':profile['revision'],'mode':mode,'source_revision':None}
        else:
            validation,compiled=compile_profile(document,self.catalog,self.resource_config(),remote_descriptor,recheck=True)
            if not validation['ready']:raise PolicyError('PROFILE_NOT_READY','Profile is not ready for preparation',409,validation)
            request={'document':document,'profile_revision':profile['revision'],'runtime_profile':self._pin(compiled),
                'mode':mode,'source_revision':self.catalog.sources()['revision']}
        response=self._call('policy_prepare',**request)
        with self._lock:self._runtime_cache=None
        return {'schema_version':1,'job':response,**{k:v for k,v in self.runtime().items() if k in ('applied','pending')}}

    def upload(self,upload_id):
        result=self.uploads.get(upload_id)
        if result.get('state')=='cataloged' and not result.get('model_id'):
            for model in self.catalog.models(source_id=result['source_id'])['models']:
                if model.get('artifact_revision')==result.get('artifact_revision'):
                    result['model_id']=model['id'];break
        return result

    def public_runtime_profiles(self):
        snapshot=self.runtime()
        return snapshot.get('runtime_profiles',[])

    def close(self):
        if self.closed:return
        self.closed=True;self.uploads.close();self.catalog.close()
        if self.bridge is not None and hasattr(self.bridge,'close'):self.bridge.close()
