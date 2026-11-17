"""Bounded, idempotent HTTP action requests with exact-goal cancellation.

No arm-wide Hold is issued here. A cancellation received before ROS admission
is attached to that request and applies to its eventual goal handle only.
"""
import json
import hashlib
import re
import threading
import uuid
from collections import OrderedDict


class RequestConflict(ValueError):pass


class OperationRequests:
    def __init__(self, capacity=256):
        self.records=OrderedDict();self.canceled_before_submission=OrderedDict()
        self.capacity=capacity;self.lock=threading.RLock()

    @staticmethod
    def identity(identifier,principal):
        if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',identifier):
            raise ValueError('request_id must be a bounded identifier')
        if not isinstance(principal,str) or not 1<=len(principal)<=64:
            raise ValueError('operator_session_id must be a bounded identifier')

    def register(self, body, principal, send_goal, encode_result):
        identifier=body['request_id'] if 'request_id' in body else uuid.uuid4().hex
        self.identity(identifier,principal)
        fingerprint=hashlib.sha256(json.dumps([principal,body],sort_keys=True,separators=(',',':'),allow_nan=False).encode()).digest()
        with self.lock:
            existing=self.records.get(identifier)
            if existing:
                if existing['fingerprint']!=fingerprint:raise RequestConflict('request_id already identifies a different request')
                return identifier
            for key,value in list(self.records.items()):
                if len(self.records)<self.capacity:break
                if value['done']:self.records.pop(key)
            if len(self.records)>=self.capacity:raise RuntimeError('Too many unresolved operation requests')
            record={'principal':principal,'fingerprint':fingerprint,'feedback':{},'handle':None,
                    'future':None,'done':False,'result':None,'cancel_requested':False,
                    'cancel_sent':False,'error':'','encode_result':encode_result}
            self.records[identifier]=record
            if self.canceled_before_submission.pop((principal,identifier),False):
                record.update(done=True,cancel_requested=True,result={'result_code':'CANCELED',
                    'reason':'Canceled before submission; no ROS goal was sent',
                    'execution_completed':False,'task_outcome':'NOT_EXECUTED'})
                return identifier
        def feedback(value):
            with self.lock:record['feedback']=dict(value)
        try:
            pending=send_goal(feedback)
            pending.add_done_callback(lambda future:self._admitted(record,future))
        except Exception as error:
            with self.lock:record['error']='Admission could not be confirmed: '+str(error)[:256]
        return identifier

    def _admitted(self,record,future):
        try:
            handle=future.result()
            if not handle.accepted:
                with self.lock:
                    record.update(done=True,result={'result_code':'REJECTED','reason':'Operation rejected',
                        'execution_completed':False,'task_outcome':'UNKNOWN'})
                return
            # Preserve cancellation authority even if observing the result fails.
            with self.lock:record.update(handle=handle,error='')
            try:
                result_future=handle.get_result_async()
                with self.lock:record['future']=result_future
                result_future.add_done_callback(lambda future:self._finished(record,future))
            except Exception as error:
                with self.lock:record['error']='Operation result could not be monitored: '+str(error)[:256]
            finally:self._cancel(record)
        except Exception as error:
            with self.lock:record['error']='Admission could not be confirmed: '+str(error)[:256]

    def _finished(self,record,future):
        try:
            result=record['encode_result'](future.result())
            with self.lock:record.update(done=True,result=result,error='',handle=None,future=None)
        except Exception as error:
            with self.lock:record['error']='Operation result could not be confirmed: '+str(error)[:256]

    def _cancel(self,record):
        with self.lock:
            handle=record['handle']
            if not record['cancel_requested'] or record['done'] or handle is None or record['cancel_sent']:return
            record['cancel_sent']=True
            if record['error'].startswith(('Cancellation','Goal cancellation')):record['error']=''
        try:
            pending=handle.cancel_goal_async()
            def confirmed(future):
                try:
                    response=future.result()
                    if not response.goals_canceling:
                        with self.lock:
                            if not record['done']:
                                record['cancel_sent']=False;record['error']='Goal cancellation is not confirmed'
                except Exception as error:
                    with self.lock:
                        if not record['done']:
                            record['cancel_sent']=False;record['error']='Cancellation could not be confirmed: '+str(error)[:256]
            pending.add_done_callback(confirmed)
        except Exception as error:
            with self.lock:
                if not record['done']:
                    record['cancel_sent']=False;record['error']='Cancellation could not be confirmed: '+str(error)[:256]

    def cancel(self,identifier,principal):
        self.identity(identifier,principal)
        with self.lock:
            if identifier not in self.records:
                key=(principal,identifier)
                if key not in self.canceled_before_submission and len(self.canceled_before_submission)>=self.capacity:
                    raise RuntimeError('Too many unresolved pre-submission cancellations')
                self.canceled_before_submission[key]=True
                self.canceled_before_submission.move_to_end(key)
                return {'accepted':True,'done':False,
                        'reason':'Cancellation intent recorded; this request has not yet been observed'}
            record=self.records[identifier]
            if principal!=record['principal']:raise PermissionError('Operation belongs to another operator session')
            record['cancel_requested']=True
        self._cancel(record)
        with self.lock:
            return {'accepted':record['done'] or record['cancel_sent'] or not bool(record['error']),'done':record['done'],
                    'reason':record['error'] or ('Operation already ended' if record['done'] else 'Cancellation requested for this goal')}

    def snapshot(self,identifier):
        with self.lock:
            record=self.records[identifier]
            return {'done':record['done'],'result':record['result'],'feedback':dict(record['feedback']),
                    'cancel_requested':record['cancel_requested'],'reason':record['error']}

    def contains(self,identifier):
        with self.lock:return identifier in self.records
