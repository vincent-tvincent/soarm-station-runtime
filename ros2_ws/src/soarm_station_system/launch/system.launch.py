"""Service-independent ROS entry point for the full workstation graph."""
import os
import shlex
import sys
from pathlib import Path
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument,OpaqueFunction,SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration,PathJoinSubstitution
from launch_ros.actions import Node


def default_root():
    # This bootstrap must work before the outer core package is on sys.path.
    for parent in Path(__file__).resolve().parents:
        if (parent/'python/soarm_station/config.py').is_file() and (parent/'ros2_ws').is_dir():return str(parent)
    return os.environ.get('SOARM_STATION_ROOT','')


def setup(context):
    candidate=LaunchConfiguration('root').perform(context)
    if not candidate:raise RuntimeError('Pass root:=/absolute/path/to/the/full/soarm-station-runtime/checkout')
    sys.path.insert(0,str(Path(candidate).expanduser().resolve()/'python'))
    from soarm_station.config import load
    from soarm_station.launch_support import resolve_root,child_environment
    root=resolve_root(candidate,__file__)
    config=load(LaunchConfiguration('config').perform(context))
    env=child_environment(root,config,context.environment,
        secrets_file=LaunchConfiguration('secrets_file').perform(context),
        keystore=LaunchConfiguration('security_keystore').perform(context))
    respawn=LaunchConfiguration('respawn').perform(context).lower()=='true'
    log_level=LaunchConfiguration('log_level').perform(context)
    actions=[SetEnvironmentVariable(name=name,value=value) for name,value in env.items()]
    def application(executable):
        return Node(package='soarm_station_system',executable=executable,name=executable,namespace='/soarm_station',
            parameters=[{'config':config['_path']}],prefix=shlex.quote(str(root/'.venv/bin/python')),
            ros_arguments=['--log-level',log_level],respawn=respawn,respawn_delay=3.,output='screen')
    actions.extend(application(name) for name in ('lerobot_node','workflow_manager','telemetry_node','dashboard_node','static_transform_publishers'))
    for arm in config['arms']:
        if arm.get('urdf'):
            actions.append(Node(package='robot_state_publisher',executable='robot_state_publisher',name='robot_state_publisher',
                namespace='/soarm_station/arms/'+arm['id'],parameters=[{'robot_description':Path(arm['urdf']).read_text(),'frame_prefix':arm['id']+'/'}],
                remappings=[('joint_states','/soarm_station/arms/'+arm['id']+'/joint_states')],
                ros_arguments=['--log-level',log_level],output='screen'))
    if config.get('recording',{}).get('enabled',False):actions.append(application('recorder'))
    return actions


def generate_launch_description():
    root=default_root()
    return LaunchDescription([
        DeclareLaunchArgument('root',default_value=root,description='Full checkout; normally inferred from this launch file'),
        DeclareLaunchArgument('config',default_value=os.environ.get('SOARM_STATION_CONFIG') or PathJoinSubstitution([LaunchConfiguration('root'),'config','mock.yaml']),
            description='Deployment YAML; default is the tracked mock profile'),
        DeclareLaunchArgument('log_level',default_value='info',choices=['debug','info','warn','error','fatal']),
        DeclareLaunchArgument('respawn',default_value='false',choices=['true','false'],description='False for debugging; service wrapper explicitly enables it'),
        DeclareLaunchArgument('secrets_file',default_value='',description='Optional NAME=value file; default ROOT/local/secrets.env'),
        DeclareLaunchArgument('security_keystore',default_value='',description='Optional DDS keystore; default environment or ROOT/local/security'),
        OpaqueFunction(function=setup)])
