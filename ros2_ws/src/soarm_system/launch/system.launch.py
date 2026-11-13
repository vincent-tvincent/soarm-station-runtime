import os
from pathlib import Path
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument,OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
def setup(context):
    from soarm.config import load
    path=LaunchConfiguration('config').perform(context);c=load(path)
    nodes=[Node(package='soarm_system',executable=n,name=n,namespace='/soarm',parameters=[{'config':path}],prefix=str(Path(os.environ['SOARM_ROOT'])/'.venv/bin/python'),respawn=True,respawn_delay=3.,output='screen')
        for n in ['lerobot_node','workflow_manager','telemetry_node','dashboard_node','static_transform_publishers']]
    for a in c['arms']:
        if a.get('urdf'):
            nodes.append(Node(package='robot_state_publisher',executable='robot_state_publisher',name='robot_state_publisher',
                namespace='/soarm/arms/'+a['id'],parameters=[{'robot_description':Path(a['urdf']).read_text(),'frame_prefix':a['id']+'/'}],
                remappings=[('joint_states','/soarm/arms/'+a['id']+'/joint_states')],output='screen'))
    if c.get('recording',{}).get('enabled',False):nodes.append(Node(package='soarm_system',executable='recorder',parameters=[{'config':path}],prefix=str(Path(os.environ['SOARM_ROOT'])/'.venv/bin/python'),respawn=True,respawn_delay=3.,output='screen'))
    return nodes
def generate_launch_description():
    return LaunchDescription([DeclareLaunchArgument('config',default_value=os.environ.get('SOARM_CONFIG','')),OpaqueFunction(function=setup)])
