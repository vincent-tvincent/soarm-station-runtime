"""Mountable management routes; the host application owns authentication."""
import asyncio
import json
from aiohttp import web
from .policy_common import PolicyError


def mount_policy_routes(app,manager):
    async def body(request):
        value=await request.json()
        if not isinstance(value,dict):raise PolicyError('INVALID_REQUEST','JSON object required')
        return value
    def route(method,path,operation,needs_body=False,binary=False):
        async def handler(request):
            try:
                if binary:
                    data=bytearray()
                    async for chunk in request.content.iter_chunked(32768):
                        data.extend(chunk)
                        if len(data)>131072:raise PolicyError('CHUNK_TOO_LARGE','Maximum raw chunk is128KiB',413)
                    result=await asyncio.to_thread(operation,request,bytes(data))
                else:
                    payload=await body(request) if needs_body else None
                    result=await asyncio.to_thread(operation,request,payload)
                if isinstance(result,web.StreamResponse):return result
                return web.json_response(result)
            except PolicyError as exc:return web.json_response(exc.response(),status=exc.status)
            except (ValueError,TypeError,KeyError) as exc:
                return web.json_response({'schema_version':1,'code':'INVALID_REQUEST','message':str(exc)[:256]},status=400)
        app.router.add_route(method,path,handler)
    prefix='/api/v1'
    route('GET',prefix+'/policy-sources',lambda r,b:manager.sources())
    route('POST',prefix+'/policy-sources/reload',lambda r,b:manager.reload_sources(b),True)
    route('GET',prefix+'/policies',lambda r,b:manager.models(r.query.get('q',''),r.query.get('source_id')))
    route('GET',prefix+'/policies/{id}',lambda r,b:{'schema_version':1,'model':manager.model(r.match_info['id'])})
    route('POST',prefix+'/policy-scans',lambda r,b:{'schema_version':1,'job':manager.catalog.scan(b.get('source_ids'))},True)
    route('GET',prefix+'/policy-scans/{id}',lambda r,b:{'schema_version':1,'job':manager.catalog.scan_status(r.match_info['id'])})
    route('GET',prefix+'/policy-profiles',lambda r,b:manager.profiles())
    route('POST',prefix+'/policy-profiles',lambda r,b:manager.store.save(b,create=True),True)
    route('POST',prefix+'/policy-profiles/validate',lambda r,b:{'schema_version':1,**manager.validate(b)},True)
    route('POST',prefix+'/policy-profiles/refresh',lambda r,b:manager.store.refresh(),True)
    route('POST',prefix+'/policy-profiles/import-preview',lambda r,b:manager.store.import_preview(b),True)
    route('POST',prefix+'/policy-profiles/import',lambda r,b:manager.store.import_profile(b),True)
    route('POST',prefix+'/policy-profiles/inspect-draft',lambda r,b:manager.inspect_draft(b),True)
    route('GET',prefix+'/policy-profiles/{id}',lambda r,b:manager.store.get(r.match_info['id']))
    route('PUT',prefix+'/policy-profiles/{id}',lambda r,b:manager.store.save(b,profile_id=r.match_info['id']),True)
    def export(request,_):
        text,mime=manager.store.export(request.match_info['id'],request.query.get('format','yaml'))
        media=mime.split(';',1)[0].strip();extension='json' if media=='application/json' else 'yaml'
        return web.Response(text=text,content_type=media,charset='utf-8',headers={'Content-Disposition':f'attachment; filename="{request.match_info["id"]}.{extension}"'})
    route('GET',prefix+'/policy-profiles/{id}/export',export)
    route('POST',prefix+'/policy-profiles/{id}/inspect',lambda r,b:manager.inspect_saved(r.match_info['id'],b),True)
    route('POST',prefix+'/policy-profiles/{id}/prepare',lambda r,b:manager.prepare(r.match_info['id'],b),True)
    route('GET',prefix+'/policy-preparations/{id}',lambda r,b:manager.job(r.match_info['id']))
    route('GET',prefix+'/policy-runtime',lambda r,b:manager.runtime())
    route('POST',prefix+'/model-uploads',lambda r,b:manager.uploads.create(b),True)
    route('GET',prefix+'/model-uploads',lambda r,b:manager.uploads.list())
    route('GET',prefix+'/model-uploads/{id}',lambda r,b:manager.upload(r.match_info['id']))
    route('PUT',prefix+'/model-uploads/{id}/files/{file}/chunks/{offset}',lambda r,data:manager.uploads.put_chunk(r.match_info['id'],int(r.match_info['file']),int(r.match_info['offset']),data,r.headers.get('X-Chunk-SHA256','')),binary=True)
    route('POST',prefix+'/model-uploads/{id}/finalize',lambda r,b:manager.uploads.finalize(r.match_info['id']),True)
    route('POST',prefix+'/model-uploads/{id}/cancel',lambda r,b:manager.uploads.cancel(r.match_info['id']),True)
    return manager
