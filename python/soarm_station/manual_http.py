"""Kinematics-only dashboard routes. Returned targets use the existing DIRECT stream."""
import asyncio
from aiohttp import web
from .manual_control import ManualControl

class ManualRoutes:
    def __init__(self, config):
        self.arms = {arm['id']: arm for arm in config.get('arms', [])}
        self.models = {}
        self.step_locks = {}

    def model(self, arm):
        if arm not in self.arms:
            raise ValueError('Unknown arm resource')
        if arm not in self.models:
            self.models[arm] = ManualControl(self.arms[arm])
        return self.models[arm]

    async def handle(self, request):
        arm, operation = request.match_info['arm'], request.match_info['operation']
        try:
            model = self.model(arm)
            if operation == 'describe' and request.method == 'GET':
                description = model.describe()
                return web.json_response({'schema_version': 1, 'available': True, **description})
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
            positions, gripper = body['positions'], body['gripper']
            if operation == 'fk':
                measured = await asyncio.to_thread(model.fk, positions, gripper)
                target = body.get('target')
                overlay = await asyncio.to_thread(model.fk, target['positions'], target['gripper']) if target else None
                return web.json_response({'measured': measured, 'target': overlay})
            if operation == 'step':
                lock = self.step_locks.setdefault(arm, asyncio.Lock())
                if lock.locked():
                    return web.json_response({'message': 'Previous IK calculation is still running'}, status=409)
                async with lock:
                    result = await asyncio.to_thread(model.step, positions, gripper,
                        keys=body.get('keys', []), dt_s=body.get('dt_s', 1/30), level=body.get('level', 2),
                        shift=body.get('shift', False), position_only=body.get('position_only', False),
                        reference_positions=body.get('reference_positions'), reference_gripper=body.get('reference_gripper'))
            elif operation == 'preset':
                result = await asyncio.to_thread(model.preset, body['name'], positions, body.get('reference_gripper',gripper),
                                                 preserve_wrist=body.get('preserve_wrist', False))
            elif operation == 'roll':
                result = await asyncio.to_thread(model.roll_preset, body['name'], positions, body.get('reference_gripper',gripper))
            else:
                raise ValueError('Unknown manual calculation')
            return web.json_response(result)
        except (KeyError, ValueError, TypeError, OSError) as exc:
            if operation == 'describe':
                return web.json_response({'schema_version': 1, 'available': False, 'presets': {}, 'reason': str(exc)})
            return web.json_response({'message': str(exc)}, status=400)

    def mount(self, app):
        app.router.add_get('/api/manual/{arm}/{operation:describe}', self.handle)
        app.router.add_post('/api/manual/{arm}/{operation:fk|step|preset|roll}', self.handle)
        return self
