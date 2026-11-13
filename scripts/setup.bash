#!/usr/bin/env bash
# Source this in Bash for ROS CLI/individual-node debugging. Starts no process.
_soarm_station_setup_main() {
    local soarm_station_root soarm_station_config soarm_station_ros soarm_station_nounset soarm_station_file soarm_station_status
    soarm_station_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    soarm_station_ros=${ROS_DISTRO:-lyrical}
    soarm_station_config=${1:-$soarm_station_root/config/mock.yaml}
    [[ "$soarm_station_config" = /* ]] || soarm_station_config="$soarm_station_root/$soarm_station_config"
    for soarm_station_file in "/opt/ros/$soarm_station_ros/setup.bash" "$soarm_station_root/ros2_ws/install/setup.bash" "$soarm_station_config"; do
        if [[ ! -f "$soarm_station_file" ]]; then echo "Missing $soarm_station_file; install/build ROS dependencies and run scripts/bootstrap first." >&2; return 1; fi
    done
    if [[ ! -f "$soarm_station_root/.venv/bin/activate" ]]; then echo 'Run scripts/bootstrap first.' >&2; return 1; fi
    soarm_station_nounset=false
    [[ $- == *u* ]] && soarm_station_nounset=true
    set +u
    soarm_station_status=0
    source "/opt/ros/$soarm_station_ros/setup.bash" || soarm_station_status=$?
    if [[ "$soarm_station_status" == 0 ]]; then source "$soarm_station_root/ros2_ws/install/setup.bash" || soarm_station_status=$?; fi
    if [[ "$soarm_station_status" == 0 ]]; then source "$soarm_station_root/.venv/bin/activate" || soarm_station_status=$?; fi
    [[ "$soarm_station_nounset" == true ]] && set -u
    [[ "$soarm_station_status" == 0 ]] || return "$soarm_station_status"
    export SOARM_STATION_ROOT="$soarm_station_root" SOARM_STATION_CONFIG="$soarm_station_config"
    export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$soarm_station_root/python"
    export TZ=UTC ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-71} ROS_AUTOMATIC_DISCOVERY_RANGE=${ROS_AUTOMATIC_DISCOVERY_RANGE:-LOCALHOST}
    export ROS_SECURITY_ENABLE=true ROS_SECURITY_STRATEGY=Enforce ROS_SECURITY_KEYSTORE=${ROS_SECURITY_KEYSTORE:-$soarm_station_root/local/security}
}
_soarm_station_setup_main "$@"
_soarm_station_setup_result=$?
unset -f _soarm_station_setup_main
return "$_soarm_station_setup_result" 2>/dev/null || exit "$_soarm_station_setup_result"
