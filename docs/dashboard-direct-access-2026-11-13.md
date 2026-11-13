# Direct dashboard access — November 13, 2026

The user requested removal of the operator-token login from their owned
station. The form is removed, and the browser connects automatically on load.
The current Tailscale and loopback access path requires neither a token nor a
cookie. Existing bearer clients remain compatible outside the trusted network;
`web.trusted_network_access: false` retains token-only API access if configured.
Actual socket peers are checked; forwarded headers are not identity. Browser
origin checks stay in the middleware without adding a user-facing step.

Source review commit: 0824108. Deployed code commit: b4b402c.
Only dashboard_node was replaced (49076 -> 49774). Service PID48080,
lerobot_node48107 and native worker48198 stayed unchanged. Native boot,
controller session and goal-write count were unchanged; permission stayed OFF,
no operation was active and feedback remained approximately100Hz. No motion
command or calibration/PID change was issued.

Verification:

- 49 frontend tests passed locally; all23 transport tests passed on the Pi.
- 18 focused access/route/startup tests plus61 subtests passed on the Pi.
- Requests without Authorization or cookies returned200 for status, resources,
  graph and all3camera JPEGs. A camera WebSocket negotiated only
  `soarm_station` and delivered a JPEG without a token.
- Actual live-browser reload connected automatically; the DOM had zero login
  forms and zero token fields. All3camera views and ROS graph populated.
  The browser console reported no errors. No Enable/Run/Hold button was used.
- Existing Tailscale Serve/Funnel was not configured; the station is accessed
  directly. This update did not change Tailscale, TLS, actuator limits or roles.

Machine evidence: ignored local/dashboard-direct-access-verification.json.
The outer project stores the browser proof in output/dashboard-ui/direct-access.jpg.
Historical deployment reports describe the earlier bearer-only behavior.
