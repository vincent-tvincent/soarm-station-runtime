/** Authenticated browser transport for the existing SOArm Station gateway.
 * No permission grant, operation reconnect, or command replay is automatic.
 * Positions are radians; gripper targets are explicit calibrated percentages.
 */

export class StationError extends Error {
  constructor(message, code = "TRANSPORT_ERROR", status = 0) {
    super(message);
    this.name = "StationError";
    this.code = code;
    this.status = status;
  }
}

const clone = (value) => JSON.parse(JSON.stringify(value));
const finite = (value, label) => {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new StationError(`${label} must be finite`, "INVALID_INPUT");
  }
  return value;
};
const noop = () => {};
const allowedKeys = (value, keys, label = "Payload") => {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new StationError(`${label} must be an object`, "INVALID_INPUT");
  for (const key of Object.keys(value))
    if (!keys.includes(key))
      throw new StationError(
        `${label} does not support field ${key}`,
        "INVALID_INPUT",
      );
};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};

export class StationClient {
  #token = "";

  constructor({
    onState = noop,
    onConnection = noop,
    onOperation = noop,
    onError = noop,
    onCamera = noop,
    env = {},
    pollMs = 100,
    operationPollMs = 50,
    manualHz = 100,
    heartbeatMs = 100,
    requestTimeoutMs = 5000,
    ackTimeoutMs = 2000,
  } = {}) {
    const g = globalThis;
    this.env = {
      fetch: (...args) => g.fetch(...args),
      WebSocket: g.WebSocket,
      setTimeout: (...args) => g.setTimeout(...args),
      clearTimeout: (id) => g.clearTimeout(id),
      now: () => g.performance.now(),
      uuid: () => g.crypto.randomUUID(),
      origin: g.location?.origin || "https://station.invalid",
      createObjectURL: (blob) => g.URL.createObjectURL(blob),
      revokeObjectURL: (url) => g.URL.revokeObjectURL(url),
      afterPaint: (callback) =>
        g.requestAnimationFrame
          ? g.requestAnimationFrame(callback)
          : g.setTimeout(callback, 0),
      decodeImage: async (url) => {
        if (!g.Image) return;
        const image = new g.Image();
        image.src = url;
        await image.decode();
      },
      ...env,
    };
    this.callbacks = { onState, onConnection, onOperation, onError, onCamera };
    this.timing = {
      pollMs,
      operationPollMs,
      heartbeatMs,
      requestTimeoutMs,
      ackTimeoutMs,
      manualMs: 1000 / manualHz,
    };
    if (!Number.isFinite(manualHz) || manualHz < 1 || manualHz > 120) {
      throw new StationError("manualHz must be within 1–120", "INVALID_INPUT");
    }
    this.state = {};
    this.connected = false;
    this.operatorSessionId = `browser-${this.env.uuid()}`;
    this._generation = 0;
    this._active = null;
    this._statusPromise = null;
    this._statusTimer = null;
    this._stateEmitTimer = null;
    this._lastStateEmit = -Infinity;
    this._requests = new Set();
    this._ownedArms = new Set();
    this._cameras = new Map();
  }

  get currentOperation() {
    return this._active ? this._publicOperation(this._active) : null;
  }
  get operation() {
    return this.currentOperation;
  }

  _emit(name, value) {
    try {
      this.callbacks[name](value);
    } catch (error) {
      if (name !== "onError") this._error(error);
    }
  }
  _error(error) {
    this._emit(
      "onError",
      error instanceof Error ? error : new StationError(String(error)),
    );
  }
  _connection(state, reason = "") {
    this._emit("onConnection", { state, reason });
  }
  _publicOperation(op) {
    return {
      state: op.state,
      type: op.type,
      mode: op.mode,
      armId: op.armId,
      requestId: op.requestId || null,
      operationId: op.operationId || null,
      ready:
        Boolean(op.grant) &&
        this.env.now() < op.grant.localDeadline &&
        !op.ending,
      transport: op.transport,
      lastAcceptedRevision: op.acceptedRevision,
      lastAcceptedInputId: op.acceptedInputId || "",
      pendingInputId: op.pending?.inputId || null,
    };
  }
  _operationEvent(op, event, extra = {}) {
    this._emit("onOperation", {
      ...this._publicOperation(op),
      event,
      detail: "",
      result: null,
      ...extra,
    });
  }
  _requireConnected() {
    if (!this.connected || !this.#token)
      throw new StationError(
        "Connect before using the station",
        "DISCONNECTED",
      );
  }
  _arm(armId) {
    if (
      typeof armId !== "string" ||
      !armId ||
      !Object.hasOwn(this.state.arms || {}, armId)
    ) {
      throw new StationError("Select a known arm explicitly", "UNKNOWN_ARM");
    }
    const arm = this.state.arms[armId];
    if (!arm.runtime_boot_id || !arm.controller_session) {
      throw new StationError("Waiting for arm session identity", "NOT_READY");
    }
    return arm;
  }
  _url(path, websocket = false) {
    if (
      typeof path !== "string" ||
      !path.startsWith(websocket ? "/ws/" : "/api/") ||
      path.includes("\\")
    ) {
      throw new StationError(
        "Only station-relative API paths are allowed",
        "INVALID_PATH",
      );
    }
    const base = new URL(this.env.origin);
    const url = new URL(path, base);
    if (url.origin !== base.origin)
      throw new StationError(
        "Cross-origin requests are not allowed",
        "INVALID_PATH",
      );
    if (websocket) url.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    return url.href;
  }

  async _request(path, body, token = this.#token) {
    if (!token)
      throw new StationError("Authentication required", "DISCONNECTED");
    const url = this._url(path);
    const encoded = body === undefined ? undefined : JSON.stringify(body);
    if (encoded && new TextEncoder().encode(encoded).length > 262144) {
      throw new StationError(
        "Request exceeds the gateway size limit",
        "INVALID_INPUT",
      );
    }
    const controller = new AbortController();
    const entry = { controller, readOnly: body === undefined };
    this._requests.add(entry);
    let timedOut = false;
    const timer = this.env.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.timing.requestTimeoutMs);
    try {
      const response = await this.env.fetch(url, {
        method: body === undefined ? "GET" : "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/json",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: encoded,
        signal: controller.signal,
        cache: "no-store",
        redirect: "error",
        credentials: "same-origin",
      });
      if (!response.ok) {
        const detail = (await response.text())
          .replaceAll(token, "[redacted]")
          .slice(0, 512);
        throw new StationError(
          detail || `HTTP ${response.status}`,
          response.status === 401 || response.status === 403
            ? "AUTH_REQUIRED"
            : "HTTP_ERROR",
          response.status,
        );
      }
      return await response.json();
    } catch (error) {
      if (timedOut)
        throw new StationError("Station request timed out", "TIMEOUT");
      throw error;
    } finally {
      this.env.clearTimeout(timer);
      this._requests.delete(entry);
    }
  }
  request(path, body) {
    this._requireConnected();
    return this._request(path, body);
  }

  async connect(token) {
    if (
      typeof token !== "string" ||
      token.trim().length < 24 ||
      /[\s,]/.test(token.trim())
    ) {
      throw new StationError(
        "Enter the configured operator token",
        "INVALID_TOKEN",
      );
    }
    if (this.#token) await this.disconnect();
    const generation = ++this._generation;
    this.#token = token.trim();
    this.operatorSessionId = `browser-${this.env.uuid()}`;
    this._connection("connecting");
    try {
      const state = await this._request("/api/status");
      if (generation !== this._generation)
        throw new StationError("Connection canceled", "CANCELED");
      this.connected = true;
      this._applyState(state);
      this._connection("connected");
      this._scheduleStatus();
      return state;
    } catch (error) {
      if (generation === this._generation) {
        this.#token = "";
        this.connected = false;
        this._connection("disconnected", error.message);
      }
      throw error;
    }
  }
  async disconnect() {
    const token = this.#token;
    const arms = new Set(this._ownedArms);
    if (this._active) arms.add(this._active.armId);
    ++this._generation;
    this.connected = false;
    this.#token = "";
    this.env.clearTimeout(this._statusTimer);
    this._statusTimer = null;
    this.env.clearTimeout(this._stateEmitTimer);
    this._stateEmitTimer = null;
    for (const entry of this._requests)
      if (entry.readOnly) entry.controller.abort();
    this._stopLocalOperation("disconnected", "Connection closed");
    for (const id of [...this._cameras.keys()]) this.closeCamera(id);
    this._ownedArms.clear();
    this._connection("disconnected");
    if (token) {
      const results = await Promise.allSettled(
        [...arms].map((arm_id) =>
          this._request("/api/hold", { arm_id }, token),
        ),
      );
      for (const result of results)
        if (result.status === "rejected")
          this._error(
            new StationError(
              "Disconnected; hold could not be confirmed",
              "HOLD_UNCONFIRMED",
            ),
          );
    }
  }
  _scheduleStatus() {
    if (!this.connected || this._statusTimer !== null) return;
    this._statusTimer = this.env.setTimeout(async () => {
      this._statusTimer = null;
      try {
        await this.refresh();
      } catch (error) {
        if (this.connected) {
          this._connection("degraded", error.message);
          if (error.code === "AUTH_REQUIRED") {
            this._error(error);
            await this.disconnect();
          }
        }
      } finally {
        this._scheduleStatus();
      }
    }, this.timing.pollMs);
  }
  refresh() {
    this._requireConnected();
    if (this._statusPromise) return this._statusPromise;
    const generation = this._generation;
    const promise = this._request("/api/status")
      .then((state) => {
        if (generation === this._generation && this.connected) {
          this._applyState(state);
          this._connection("connected");
        }
        return state;
      })
      .finally(() => {
        if (this._statusPromise === promise) this._statusPromise = null;
      });
    this._statusPromise = promise;
    return promise;
  }
  _applyState(state) {
    this.state = state;
    for (const id of this._ownedArms)
      if (state.arms?.[id]?.permission === false) this._ownedArms.delete(id);
    const op = this._active;
    const arm = op && state.arms?.[op.armId];
    if (
      arm &&
      (arm.runtime_boot_id !== op.boot || arm.controller_session !== op.session)
    ) {
      this._ownedArms.delete(op.armId);
      this._stopLocalOperation(
        "interrupted",
        "Runtime/session changed; start a new operation explicitly",
      );
    }
    this._notifyState();
  }
  _notifyState(immediate = false) {
    const wait = this.timing.pollMs - (this.env.now() - this._lastStateEmit);
    if (immediate || wait <= 0) {
      this.env.clearTimeout(this._stateEmitTimer);
      this._stateEmitTimer = null;
      this._lastStateEmit = this.env.now();
      this._emit("onState", this.state);
    } else if (this._stateEmitTimer === null) {
      this._stateEmitTimer = this.env.setTimeout(() => {
        this._stateEmitTimer = null;
        if (this.connected) this._notifyState(true);
      }, wait);
    }
  }

  async enable(armId) {
    this._requireConnected();
    const arm = this._arm(armId),
      token = this.#token,
      generation = this._generation;
    const result = await this._request("/api/permission", {
      arm_id: armId,
      controller_session: arm.controller_session,
      operator_session_id: this.operatorSessionId,
      enable: true,
    });
    if (generation !== this._generation) {
      await this._request("/api/hold", { arm_id: armId }, token).catch(
        () => {},
      );
      throw new StationError(
        "Permission response belongs to a closed connection",
        "CANCELED",
      );
    }
    if (!result.accepted)
      throw new StationError(
        result.reason || "Control permission rejected",
        "PERMISSION_REJECTED",
      );
    this._ownedArms.add(armId);
    this.state.arms[armId] = { ...arm, permission: Boolean(result.permission) };
    this._notifyState(true);
    return result;
  }
  async hold(armId) {
    this._requireConnected();
    this._arm(armId);
    if (this._active?.armId === armId)
      this._stopLocalOperation("stopping", "Hold requested");
    const result = await this._request("/api/hold", { arm_id: armId });
    if (!result.success)
      throw new StationError(
        result.message || "Hold not confirmed",
        "HOLD_UNCONFIRMED",
      );
    this._ownedArms.delete(armId);
    if (this.state.arms?.[armId]) this.state.arms[armId].permission = false;
    this._notifyState(true);
    return result;
  }

  _positions(positions, armId) {
    if (
      !positions ||
      typeof positions !== "object" ||
      Array.isArray(positions) ||
      !Object.keys(positions).length
    ) {
      throw new StationError(
        "A full named joint target is required",
        "INVALID_INPUT",
      );
    }
    const expected = this.state.joints?.[armId]?.name?.map((name) =>
      name.split("/").pop(),
    );
    if (
      expected &&
      (expected.length !== Object.keys(positions).length ||
        expected.some((name) => !Object.hasOwn(positions, name)))
    ) {
      throw new StationError(
        "Target must contain the full measured joint-name set",
        "INVALID_INPUT",
      );
    }
    return Object.fromEntries(
      Object.entries(positions).map(([name, value]) => [
        name,
        finite(value, name),
      ]),
    );
  }
  _gripper(value) {
    finite(value, "Gripper percentage");
    if (value < 0 || value > 100)
      throw new StationError(
        "Gripper percentage must be within 0–100",
        "INVALID_INPUT",
      );
    return value;
  }
  // Preview the exact semantic checks used by submit/updateStream; no I/O or grant.
  validatePayload(armId, type, mode, payload) {
    type = String(type).toUpperCase();
    mode = String(mode).toUpperCase();
    if (
      !["DIRECT", "TRAJECTORY", "MODEL"].includes(type) ||
      !["INLINE", "STREAM"].includes(mode)
    ) {
      throw new StationError(
        "Unknown operation type or delivery mode",
        "INVALID_INPUT",
      );
    }
    return type === "DIRECT" && mode === "STREAM"
      ? this._directTarget(armId, payload)
      : this._payload(type, payload, armId, mode);
  }

  _directTarget(armId, target) {
    allowedKeys(target, ["positions", "gripper", "gripper_unit"]);
    if (
      target.gripper_unit !== undefined &&
      target.gripper_unit !== "percent"
    ) {
      throw new StationError("Gripper units must be percent", "INVALID_INPUT");
    }
    return {
      positions: this._positions(target.positions, armId),
      gripper: this._gripper(target.gripper),
      gripper_unit: "percent",
    };
  }

  _payload(type, payload, armId, mode = "INLINE") {
    if (type === "MODEL") {
      allowedKeys(
        payload,
        mode === "STREAM"
          ? ["instruction"]
          : ["model_steps", "instruction", "duration_s"],
      );
      if (
        payload.model_steps &&
        (Object.hasOwn(payload, "instruction") ||
          Object.hasOwn(payload, "duration_s"))
      ) {
        throw new StationError(
          "Choose model_steps or a single instruction, not both",
          "INVALID_INPUT",
        );
      }
      const instruction = (value) => {
        if (
          typeof value !== "string" ||
          new TextEncoder().encode(value).length > 512
        ) {
          throw new StationError(
            "Instruction must contain at most 512 UTF-8 bytes",
            "INVALID_INPUT",
          );
        }
        return value;
      };
      if (mode === "STREAM")
        return { instruction: instruction(payload.instruction ?? "") };
      const steps = payload.model_steps || [
        {
          instruction: payload.instruction ?? "",
          duration_s: payload.duration_s,
        },
      ];
      if (!Array.isArray(steps) || !steps.length || steps.length > 16)
        throw new StationError("Expected 1–16 model steps", "INVALID_INPUT");
      return {
        model_steps: steps.map((step) => {
          allowedKeys(step, ["instruction", "duration_s"], "Model step");
          const result = { instruction: instruction(step.instruction ?? "") };
          if (step.duration_s != null && step.duration_s !== 0) {
            if (finite(step.duration_s, "Step duration") <= 0)
              throw new StationError(
                "Step duration must be positive",
                "INVALID_INPUT",
              );
            result.duration_s = step.duration_s;
          }
          return result;
        }),
      };
    }
    if (payload.cartesian || payload.payload_kind === "CARTESIAN_TARGET")
      return this._cartesianPayload(type, mode, payload, armId);
    allowedKeys(payload, ["points"]);
    const points = payload.points;
    const limit = type === "DIRECT" ? 16 : 256;
    if (!Array.isArray(points) || !points.length || points.length > limit)
      throw new StationError(`Expected 1–${limit} points`, "INVALID_INPUT");
    let previous = 0;
    return {
      points: points.map((point) => {
        allowedKeys(
          point,
          [
            "time_s",
            "positions",
            "gripper",
            "gripper_unit",
            "input_id",
            "velocities",
            "accelerations",
            "effort",
          ],
          "Point",
        );
        const time = finite(point.time_s, "Point time");
        if (time <= previous)
          throw new StationError(
            "Point times must be positive and increasing",
            "INVALID_INPUT",
          );
        previous = time;
        if (point.velocities || point.accelerations || point.effort)
          throw new StationError(
            "Only position/time trajectories are supported",
            "UNSUPPORTED_CAPABILITY",
          );
        const result = {
          time_s: time,
          positions: this._positions(point.positions, armId),
        };
        if (
          point.gripper_unit !== undefined &&
          point.gripper_unit !== "percent"
        )
          throw new StationError(
            "Gripper units must be percent",
            "INVALID_INPUT",
          );
        if (type === "DIRECT") result.gripper = this._gripper(point.gripper);
        else if (point.gripper !== undefined)
          throw new StationError(
            "Use DIRECT for explicit gripper targets; this trajectory gateway carries body joints only",
            "UNSUPPORTED_CAPABILITY",
          );
        return result;
      }),
    };
  }
  _cartesianPayload(type, mode, payload, armId) {
    if (type !== "TRAJECTORY" || mode !== "INLINE") {
      throw new StationError(
        "Cartesian input is supported only as an INLINE TRAJECTORY",
        "UNSUPPORTED_CAPABILITY",
      );
    }
    allowedKeys(payload, ["cartesian", "payload_kind"]);
    if (
      payload.payload_kind !== undefined &&
      payload.payload_kind !== "CARTESIAN_TARGET"
    ) {
      throw new StationError(
        "Cartesian payload kind must be CARTESIAN_TARGET",
        "INVALID_INPUT",
      );
    }
    const arms = this.state.resources?.arms || [];
    const resource = Array.isArray(arms)
      ? arms.find((arm) => arm.id === armId)
      : arms[armId];
    if (resource?.capabilities?.cartesian_endpoints !== true) {
      throw new StationError(
        "Cartesian endpoints are not enabled for this arm",
        "UNSUPPORTED_CAPABILITY",
      );
    }
    if (
      !Array.isArray(payload.cartesian) ||
      payload.cartesian.length < 1 ||
      payload.cartesian.length > 16
    ) {
      throw new StationError(
        "Expected 1–16 Cartesian endpoints",
        "INVALID_INPUT",
      );
    }
    const vector = (value, count, label) => {
      if (!Array.isArray(value) || value.length !== count)
        throw new StationError(
          `${label} requires ${count} finite values`,
          "INVALID_INPUT",
        );
      return value.map((item) => finite(item, label));
    };
    return {
      payload_kind: "CARTESIAN_TARGET",
      cartesian: payload.cartesian.map((target) => {
        allowedKeys(
          target,
          [
            "frame_id",
            "position",
            "orientation_mode",
            "orientation",
            "duration_s",
          ],
          "Cartesian endpoint",
        );
        if (
          typeof target.frame_id !== "string" ||
          !target.frame_id ||
          new TextEncoder().encode(target.frame_id).length > 128
        ) {
          throw new StationError(
            "Cartesian endpoint requires a configured frame ID",
            "INVALID_INPUT",
          );
        }
        if (resource.base_frame && target.frame_id !== resource.base_frame) {
          throw new StationError(
            "Cartesian frame must match the configured base frame",
            "INVALID_INPUT",
          );
        }
        const orientationMode = target.orientation_mode || "POSITION_ONLY";
        if (!["POSITION_ONLY", "STRICT_POSE"].includes(orientationMode)) {
          throw new StationError(
            "Unsupported Cartesian orientation mode",
            "UNSUPPORTED_CAPABILITY",
          );
        }
        const position = vector(target.position, 3, "Cartesian position");
        const orientation = vector(
          target.orientation ??
            (orientationMode === "POSITION_ONLY" ? [0, 0, 0, 1] : null),
          4,
          "Quaternion",
        );
        if (
          orientationMode === "STRICT_POSE" &&
          Math.abs(Math.hypot(...orientation) - 1) > 0.001
        ) {
          throw new StationError(
            "Strict-pose quaternion must be normalized",
            "INVALID_INPUT",
          );
        }
        const duration = finite(
          target.duration_s,
          "Cartesian endpoint duration",
        );
        if (duration < 0.001 || duration * 1000 > 0xffffffff) {
          throw new StationError(
            "Cartesian duration must be positive and representable in milliseconds",
            "INVALID_INPUT",
          );
        }
        return {
          frame_id: target.frame_id,
          position,
          orientation_mode: orientationMode,
          orientation,
          duration_s: duration,
        };
      }),
    };
  }
  _reserve(armId, type, mode, transport) {
    this._requireConnected();
    const arm = this._arm(armId);
    if (this._active)
      throw new StationError(
        "End the current operation before starting another",
        "BUSY",
      );
    const op = {
      armId,
      type,
      mode,
      transport,
      boot: arm.runtime_boot_id,
      session: arm.controller_session,
      principal: this.operatorSessionId,
      state: "submitting",
      grant: null,
      epoch: null,
      operationId: "",
      requestId: "",
      sequence: 0,
      nextRevision: 0,
      acceptedRevision: 0,
      windows: new Map(),
      generation: this._generation,
      timers: new Set(),
      pending: null,
      ending: false,
      lastPost: -Infinity,
      postBusy: false,
      finished: deferred(),
    };
    // The completion promise is optional for clients; prevent unobserved rejection.
    op.finished.promise.catch(noop);
    this._active = op;
    this._operationEvent(op, "submitting");
    return op;
  }
  _later(op, callback, ms) {
    const id = this.env.setTimeout(() => {
      op.timers.delete(id);
      if (this._active === op) callback();
    }, ms);
    op.timers.add(id);
    return id;
  }

  async submit(armId, type, mode, payload = {}, profileId = "", options = {}) {
    type = String(type).toUpperCase();
    mode = String(mode).toUpperCase();
    if (
      !["MODEL", "TRAJECTORY", "DIRECT"].includes(type) ||
      !["INLINE", "STREAM"].includes(mode)
    ) {
      throw new StationError(
        "Unknown operation type or delivery mode",
        "INVALID_INPUT",
      );
    }
    if (type === "DIRECT" && mode === "STREAM") {
      allowedKeys(payload, ["positions", "gripper", "gripper_unit"]);
      const target = Object.keys(payload).length
        ? this._directTarget(armId, payload)
        : null;
      const result = await this.startManual(armId, options);
      if (target) this.setManualTarget(target);
      return result;
    }
    if (type === "MODEL" && (!profileId || typeof profileId !== "string"))
      throw new StationError(
        "Select a registered policy profile",
        "INVALID_INPUT",
      );
    const normalized =
      mode === "INLINE" ? this._payload(type, payload, armId) : {};
    const initialPayload =
      mode === "STREAM" && Object.keys(payload).length
        ? this._payload(type, payload, armId, "STREAM")
        : null;
    if (
      options.duration_s != null &&
      options.duration_s !== 0 &&
      finite(options.duration_s, "Operation duration") <= 0
    ) {
      throw new StationError(
        "Operation duration must be positive or omitted",
        "INVALID_INPUT",
      );
    }
    const op = this._reserve(armId, type, mode, "http"),
      token = this.#token;
    op.initialPayload = initialPayload;
    const body = {
      arm_id: armId,
      runtime_boot_id: op.boot,
      controller_session: op.session,
      operator_session_id: op.principal,
      request_id: this.env.uuid(),
      operation_type: type,
      mode,
      ...(profileId ? { profile_id: profileId } : {}),
      ...normalized,
      ...(options.duration_s ? { duration_s: options.duration_s } : {}),
    };
    try {
      const response = await this._request("/api/operate", body);
      if (this._active !== op || op.generation !== this._generation) {
        await this._request("/api/hold", { arm_id: armId }, token).catch(noop);
        throw new StationError(
          "Operation response belongs to a closed session",
          "CANCELED",
        );
      }
      if (!response.request_id)
        throw new StationError(
          "Gateway returned no operation request ID",
          "INVALID_RESPONSE",
        );
      op.requestId = response.request_id;
      op.state = "preparing";
      this._operationEvent(op, "accepted");
      this._pollOperation(op);
      if (mode === "STREAM") this._httpTick(op);
      return this._publicOperation(op);
    } catch (error) {
      if (this._active === op) {
        const definitiveRejection =
          error.code === "HTTP_ERROR" &&
          error.status >= 400 &&
          error.status < 500;
        if (definitiveRejection)
          this._stopLocalOperation("error", error.message);
        else this._failOperation(op, error); // Accepted action + lost response is uncertain, never replay it.
      }
      throw error;
    }
  }

  async _pollOperation(op) {
    const started = this.env.now();
    try {
      const value = await this._request(
        `/api/operations/${encodeURIComponent(op.requestId)}`,
      );
      if (this._active !== op) return;
      if (value.done) {
        this._finishOperation(op, value.result);
        return;
      }
      this._acceptFeedback(op, value.feedback || {}, started);
    } catch (error) {
      if (this._active === op) {
        this._failOperation(op, error);
        return;
      }
    }
    if (this._active === op)
      this._later(
        op,
        () => this._pollOperation(op),
        this.timing.operationPollMs,
      );
  }
  _acceptFeedback(op, feedback, requestStarted = this.env.now()) {
    if (this._active !== op || !feedback.operation_id) return;
    if (
      feedback.arm_id !== op.armId ||
      feedback.runtime_boot_id !== op.boot ||
      feedback.controller_session !== op.session ||
      (op.operationId && feedback.operation_id !== op.operationId) ||
      (op.epoch !== null && feedback.operation_epoch !== op.epoch)
    ) {
      this._failOperation(
        op,
        new StationError(
          "Operation session changed; stale input discarded",
          "SESSION_CHANGED",
        ),
      );
      return;
    }
    op.operationId = feedback.operation_id;
    op.epoch = feedback.operation_epoch;
    op.sequence = Math.max(op.sequence, Number(feedback.last_sequence) || 0);
    op.acceptedRevision = Number(feedback.last_accepted_semantic_revision) || 0;
    op.acceptedInputId = feedback.last_accepted_input_id || "";
    if (
      feedback.stream_ready &&
      ["READY", "ACTIVE"].includes(feedback.phase) &&
      feedback.window_id
    ) {
      // These are local receipt/round-trip bounds only. The Pi remains the expiry authority.
      const deadline =
        requestStarted +
        Math.max(
          0,
          Math.min(250, Number(feedback.window_valid_for_ms) || 0) - 25,
        );
      op.windows.set(
        feedback.window_id,
        Math.min(op.windows.get(feedback.window_id) ?? Infinity, deadline),
      );
      for (const [id, expiry] of op.windows)
        if (expiry < this.env.now() - 1000) op.windows.delete(id);
      op.grant = {
        ...feedback,
        localDeadline: op.windows.get(feedback.window_id),
      };
      if (!op.ending) op.state = feedback.phase.toLowerCase();
    } else {
      op.grant = null;
      if (!op.ending) op.state = feedback.phase?.toLowerCase() || "preparing";
    }
    if (op.pickupIds?.has(op.acceptedInputId)) op.pickedUp = true;
    const pending = op.pending;
    if (
      pending &&
      op.acceptedRevision === pending.revision &&
      op.acceptedInputId === pending.inputId
    ) {
      op.pending = null;
      pending.resolve({
        inputId: pending.inputId,
        semanticRevision: pending.revision,
      });
      this._operationEvent(op, "input-accepted");
    } else if (
      pending &&
      (op.acceptedRevision > pending.revision ||
        (pending.sent &&
          feedback.reason &&
          feedback.reason !== pending.initialReason))
    ) {
      op.pending = null;
      const error = new StationError(
        feedback.reason || "Input superseded by a newer revision",
        "INPUT_REJECTED",
      );
      pending.reject(error);
      this._error(error);
      this._operationEvent(op, "input-rejected", {
        detail: `${error.message}. The previous accepted intent may remain active.`,
      });
    }
    op.lastReason = feedback.reason || "";
    const { window_id, window_valid_for_ms, ...publicFeedback } = feedback;
    this._operationEvent(op, "feedback", { feedback: publicFeedback });
    if (op.initialPayload && op.grant && !op.ending && this._active === op) {
      const payload = op.initialPayload;
      op.initialPayload = null;
      this.updateStream(payload).catch(noop);
    }
  }
  _frame(op, kind, extra = {}) {
    const grant = op.grant;
    if (!grant || this.env.now() >= grant.localDeadline) return null;
    return {
      arm_id: op.armId,
      operator_session_id: op.principal,
      frame_kind: kind,
      runtime_boot_id: op.boot,
      controller_session: op.session,
      operation_id: op.operationId,
      operation_epoch: op.epoch,
      window_id: grant.window_id,
      sequence: ++op.sequence,
      payload_kind: "NONE",
      ...extra,
    };
  }
  updateStream(payload) {
    const op = this._active;
    if (
      op?.type === "DIRECT" &&
      op.mode === "STREAM" &&
      op.transport === "websocket" &&
      !op.ending
    ) {
      this.setManualTarget(payload);
      return Promise.resolve({ staged: true, pickupPending: !op.pickedUp });
    }
    if (
      !op ||
      op.mode !== "STREAM" ||
      op.transport !== "http" ||
      op.ending ||
      !op.grant
    ) {
      throw new StationError(
        "Wait for an open MODEL or TRAJECTORY stream to become ready",
        "NOT_READY",
      );
    }
    if (op.pending)
      throw new StationError(
        "Wait for the pending sparse input acknowledgment",
        "INPUT_PENDING",
      );
    const body = this._payload(op.type, payload, op.armId, "STREAM");
    const revision = Math.max(op.nextRevision, op.acceptedRevision) + 1;
    if (!Number.isSafeInteger(revision))
      throw new StationError(
        "Start a new operation before revision exhaustion",
        "INVALID_INPUT",
      );
    op.nextRevision = revision;
    const pending = {
      ...deferred(),
      body,
      revision,
      inputId: this.env.uuid(),
      deadline: this.env.now() + this.timing.ackTimeoutMs,
      lastSent: -Infinity,
      initialReason: op.lastReason || "",
      sent: false,
    };
    op.pending = pending;
    this._operationEvent(op, "input-pending");
    return pending.promise;
  }
  async _httpTick(op) {
    if (this._active !== op || op.ending) return;
    const now = this.env.now();
    if (op.pending && now >= op.pending.deadline) {
      this._failOperation(
        op,
        new StationError(
          "Input acknowledgment timed out; hold requested instead of retaining an uncertain task",
          "ACK_TIMEOUT",
        ),
      );
      return;
    }
    if (!op.postBusy && op.grant) {
      let frame;
      const pending = op.pending;
      if (pending && now - pending.lastSent >= 150) {
        frame = this._frame(op, "COMMAND", {
          payload_kind: op.type === "MODEL" ? "MODEL_TASK" : "JOINT_TRAJECTORY",
          input_id: pending.inputId,
          semantic_revision: pending.revision,
          body: clone(pending.body),
        });
        if (frame) {
          pending.lastSent = now;
          pending.sent = true;
        }
      } else if (now - op.lastPost >= this.timing.heartbeatMs)
        frame = this._frame(op, "HEARTBEAT");
      if (frame) {
        op.postBusy = true;
        op.lastPost = now;
        try {
          await this._request("/api/stream", frame);
        } catch (error) {
          if (this._active === op) this._failOperation(op, error);
        } finally {
          op.postBusy = false;
        }
      }
    }
    if (this._active === op) this._later(op, () => this._httpTick(op), 25);
  }

  _measuredTarget(armId) {
    const joints = this.state.joints?.[armId],
      arm = this._arm(armId);
    if (
      !arm.feedback_valid ||
      !joints?.name?.length ||
      joints.name.length !== joints.position?.length
    ) {
      throw new StationError(
        "Fresh measured joints are required for manual pickup",
        "NOT_READY",
      );
    }
    return {
      positions: this._positions(
        Object.fromEntries(
          joints.name.map((name, i) => [
            name.split("/").pop(),
            joints.position[i],
          ]),
        ),
        armId,
      ),
      gripper: this._gripper(arm.gripper_percent),
      gripper_unit: "percent",
    };
  }
  async startManual(armId, options = {}) {
    if (
      options.duration_s != null &&
      options.duration_s !== 0 &&
      finite(options.duration_s, "Operation duration") <= 0
    ) {
      throw new StationError(
        "Operation duration must be positive or omitted",
        "INVALID_INPUT",
      );
    }
    const seed = this._measuredTarget(armId);
    const op = this._reserve(armId, "DIRECT", "STREAM", "websocket");
    op.target = seed;
    op.targetEdited = false;
    op.pickupIds = new Set();
    op.pickedUp = false;
    const opened = deferred();
    const ws = new this.env.WebSocket(this._url("/ws/control", true), [
      "soarm_station",
      this.#token,
    ]);
    op.ws = ws;
    ws.onopen = () => {
      if (this._active !== op) {
        ws.close();
        return;
      }
      const goal = {
        arm_id: armId,
        runtime_boot_id: op.boot,
        controller_session: op.session,
        operator_session_id: op.principal,
        operation_type: "DIRECT",
        mode: "STREAM",
      };
      if (options.duration_s)
        goal.duration_s = finite(options.duration_s, "Operation duration");
      ws.send(JSON.stringify(goal));
      op.state = "preparing";
      this._operationEvent(op, "accepted");
      opened.resolve(this._publicOperation(op));
      this._manualTick(op);
    };
    ws.onmessage = (event) => {
      if (this._active !== op) return;
      try {
        const data = JSON.parse(event.data);
        if (data.type === "grant") this._acceptFeedback(op, data.grant);
        else if (data.type === "state") {
          if (data.state) this.state.arms[armId] = data.state;
          if (data.joints) this.state.joints[armId] = data.joints;
          this._applyState(this.state);
        } else if (data.type === "result")
          this._finishOperation(op, data.result);
        else if (data.type === "error")
          this._failOperation(
            op,
            new StationError(
              data.reason || "Control rejected",
              "CONTROL_ERROR",
            ),
          );
      } catch (error) {
        this._failOperation(op, error);
      }
    };
    ws.onerror = () => {
      opened.reject(new StationError("Control socket failed", "SOCKET_ERROR"));
      this._failOperation(
        op,
        new StationError(
          "Control socket failed; no automatic reconnect",
          "SOCKET_ERROR",
        ),
      );
    };
    ws.onclose = () => {
      opened.reject(
        new StationError(
          "Control socket closed before opening",
          "SOCKET_CLOSED",
        ),
      );
      if (this._active === op)
        this._stopLocalOperation(
          "stopped",
          "Control socket closed; permission must be checked before a new operation",
        );
    };
    this._later(
      op,
      () => {
        if (ws.readyState === 0) {
          const error = new StationError(
            "Control socket connection timed out",
            "TIMEOUT",
          );
          opened.reject(error);
          this._failOperation(op, error);
        }
      },
      this.timing.requestTimeoutMs,
    );
    return opened.promise;
  }
  setManualTarget(target) {
    const op = this._active;
    // Controls can be edited before opening manual mode; this does not select an arm or send data.
    if (!op || op.transport !== "websocket" || op.ending) return false;
    // User-held browser intent, not a cached remote-leader sample.
    op.target = this._directTarget(op.armId, target);
    op.targetEdited = true;
    return true;
  }
  _manualTick(op) {
    if (this._active !== op || op.ending) return;
    if (op.ws.readyState === 1 && op.ws.bufferedAmount === 0 && op.grant) {
      try {
        let target = op.target;
        if (!op.pickedUp) {
          target = this._measuredTarget(op.armId);
          if (!op.targetEdited) op.target = clone(target);
        }
        const id = this.env.uuid();
        const frame = this._frame(op, "COMMAND", {
          payload_kind: "DIRECT_JOINT_TARGET",
          input_id: id,
          body: clone(target),
        });
        if (frame) {
          if (!op.pickedUp) {
            op.pickupIds.add(id);
            if (op.pickupIds.size > 32)
              op.pickupIds.delete(op.pickupIds.values().next().value);
          }
          op.ws.send(JSON.stringify(frame));
        }
      } catch (error) {
        this._failOperation(op, error);
        return;
      }
    }
    // Schedule from now: a delayed tab never emits a catch-up burst.
    this._later(op, () => this._manualTick(op), this.timing.manualMs);
  }

  async endOperation() {
    const op = this._active;
    if (!op) return null;
    if (op.ending) return op.finished.promise;
    if (op.mode === "INLINE") {
      await this.hold(op.armId);
      return null;
    }
    op.ending = true;
    op.state = "ending";
    if (op.pending) {
      op.pending.reject(
        new StationError(
          "Stream ended before input acknowledgment",
          "CANCELED",
        ),
      );
      op.pending = null;
    }
    this._operationEvent(op, "ending");
    const frame = this._frame(op, "END");
    if (
      !frame ||
      (op.transport === "websocket" &&
        (op.ws.readyState !== 1 || op.ws.bufferedAmount > 0))
    ) {
      await this.hold(op.armId);
      return null;
    }
    try {
      if (op.transport === "websocket") op.ws.send(JSON.stringify(frame));
      else await this._request("/api/stream", frame);
    } catch (error) {
      this._failOperation(op, error);
      throw error;
    }
    this._later(
      op,
      () =>
        this._failOperation(
          op,
          new StationError(
            "End was not confirmed; hold requested",
            "END_TIMEOUT",
          ),
        ),
      2000,
    );
    return op.finished.promise;
  }
  _finishOperation(op, result) {
    if (this._active !== op) return;
    op.state = result?.result_code === "COMPLETED" ? "done" : "ended";
    if (op.mode === "STREAM" || result?.result_code !== "COMPLETED")
      this._ownedArms.delete(op.armId);
    this._operationEvent(op, "result", {
      result,
      detail: result?.reason || "",
    });
    op.finished.resolve(result);
    this._clearOperation(op);
    // Successful INLINE deliberately does not send hold or revoke its retained grant.
  }
  _clearOperation(op) {
    if (this._active === op) this._active = null;
    for (const timer of op.timers) this.env.clearTimeout(timer);
    op.timers.clear();
    op.grant = null;
    if (op.pending) {
      op.pending.reject(new StationError("Operation ended", "CANCELED"));
      op.pending = null;
    }
    if (op.ws && op.ws.readyState < 2) op.ws.close();
  }
  _stopLocalOperation(state, detail) {
    const op = this._active;
    if (!op) return;
    if (state === "stopped" && op.transport === "websocket")
      this._ownedArms.delete(op.armId);
    op.state = "closed";
    this._operationEvent(op, "closed", { detail, terminalReason: state });
    op.finished.resolve(null);
    this._clearOperation(op);
  }
  _failOperation(op, error) {
    if (this._active !== op) return;
    op.state = "error";
    this._operationEvent(op, "error", { detail: error.message });
    this._error(error);
    if (op.pending) {
      op.pending.reject(error);
      op.pending = null;
    }
    op.finished.reject(error);
    this._clearOperation(op);
    if (this.connected)
      this._request("/api/hold", { arm_id: op.armId })
        .then((result) => {
          if (!result.success)
            throw new StationError(
              result.message || "Hold rejected",
              "HOLD_UNCONFIRMED",
            );
          this._ownedArms.delete(op.armId);
        })
        .catch(() =>
          this._error(
            new StationError("Hold could not be confirmed", "HOLD_UNCONFIRMED"),
          ),
        );
  }

  openCamera(cameraId, onFrame = this.callbacks.onCamera) {
    this._requireConnected();
    if (typeof cameraId !== "string" || !cameraId)
      throw new StationError("Camera ID required", "INVALID_INPUT");
    this.closeCamera(cameraId);
    const camera = {
      id: cameraId,
      onFrame,
      closed: false,
      busy: false,
      pending: null,
      urls: new Set(),
      current: null,
      times: [],
    };
    const ws = new this.env.WebSocket(
      this._url(`/ws/camera/${encodeURIComponent(cameraId)}`, true),
      ["soarm_station", this.#token],
    );
    camera.ws = ws;
    ws.binaryType = "blob";
    this._cameras.set(cameraId, camera);
    ws.onmessage = (event) => {
      if (camera.closed || typeof event.data === "string") return;
      camera.pending = event.data;
      this._cameraDecode(camera);
    };
    ws.onerror = () =>
      this._error(
        new StationError(
          `Camera ${cameraId} connection failed`,
          "CAMERA_ERROR",
        ),
      );
    ws.onclose = () => {
      if (!camera.closed) {
        this.closeCamera(cameraId);
        onFrame({ cameraId, state: "closed", url: null });
      }
    };
    return () => this.closeCamera(cameraId);
  }
  async _cameraDecode(camera) {
    if (camera.busy || camera.closed || !camera.pending) return;
    camera.busy = true;
    const data = camera.pending;
    camera.pending = null;
    const blob =
      data instanceof Blob ? data : new Blob([data], { type: "image/jpeg" });
    const url = this.env.createObjectURL(blob);
    camera.urls.add(url);
    try {
      await this.env.decodeImage(url);
      if (camera.closed) return;
      const now = this.env.now();
      camera.times.push(now);
      if (camera.times.length > 30) camera.times.shift();
      const previous = camera.current;
      camera.current = url;
      camera.onFrame({
        cameraId: camera.id,
        state: "frame",
        url,
        receivedAt: now,
        fps:
          camera.times.length > 1 && now > camera.times[0]
            ? ((camera.times.length - 1) * 1000) / (now - camera.times[0])
            : null,
        sourceTimeNs: null,
        timestampSource: "browser-receipt",
      });
      // Decode first, then let the consumer install the new URL before retiring the old one.
      if (previous)
        this.env.afterPaint(() => this._releaseCameraURL(camera, previous));
    } catch (error) {
      this._releaseCameraURL(camera, url);
      if (!camera.closed) this._error(error);
    } finally {
      if (camera.closed) this._releaseCameraURL(camera, url);
      camera.busy = false;
      if (!camera.closed && camera.pending) this._cameraDecode(camera);
    }
  }
  _releaseCameraURL(camera, url) {
    if (camera.urls.delete(url)) this.env.revokeObjectURL(url);
  }
  closeCamera(cameraId) {
    const camera = this._cameras.get(cameraId);
    if (!camera) return;
    camera.closed = true;
    camera.pending = null;
    this._cameras.delete(cameraId);
    if (camera.ws.readyState < 2) camera.ws.close();
    for (const url of [...camera.urls]) this._releaseCameraURL(camera, url);
  }
}

export default StationClient;
