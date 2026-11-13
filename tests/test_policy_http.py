"""Real management CRUD through a hardware-free loopback aiohttp server."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestClient as AiohttpClient, TestServer as AiohttpServer

from soarm_station.policy_http import mount_policy_routes
from test_policy_manager import checkpoint_files, manager, profile_document, write_sources


@asynccontextmanager
async def api(manager):
    app = web.Application(client_max_size=256 * 1024)
    assert mount_policy_routes(app, manager) is manager
    async with AiohttpClient(AiohttpServer(app)) as client:
        yield client


async def request(client, method, path, *, body=None, status=200, **kwargs):
    if body is not None:
        kwargs['json'] = body
    async with client.request(method, '/api/v1' + path, **kwargs) as response:
        text = await response.text()
        assert response.status == status, (response.status, text)
        result = json.loads(text)
        assert result['schema_version'] == 1
        return result


async def wait_scan_http(client, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = await request(client, 'GET', '/policy-scans/' + job['id'])
        if result['job']['state'] in ('complete', 'failed'):
            assert result['job']['state'] == 'complete', result
            return result['job']
        await asyncio.sleep(.005)
    raise AssertionError('HTTP scan did not finish within five seconds')


def test_sources_catalog_scan_envelopes_and_atomic_reload(manager):
    async def exercise():
        async with api(manager) as client:
            before = await request(client, 'GET', '/policy-sources')
            assert before['profile_store'] == str(manager.profiles_dir)
            assert before['config_path'] == str(manager.source_config)
            assert before['roots'][0]['id'] == 'ssd'
            assert before['roots'][0]['state'] == 'online'
            models = await request(client, 'GET', '/policies?q=Fixture')
            assert models['total'] == 1 and models['groups'][0]['source_id'] == 'ssd'
            model = models['models'][0]
            found = await request(client, 'GET', '/policies/' + model['id'])
            assert found['model'] == model
            scan = await request(client, 'POST', '/policy-scans', body={'source_ids': ['ssd']})
            await wait_scan_http(client, scan['job'])
            unknown = await request(client, 'POST', '/policy-scans', body={'source_ids': ['unknown']}, status=404)
            assert unknown['code'] == 'SOURCE_NOT_FOUND'
            manager.source_config.write_text('schema_version: 1\nroots: [{id: replacement, path: models, enabled: invalid}]\n')
            invalid = await request(client, 'POST', '/policy-sources/reload', body={'expected_revision': before['revision']}, status=400)
            assert invalid['code'] == 'INVALID_SOURCES'
            after = await request(client, 'GET', '/policy-sources')
            assert after['revision'] == before['revision'] and after['text'] == before['text']
            assert [root['id'] for root in after['roots']] == ['ssd']
            rejected = await request(client, 'POST', '/policy-sources/reload', body={'roots': []}, status=400)
            assert rejected['code'] == 'INVALID_REQUEST'
            write_sources(manager.source_config, [{'id': 'ssd', 'label': 'Renamed fixture source', 'path': 'models', 'allow_upload': True}])
            reloaded = await request(client, 'POST', '/policy-sources/reload', body={'expected_revision': before['revision']})
            assert reloaded['revision'] != before['revision']
            assert reloaded['changed_source_ids'] == ['ssd']
            assert reloaded['profile_store'] == str(manager.profiles_dir)
            await wait_scan_http(client, reloaded['scan_job'])
            conflict = await request(client, 'POST', '/policy-sources/reload', body={'expected_revision': before['revision']}, status=409)
            assert conflict['code'] == 'REVISION_CONFLICT'
    asyncio.run(exercise())


def test_profile_crud_import_conflict_refresh_and_single_file_authority(manager):
    async def exercise():
        async with api(manager) as client:
            document = profile_document(manager, unresolved=True)
            text = '# A draft whose artifact is not copied yet\n' + json.dumps(document)
            created = await request(client, 'POST', '/policy-profiles', body={'text': text})
            assert created['text'] == text
            assert created['validation']['valid'] and not created['validation']['ready']
            assert created['validation']['errors']
            found = await request(client, 'GET', '/policy-profiles/cup_act')
            assert found == created
            listed = await request(client, 'GET', '/policy-profiles')
            assert listed['profiles'][0]['id'] == 'cup_act'
            assert listed['profiles'][0]['state'] == 'draft'
            assert listed['profiles'][0]['can_apply'] is False
            preview = await request(client, 'POST', '/policy-profiles/import-preview', body={'text': text})
            assert preview['valid'] and preview['conflict']['revision'] == created['revision']
            duplicate = await request(client, 'POST', '/policy-profiles/import', body={'text': text, 'id': 'cup_act', 'mode': 'new'}, status=409)
            assert duplicate['details']['text'] == text
            copied = await request(client, 'POST', '/policy-profiles/import', body={'text': text, 'id': 'cup_act_copy', 'mode': 'new'})
            assert copied['id'] == copied['document']['id'] == 'cup_act_copy'
            edited_document = deepcopy(created['document'])
            edited_document['name'] = 'Updated draft'
            updated = await request(client, 'PUT', '/policy-profiles/cup_act', body={'document': edited_document, 'expected_revision': created['revision']})
            assert updated['revision'] != created['revision']
            stale = await request(client, 'PUT', '/policy-profiles/cup_act', body={'text': text, 'expected_revision': created['revision']}, status=409)
            assert stale['details']['revision'] == updated['revision']
            assert stale['details']['text'] == updated['text']
            path = manager.profiles_dir / 'cup_act.yaml'
            external = updated['text'].replace('Updated draft', 'External draft')
            path.write_text(external)
            refreshed = await request(client, 'POST', '/policy-profiles/refresh', body={})
            assert {item['name'] for item in refreshed['profiles']} == {'External draft', 'Cup placement'}
            stale = await request(client, 'PUT', '/policy-profiles/cup_act', body={'text': updated['text'], 'expected_revision': updated['revision']}, status=409)
            assert stale['details']['text'] == external
            assert path.read_text() == external
            unknown = await request(client, 'GET', '/policy-profiles/absent', status=404)
            assert unknown['code'] == 'PROFILE_NOT_FOUND'
    asyncio.run(exercise())


def test_validation_success_has_versioned_envelope(manager):
    async def exercise():
        async with api(manager) as client:
            validation = await request(client, 'POST', '/policy-profiles/validate', body={'document': profile_document(manager)})
            assert validation['valid'] and validation['ready']
    asyncio.run(exercise())


def test_bad_raw_requests_are_controlled_4xx_and_preserve_saved_revision(manager):
    async def exercise():
        async with api(manager) as client:
            created = await request(client, 'POST', '/policy-profiles', body={'document': profile_document(manager)})
            invalid_text = 'schema_version: 1\nname: [unterminated\n'
            invalid = await request(client, 'PUT', '/policy-profiles/cup_act', body={'text': invalid_text, 'expected_revision': created['revision']}, status=400)
            assert invalid['details']['text'] == invalid_text
            assert invalid['details']['validation']['errors'][0]['line'] >= 1
            assert (await request(client, 'GET', '/policy-profiles/cup_act'))['revision'] == created['revision']
            for payload in ({}, {'text': created['text'], 'document': created['document']}, {'text': '!!python/object/apply:os.system [no]\n'}):
                rejected = await request(client, 'POST', '/policy-profiles', body=payload, status=400)
                assert rejected['code']
            rejected = await request(client, 'POST', '/policy-profiles', data='{"text":', headers={'Content-Type': 'application/json'}, status=400)
            assert rejected['code'] == 'INVALID_REQUEST'
            rejected = await request(client, 'POST', '/policy-profiles', body=[], status=400)
            assert rejected['code'] == 'INVALID_REQUEST'
            oversized = await request(client, 'POST', '/policy-profiles', body={'text': ' ' * (64 * 1024 + 1)}, status=413)
            assert oversized['code'] == 'PROFILE_TOO_LARGE'
    asyncio.run(exercise())


def test_exports_are_attachments_with_correct_content_type_and_filename(manager):
    async def exercise():
        async with api(manager) as client:
            created = await request(client, 'POST', '/policy-profiles', body={'document': profile_document(manager)})
            for format in ('yaml', 'json'):
                async with client.get('/api/v1/policy-profiles/cup_act/export?format=' + format) as response:
                    text = await response.text()
                    assert response.status == 200, text
                    assert response.content_type == 'application/' + format
                    assert 'cup_act.' + format in response.headers['Content-Disposition']
                    if format == 'yaml':
                        assert text == created['text']
                    else:
                        assert json.loads(text) == created['document']
            rejected = await request(client, 'GET', '/policy-profiles/cup_act/export?format=pickle', status=400)
            assert rejected['code'] == 'INVALID_EXPORT_FORMAT'
    asyncio.run(exercise())


def test_runtime_and_prepare_without_bridge_are_honestly_unavailable(manager):
    async def exercise():
        async with api(manager) as client:
            created = await request(client, 'POST', '/policy-profiles', body={'document': profile_document(manager)})
            snapshot = await request(client, 'GET', '/policy-runtime')
            assert snapshot['state'] == 'unavailable'
            assert snapshot['applied'] is None and snapshot['pending'] is None
            assert not snapshot['capabilities']['local_prepare']
            prepared = await request(client, 'POST', '/policy-profiles/cup_act/prepare', body={'expected_revision': created['revision'], 'mode': 'idle'}, status=503)
            assert prepared['code'] == 'RUNTIME_UNAVAILABLE'
            stale = await request(client, 'POST', '/policy-profiles/cup_act/prepare', body={'expected_revision': 'stale'}, status=409)
            assert stale['code'] == 'REVISION_CONFLICT'
            inspected = await request(client, 'POST', '/policy-profiles/inspect-draft', body={'document': profile_document(manager)})
            assert inspected['admitted'] is False and inspected['descriptor']
            remote = await request(client, 'POST', '/policy-profiles/inspect-draft', body={'document': profile_document(manager, remote=True)}, status=503)
            assert remote['code'] == 'RUNTIME_UNAVAILABLE'
            after = await request(client, 'GET', '/policy-runtime')
            assert after['applied'] is None and after['pending'] is None
    asyncio.run(exercise())


def test_compact_upload_chunk_integrity_no_overwrite_and_catalog_publication(manager):
    async def exercise():
        async with api(manager) as client:
            payloads = checkpoint_files()
            inventory = [{'path': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                         for path, data in payloads.items()]
            upload = await request(client, 'POST', '/model-uploads', body={'source_id': 'ssd', 'destination_name': 'uploaded-demo', 'kind': 'folder', 'files': inventory})
            assert upload['state'] == 'uploading' and upload['chunk_size'] == 131072
            upload_path = '/model-uploads/' + upload['id']
            incomplete = await request(client, 'POST', upload_path + '/finalize', body={}, status=409)
            assert incomplete['code'] == 'UPLOAD_INCOMPLETE'
            first_chunk = None
            for index, file in enumerate(upload['files']):
                data = payloads[file['path']]
                digest = hashlib.sha256(data).hexdigest()
                chunk_path = upload_path + f'/files/{index}/chunks/0'
                if index == 0:
                    bad = await request(client, 'PUT', chunk_path, data=data, headers={'X-Chunk-SHA256': '0' * 64}, status=409)
                    assert bad['code'] == 'CHUNK_HASH_MISMATCH'
                received = await request(client, 'PUT', chunk_path, data=data, headers={'X-Chunk-SHA256': digest})
                if index == 0:
                    first_chunk = (chunk_path, data, digest)
                    retry = await request(client, 'PUT', chunk_path, data=data, headers={'X-Chunk-SHA256': digest})
                    assert retry['received_bytes'] == received['received_bytes']
                    changed = b'x' * len(data)
                    conflict = await request(client, 'PUT', chunk_path, data=changed, headers={'X-Chunk-SHA256': hashlib.sha256(changed).hexdigest()}, status=409)
                    assert conflict['code']
            resumed = await request(client, 'GET', upload_path)
            assert resumed['received_bytes'] == resumed['total_bytes']
            assert all(file['chunks'][0]['sha256'] == hashlib.sha256(payloads[file['path']]).hexdigest() for file in resumed['files'])
            path, _data, _digest = first_chunk
            too_big = await request(client, 'PUT', path, data=b'x' * 131073, headers={'X-Chunk-SHA256': hashlib.sha256(b'x' * 131073).hexdigest()}, status=413)
            assert too_big['code'] == 'CHUNK_TOO_LARGE'
            verifying = await request(client, 'POST', upload_path + '/finalize', body={})
            assert verifying['state'] in ('verifying', 'cataloged')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                final = await request(client, 'GET', upload_path)
                if final['state'] in ('cataloged', 'failed'):
                    break
                await asyncio.sleep(.005)
            assert final['state'] == 'cataloged', final
            await wait_scan_http(client, final['scan_job'])
            final = await request(client, 'GET', upload_path)
            assert final['model_id']
            models = await request(client, 'GET', '/policies')
            assert models['total'] == 2
            uploaded = next(model for model in models['models'] if model['relative_path'] == 'uploaded-demo')
            assert uploaded['artifact_revision'] == final['artifact_revision']
            assert uploaded['state'] == 'available' and uploaded['category'] == 'act'
            for name, data in payloads.items():
                assert (manager.source_config.parent / 'models' / 'uploaded-demo' / name).read_bytes() == data
            duplicate = await request(client, 'POST', '/model-uploads', body={'source_id': 'ssd', 'destination_name': 'uploaded-demo', 'kind': 'folder', 'files': inventory}, status=409)
            assert duplicate['code'] == 'DESTINATION_EXISTS'
            all_uploads = await request(client, 'GET', '/model-uploads')
            assert all_uploads['uploads'][0]['id'] == upload['id']
    asyncio.run(exercise())
