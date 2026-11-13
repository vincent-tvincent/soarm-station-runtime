#!/usr/bin/env bash
# Source this in Bash for ROS CLI/individual-node debugging. Starts no process.
_soarm_setup_main() {
    local soarm_root soarm_config soarm_ros soarm_nounset soarm_file soarm_status
    soarm_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    soarm_ros=${ROS_DISTRO:-lyrical}
    soarm_config=${1:-$soarm_root/config/mock.yaml}
    [[ "$soarm_config" = /* ]] || soarm_config="$soarm_root/$soarm_config"
    for soarm_file in "/opt/ros/$soarm_ros/setup.bash" "$soarm_root/ros2_ws/install/setup.bash" "$soarm_config"; do
        if [[ ! -f "$soarm_file" ]]; then echo "Missing $soarm_file; install/build ROS dependencies and run scripts/bootstrap first." >&2; return 1; fi
    done
    if [[ ! -f "$soarm_root/.venv/bin/activate" ]]; then echo 'Run scripts/bootstrap first.' >&2; return 1; fi
    soarm_nounset=false
    [[ $- == *u* ]] && soarm_nounset=true
    set +u
    soarm_status=0
    source "/opt/ros/$soarm_ros/setup.bash" || soarm_status=$?
    if [[ "$soarm_status" == 0 ]]; then source "$soarm_root/ros2_ws/install/setup.bash" || soarm_status=$?; fi
    if [[ "$soarm_status" == 0 ]]; then source "$soarm_root/.venv/bin/activate" || soarm_status=$?; fi
    [[ "$soarm_nounset" == true ]] && set -u
    [[ "$soarm_status" == 0 ]] || return "$soarm_status"
    export SOARM_ROOT="$soarm_root" SOARM_CONFIG="$soarm_config"
    export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$soarm_root/python"
    export TZ=UTC ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-71} ROS_AUTOMATIC_DISCOVERY_RANGE=${ROS_AUTOMATIC_DISCOVERY_RANGE:-LOCALHOST}
    export ROS_SECURITY_ENABLE=true ROS_SECURITY_STRATEGY=Enforce ROS_SECURITY_KEYSTORE=${ROS_SECURITY_KEYSTORE:-$soarm_root/local/security}
}
_soarm_setup_main "$@"
_soarm_setup_result=$?
unset -f _soarm_setup_main
return "$_soarm_setup_result" 2>/dev/null || exit "$_soarm_setup_result"
