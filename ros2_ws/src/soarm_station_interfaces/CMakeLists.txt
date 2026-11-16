cmake_minimum_required(VERSION 3.16)
project(soarm_station_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(trajectory_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
"msg/RequestContext.msg"
"msg/GripperTarget.msg"
"msg/DirectJointStep.msg"
"msg/ModelTaskStep.msg"
"msg/CartesianTarget.msg"
"msg/OperationConstraints.msg"
"msg/OperationSpec.msg"
"msg/InlineSequence.msg"
"msg/InputSpec.msg"
"msg/OperationInput.msg"
"msg/OperationFeedback.msg"
"msg/OperationStatus.msg"
"msg/CameraReference.msg"
"msg/Observation.msg"
"msg/WorkflowStatus.msg"
"msg/WorkflowDecision.msg"
"msg/TelemetrySample.msg"
"msg/UploadStatus.msg"
"msg/Event.msg"
"msg/HistoryRecord.msg"
"action/ExecuteOperation.action"
"action/ExecuteWorkflow.action"
"srv/SetControlPermission.srv"
"srv/SetTorque.srv"
"srv/Hold.srv"
"srv/RecordEvent.srv"
"srv/QueryHistory.srv"
DEPENDENCIES std_msgs sensor_msgs geometry_msgs trajectory_msgs builtin_interfaces)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
