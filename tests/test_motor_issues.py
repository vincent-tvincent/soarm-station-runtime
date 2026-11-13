import copy
import unittest

from soarm_station.motor_issues import project_motor_issues,validate_motor_issues,motor_issue_for


def arm():
    return {'id':'arm1','joints':[{'name':'elbow_flex','goal_tolerance_rad':.07},
                                 {'name':'wrist_flex','goal_tolerance_rad':.03}],
            'gripper':{'name':'jaw','goal_tolerance_percent':3.}}


def snapshot():
    return {'arm_id':'arm1','phase':'ACTIVE','error':'','age_ms':5.,'time_ns':10_000_000_000,
            'sequence':50,'health_time_ns':9_500_000_000,'positions':{'elbow_flex':.5,'wrist_flex':.2},
            'reference_positions':{'elbow_flex':.51,'wrist_flex':.2},'gripper':50.,'reference_gripper':51.,
            'stats':{'tracking_state':'FOLLOWING','limited_actuators':[]},
            'hold_context':{},'health':{'source':'measured','motors':[
                {'name':'elbow_flex','id':3,'role':'joint','unit':'rad','torque_enabled':True,'voltage_v':12.,'temperature_c':30.},
                {'name':'wrist_flex','id':4,'role':'joint','unit':'rad','torque_enabled':True,'voltage_v':12.,'temperature_c':30.},
                {'name':'jaw','id':6,'role':'gripper','unit':'percent','torque_enabled':True,'voltage_v':12.,'temperature_c':30.},
            ]}}


def project(value):return project_motor_issues(value,arm_config=arm())
def row(value,name='elbow_flex'):return project(value)['by_name'][name]
def latch(previous,issue):
    if issue['active'] is True:return issue['kind']
    if previous in issue['clear_kinds']:return None
    return previous


class MotorIssueTests(unittest.TestCase):
    def test_explicit_stall_identifies_only_the_named_motor_and_preserves_input(self):
        value=snapshot();value['phase']='HOLD';value['error']='TRACKING_STALLED: elbow_flex'
        original=copy.deepcopy(value);result=project(value)
        self.assertEqual(result['by_name']['elbow_flex']['state'],'stall')
        self.assertEqual(result['by_name']['elbow_flex']['code'],'TRACKING_STALLED')
        self.assertEqual([n for n,r in result['by_name'].items() if r['active']],['elbow_flex'])
        self.assertEqual(value,original)

    def test_stale_unknown_and_error_string_reset_do_not_clear_a_latched_stall(self):
        value=snapshot();value['phase']='HOLD';value['error']='TRACKING_STALLED: elbow_flex'
        value['hold_context']={'reason':value['error']}
        previous=latch(None,row(value));self.assertEqual(previous,'stall')
        value['error']=''
        self.assertEqual(latch(previous,row(value)),'stall')
        value['phase']='ACTIVE';value['age_ms']=900.
        self.assertEqual(row(value)['state'],'unknown')
        self.assertEqual(latch(previous,row(value)),'stall')
        value['age_ms']=5.;value['stats']={};value['reference_positions']=None
        self.assertEqual(row(value)['clear_kinds'],[])
        self.assertEqual(latch(previous,row(value)),'stall')

    def test_fresh_following_at_configured_goal_tolerance_is_an_explicit_clear(self):
        value=snapshot();value['reference_positions']['elbow_flex']=.56
        issue=row(value)
        self.assertEqual(issue['state'],'clear')
        self.assertIn('stall',issue['clear_kinds']);self.assertIn('fault',issue['clear_kinds'])
        self.assertIsNone(latch('stall',issue));self.assertAlmostEqual(issue['tracking_error'],.06)
        value['reference_positions']['elbow_flex']=.58
        self.assertEqual(row(value)['state'],'unknown')
        self.assertEqual(latch('stall',row(value)),'stall')

    def test_ordinary_lead_limiting_and_gripper_contact_are_not_stalls(self):
        value=snapshot();value['stats']={'tracking_state':'LEAD_LIMITED','limited_actuators':['elbow_flex','_gripper']}
        value['reference_positions']['elbow_flex']=.62;value['reference_gripper']=35.
        result=project(value)
        self.assertEqual(result['by_name']['elbow_flex']['state'],'unknown')
        self.assertEqual(result['by_name']['jaw']['state'],'unknown')
        self.assertFalse(any(r['active'] for r in result['by_name'].values()))
        self.assertEqual(latch('stall',result['by_name']['jaw']),'stall')
        value['error']='TRACKING_STALLED: _gripper';value['phase']='HOLD'
        self.assertEqual(row(value,'jaw')['state'],'stall')
        value['error']='';value['phase']='ACTIVE';value['stats']={'tracking_state':'FOLLOWING','limited_actuators':[]};value['reference_gripper']=51.
        self.assertIsNone(latch('stall',row(value,'jaw')))

    def test_observed_direct_nonprogress_flags_at_indicator_threshold_without_motion_timeout(self):
        value=snapshot();value['operation_type']='DIRECT';value['reference_positions']['elbow_flex']=.62
        value['stats']={'tracking_state':'LEAD_LIMITED','limited_actuators':['elbow_flex'],
                        'no_progress_s':{'elbow_flex':.999}}
        self.assertEqual(row(value)['state'],'unknown')
        value['stats']['no_progress_s']['elbow_flex']=1.
        original=copy.deepcopy(value);issue=row(value)
        self.assertEqual(issue['state'],'stall');self.assertEqual(issue['code'],'TRACKING_NOT_PROGRESSING')
        self.assertEqual(issue['source'],'runtime_progress');self.assertIn('No measured progress',issue['reason'])
        self.assertEqual(value,original);self.assertEqual(value['phase'],'ACTIVE');self.assertEqual(value['error'],'')
        self.assertFalse(any('tracking_timeout_s' in joint for joint in arm()['joints']))
        configured=arm();configured['manual_control']={'stall_indicator_s':2.}
        self.assertEqual(project_motor_issues(value,arm_config=configured)['by_name']['elbow_flex']['state'],'unknown')
        value['stats']['no_progress_s']['elbow_flex']=2.
        self.assertEqual(project_motor_issues(value,arm_config=configured)['by_name']['elbow_flex']['code'],'TRACKING_NOT_PROGRESSING')

    def test_observed_nonprogress_requires_fresh_limited_body_error_and_current_progress_evidence(self):
        base=snapshot();base['reference_positions']['elbow_flex']=.62
        base['stats']={'tracking_state':'LEAD_LIMITED','limited_actuators':['elbow_flex'],
                       'no_progress_s':{'elbow_flex':2.}}
        variants=[]
        value=copy.deepcopy(base);value['age_ms']=500.;variants.append(value)
        value=copy.deepcopy(base);value['positions'].pop('elbow_flex');variants.append(value)
        value=copy.deepcopy(base);value['reference_positions']=None;variants.append(value)
        value=copy.deepcopy(base);value['reference_positions']['elbow_flex']=.56;variants.append(value)
        value=copy.deepcopy(base);value['phase']='IDLE';variants.append(value)
        value=copy.deepcopy(base);value['stats']['limited_actuators']=[];variants.append(value)
        for duration in (None,float('nan'),-1.,True,'2'):
            value=copy.deepcopy(base);value['stats']['no_progress_s']['elbow_flex']=duration;variants.append(value)
        for value in variants:
            with self.subTest(value=value):
                self.assertIsNone(row(value)['active']);self.assertNotIn('stall',row(value)['clear_kinds'])

    def test_progress_resets_indicator_but_only_following_at_goal_clears_the_latch(self):
        value=snapshot();value['reference_positions']['elbow_flex']=.62
        value['stats']={'tracking_state':'LEAD_LIMITED','limited_actuators':['elbow_flex'],
                        'no_progress_s':{'elbow_flex':2.}}
        previous=latch(None,row(value));self.assertEqual(previous,'stall')
        value['stats']['no_progress_s']['elbow_flex']=.05
        self.assertEqual(row(value)['state'],'unknown');self.assertEqual(latch(previous,row(value)),'stall')
        value['positions']['elbow_flex']=.61
        value['stats']={'tracking_state':'FOLLOWING','limited_actuators':[],'no_progress_s':{}}
        self.assertIsNone(latch(previous,row(value)))

    def test_estimated_stall_excludes_long_gripper_contact_and_native_report_still_wins(self):
        value=snapshot();value['reference_gripper']=20.
        value['stats']={'tracking_state':'LEAD_LIMITED','limited_actuators':['_gripper'],
                        'no_progress_s':{'_gripper':60.,'jaw':60.}}
        self.assertIsNone(row(value,'jaw')['active'])
        value['error']='TRACKING_STALLED: _gripper';value['phase']='HOLD'
        self.assertEqual(row(value,'jaw')['code'],'TRACKING_STALLED')

    def test_current_outside_envelope_wait_is_body_specific_without_stop_timeout(self):
        value=snapshot();value['reference_positions']['elbow_flex']=.64;value['reference_gripper']=20.
        value['stats']={'tracking_state':'REFERENCE_OUTSIDE_ENVELOPE','limited_actuators':['elbow_flex','_gripper'],
                        'no_progress_s':{'elbow_flex':100.,'_gripper':100.}}
        issue=row(value)
        self.assertEqual(issue['state'],'fault');self.assertEqual(issue['code'],'REFERENCE_OUTSIDE_ENVELOPE')
        self.assertIsNone(row(value,'wrist_flex')['active']);self.assertIsNone(row(value,'jaw')['active'])
        self.assertEqual(value['phase'],'ACTIVE');self.assertEqual(value['error'],'')
        value['age_ms']=500.;self.assertIsNone(row(value)['active'])
        value['age_ms']=5.;value['positions']['elbow_flex']=.63
        value['stats']={'tracking_state':'FOLLOWING','limited_actuators':[],'no_progress_s':{}}
        self.assertIsNone(latch('fault',row(value)))

    def test_specific_hardware_fault_uses_observed_unique_motor_identity(self):
        value=snapshot();value['phase']='FAULT';value['error']='Servo protection id=3 status=32'
        result=project(value)
        self.assertEqual(result['by_name']['elbow_flex']['state'],'fault')
        self.assertEqual(result['by_name']['elbow_flex']['code'],'SERVO_PROTECTION')
        self.assertFalse(result['by_name']['wrist_flex']['active'])
        value['health']['motors'][1]['id']=3
        self.assertFalse(any(r['active'] for r in project(value)['by_name'].values()))

    def test_generic_fault_or_unknown_id_stays_summary_only(self):
        for error in ('Torque was lost; reconnect and obtain fresh permission before rearming',
                      'TRACKING_TIMEOUT','Servo protection id=99 status=32','Calibration mismatch: unknown_joint',
                      'TRACKING_STALLED: unknown_joint'):
            value=snapshot();value['phase']='FAULT';value['error']=error
            result=project(value)
            self.assertEqual(result['summary'],error)
            self.assertFalse(any(r['active'] for r in result['by_name'].values()))
            self.assertFalse(any(r['clear_kinds'] for r in result['by_name'].values()))

    def test_known_io_configuration_and_tracking_faults_are_specific_not_blanket(self):
        cases=[('Invalid read reply id=4 address=56 bytes=2','wrist_flex','FEEDBACK_ERROR'),
               ('Calibration mismatch: elbow_flex','elbow_flex','CALIBRATION_MISMATCH')]
        for error,name,code in cases:
            value=snapshot();value['phase']='FAULT';value['error']=error;result=project(value)
            self.assertEqual(result['by_name'][name]['code'],code)
            self.assertEqual(sum(r['active'] is True for r in result['by_name'].values()),1)
        value=snapshot();value['phase']='HOLD';value['error']='TRACKING_ENVELOPE_LOST'
        value['stats']={'tracking_state':'REFERENCE_OUTSIDE_ENVELOPE','limited_actuators':['elbow_flex']}
        self.assertEqual(row(value)['state'],'fault')
        self.assertIsNone(row(value,'wrist_flex')['active'])

    def test_historical_hold_reason_does_not_reassert_a_resolved_issue_during_following(self):
        value=snapshot();value['hold_context']={'reason':'TRACKING_STALLED: elbow_flex'}
        self.assertEqual(row(value)['state'],'clear')
        value['error']='OPERATOR_HOLD';value['phase']='HOLD'
        self.assertEqual(row(value)['state'],'unknown')
        self.assertEqual(project(value)['summary'],'OPERATOR_HOLD')

    def test_fault_clear_needs_fresh_motor_health_not_just_normal_tracking(self):
        value=snapshot();value['health_time_ns']=1
        issue=row(value)
        self.assertEqual(issue['clear_kinds'],['stall'])
        self.assertEqual(latch('fault',issue),'fault')
        value['health_time_ns']=9_500_000_000
        self.assertIsNone(latch('fault',row(value)))
        value['health']['motors'][0]['temperature_c']=None
        self.assertNotIn('fault',row(value)['clear_kinds'])

    def test_missing_measurement_reference_tolerance_or_current_error_field_never_clears(self):
        value=snapshot()
        self.assertEqual(project_motor_issues(value)['by_name']['elbow_flex']['clear_kinds'],[])
        for key in ('error','age_ms','time_ns','reference_positions'):
            altered=copy.deepcopy(value);altered.pop(key)
            self.assertEqual(row(altered)['clear_kinds'],[],key)
        value['positions']['elbow_flex']=float('nan')
        self.assertEqual(row(value)['clear_kinds'],[])

    def test_validator_and_stale_projection_keep_clears_explicit_and_bounded(self):
        projected=project(snapshot());checked=validate_motor_issues(projected,('elbow_flex','wrist_flex','jaw'))
        self.assertEqual(checked,projected)
        stale=motor_issue_for(checked,'elbow_flex',fresh=False)
        self.assertEqual(stale['state'],'unknown');self.assertEqual(stale['clear_kinds'],[])
        self.assertEqual(latch('stall',stale),'stall')
        self.assertEqual(motor_issue_for(checked,'missing')['state'],'unknown')
        self.assertEqual(validate_motor_issues(None,()),{'schema_version':1,'summary':'','by_name':{}})
        invalid_rows=[{'state':'broken'},{'state':[]},{'active':True},{'clear_kinds':['bad']},
                      {'clear_kinds':[{}]},{'feedback_fresh':False},{'tracking_error':float('nan')},
                      {'sample_sequence':True},{'reason':'x'*513}]
        for changes in invalid_rows:
            bad=copy.deepcopy(projected);bad['by_name']['elbow_flex'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_motor_issues(bad,('elbow_flex','wrist_flex','jaw'))
        with self.assertRaises(ValueError):validate_motor_issues(projected,('jaw',))


if __name__=='__main__':unittest.main()
