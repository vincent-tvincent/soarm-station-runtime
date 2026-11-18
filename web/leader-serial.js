/** Read-only STS3215 leader sampling through Web Serial.
 * Wire instructions are restricted to vendor SYNC READ (0x82), two bytes at
 * model/calibration/position registers. There is no generic command interface.
 */
const REGISTERS = new Set([3, 9, 11, 31, 56]);
const QUIET_MS = 2;
const PRIME_MS = 50;
const MAX_IDLE_BYTES = 4096;
const now = () => performance.now();
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const problem = (code, message) => Object.assign(new Error(message), {
  name: code === 'ABORTED' ? 'AbortError' : 'LeaderSerialError', code,
});
const canceled = () => problem('ABORTED', 'Leader serial operation canceled');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  promise.catch(() => {}); // A serial error can precede the write acknowledgement.
  return { promise, resolve, reject };
};
function bounded(promise, milliseconds, code, message) {
  let timer;
  const expired = new Promise((_, reject) => { timer = setTimeout(() => reject(problem(code, message)), milliseconds); });
  return Promise.race([promise, expired]).finally(() => clearTimeout(timer));
}
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value) => typeof value === 'string' && value.length > 0 && value.length <= 128;
const integer = (value, low, high) => Number.isInteger(value) && value >= low && value <= high;

function compileProfile(profile) {
  if (!object(profile) || !text(profile.id) || profile.motor_model !== 'sts3215')
    throw problem('PROFILE_INVALID', 'An explicit STS3215 leader profile is required');
  if (!object(profile.joint_map) || !object(profile.calibration) || !text(profile.gripper_source))
    throw problem('PROFILE_INVALID', 'Named joint mapping, gripper source and calibration are required');
  const mapping = Object.entries(profile.joint_map);
  if (!mapping.length || mapping.length > 63 || mapping.some(([a, b]) => !text(a) || !text(b)) ||
      new Set(mapping.map(([, b]) => b)).size !== mapping.length || Object.hasOwn(profile.joint_map, profile.gripper_source))
    throw problem('PROFILE_INVALID', 'Leader joint mapping must be complete and one-to-one');
  const entries = Object.entries(profile.calibration);
  const required = new Set([...mapping.map(([a]) => a), profile.gripper_source]);
  if (entries.length !== required.size || entries.some(([name]) => !required.has(name)))
    throw problem('PROFILE_INVALID', 'Calibration must exactly cover the mapped motors and gripper');
  const motors = entries.map(([name, c]) => {
    if (!object(c) || !integer(c.id, 1, 253) || c.drive_mode !== 0 ||
        !integer(c.homing_offset, -2047, 2047) || !integer(c.range_min, 0, 4095) ||
        !integer(c.range_max, 0, 4095) || c.range_min >= c.range_max)
      throw problem('PROFILE_INVALID', `Invalid saved calibration for ${name}`);
    return Object.freeze({ name, id: c.id, drive_mode: 0, homing_offset: c.homing_offset,
      range_min: c.range_min, range_max: c.range_max });
  });
  if (new Set(motors.map((m) => m.id)).size !== motors.length)
    throw problem('PROFILE_INVALID', 'Motor IDs must be unique');
  const match = profile.match ?? {};
  if (!object(match) || ['vid', 'pid'].some((key) => match[key] != null && !integer(match[key], 0, 65535)))
    throw problem('PROFILE_INVALID', 'USB vendor/product identifiers must be uint16 integers');
  return Object.freeze({ id: profile.id, motors: Object.freeze(motors),
    mapping: Object.freeze(mapping.map((entry) => Object.freeze(entry))),
    gripper: profile.gripper_source, match: Object.freeze({ vid: match.vid, pid: match.pid }) });
}

function checkPort(port, profile) {
  const info = port.getInfo?.() ?? {};
  for (const [key, field] of [['vid', 'usbVendorId'], ['pid', 'usbProductId']])
    if (profile.match[key] != null && info[field] !== profile.match[key])
      throw problem('PORT_MISMATCH', 'Selected USB adapter does not match this profile');
  return { ...info };
}

function readPacket(address, ids) {
  if (!REGISTERS.has(address) || !ids.length || ids.length > 64 || ids.some((id) => !integer(id, 1, 253)))
    throw problem('PROTOCOL_ERROR', 'Read outside the fixed servo register allowlist');
  const packet = new Uint8Array([255, 255, 254, ids.length + 4, 0x82, address, 2, ...ids, 0]);
  packet[packet.length - 1] = (~packet.slice(2, -1).reduce((sum, value) => sum + value, 0)) & 255;
  return packet;
}

function receiveBytes(pending, chunk) {
  if (!(chunk instanceof Uint8Array) || pending.used + chunk.length > pending.bytes.length)
    throw problem('PROTOCOL_ERROR', 'Unexpected or overlong servo response');
  pending.bytes.set(chunk, pending.used); pending.used += chunk.length;
  while (pending.parsed + 8 <= pending.used) {
    const p = pending.bytes.subarray(pending.parsed, pending.parsed + 8), id = p[2];
    if (p[0] !== 255 || p[1] !== 255 || p[3] !== 4 || !pending.ids.has(id) || pending.values.has(id))
      throw problem('PROTOCOL_ERROR', 'Servo response header, ID or length mismatch');
    if ((p.slice(2).reduce((sum, value) => sum + value, 0) & 255) !== 255)
      throw problem('PROTOCOL_ERROR', 'Servo response checksum mismatch');
    if (p[4] !== 0) throw problem('SERVO_ERROR', `Servo ${id} reports status ${p[4]}`);
    pending.values.set(id, p[5] | (p[6] << 8)); pending.parsed += 8;
  }
  // Reject malformed framing as soon as its fixed header becomes available.
  const remaining = pending.used - pending.parsed;
  if (remaining && pending.bytes[pending.parsed] !== 255 ||
      remaining >= 2 && pending.bytes[pending.parsed + 1] !== 255 ||
      remaining >= 4 && pending.bytes[pending.parsed + 3] !== 4)
    throw problem('PROTOCOL_ERROR', 'Malformed servo response framing');
  return pending.used === pending.bytes.length;
}

export class BrowserLeaderSerial {
  #serial; #port = null; #info = null; #session = null; #profile = null;
  #epoch = 0; #closing = Promise.resolve(); #sampling = null; #timing = null;
  #retiring = new WeakMap(); #transactionMs; #lifecycleMs;
  constructor({ serial = globalThis.navigator?.serial, transactionTimeoutMs = 1000, lifecycleTimeoutMs = 3000 } = {}) {
    if (![transactionTimeoutMs, lifecycleTimeoutMs].every((value) => Number.isFinite(value) && value > 0 && value <= 30000))
      throw problem('INVALID_TIMEOUT', 'USB transaction timeouts must be positive milliseconds, at most 30000');
    this.#transactionMs = transactionTimeoutMs; this.#lifecycleMs = lifecycleTimeoutMs;
    this.#serial = serial;
    serial?.addEventListener?.('disconnect', (event) => {
      const port = event.port || event.target;
      if (this.#session?.port === port)
        this.#lose(this.#session, problem('SERIAL_DISCONNECTED', 'Selected leader USB device disconnected'));
    });
  }
  get supported() { return typeof this.#serial?.requestPort === 'function'; }
  get selected() { return this.#port !== null; }
  get verified() { return Boolean(this.#profile && this.#session && !this.#session.closing); }
  get portInfo() { return this.#info ? { ...this.#info } : null; }
  get lastSampleTiming() { return this.#timing ? { ...this.#timing } : null; }

  async choose(profile) {
    const compiled = compileProfile(profile);
    if (!this.supported) throw problem('UNSUPPORTED', 'This browser does not provide Web Serial');
    const { epoch, closing } = this.#retire();
    const filter = compiled.match.vid == null ? undefined : { filters: [{ usbVendorId: compiled.match.vid,
      ...(compiled.match.pid == null ? {} : { usbProductId: compiled.match.pid }) }] };
    // Keep this call before any await: the browser requires the caller's gesture.
    const choice = this.#serial.requestPort(filter);
    const port = await choice;
    if (epoch !== this.#epoch) throw canceled();
    const info = checkPort(port, compiled);
    await closing;
    if (epoch !== this.#epoch) throw canceled();
    this.#port = port; this.#info = info;
    return { profile_id: compiled.id, port_info: { ...info } };
  }

  async verify(profile) {
    const compiled = compileProfile(profile), port = this.#port;
    if (!port) throw problem('PORT_REQUIRED', 'Choose a leader serial port first');
    const { epoch, closing } = this.#retire();
    await closing;
    if (epoch !== this.#epoch) throw canceled();
    const retiring = this.#retiring.get(port);
    if (retiring) await bounded(retiring, this.#lifecycleMs, 'SERIAL_BUSY', 'Previous USB open/close has not completed');
    if (epoch !== this.#epoch) throw canceled();
    const info = checkPort(port, compiled);
    const io = { port, epoch, opened: false, opening: null, closing: false, cleanup: null,
      reader: null, writer: null, pump: null, pending: null, cancel: deferred(), failure: null,
      priming: true, idleBytes: 0, lastByte: now(), profile: compiled };
    this.#session = io;
    try {
      io.opening = Promise.resolve(port.open({ baudRate: 1_000_000, dataBits: 8, stopBits: 1,
        parity: 'none', flowControl: 'none', bufferSize: 1024 })).then(() => { io.opened = true; });
      await bounded(this.#wait(io, io.opening), this.#lifecycleMs, 'SERIAL_OPEN_TIMEOUT', 'Leader USB open timed out'); this.#check(io);
      if (!port.readable || !port.writable) throw problem('SERIAL_DISCONNECTED', 'Serial streams are unavailable');
      io.reader = port.readable.getReader(); io.writer = port.writable.getWriter(); io.pump = this.#pump(io);
      await this.#quiet(io); io.priming = false;
      for (const [address, field] of [[3, 'model'], [9, 'range_min'], [11, 'range_max'], [31, 'homing_offset']]) {
        const values = await this.#query(io, address);
        for (const motor of compiled.motors) {
          let actual = values.get(motor.id);
          if (field === 'homing_offset') {
            if (actual & 0xf000) throw problem('PROTOCOL_ERROR', 'Invalid signed homing-offset register');
            actual = actual & 2048 ? -(actual & 2047) : actual;
          }
          if (actual !== (field === 'model' ? 777 : motor[field]))
            throw problem('CALIBRATION_MISMATCH', `Leader ${motor.name} ${field} does not match the saved profile`);
        }
      }
      this.#check(io); this.#profile = compiled; this.#info = info;
      return { verified: true, profile_id: compiled.id, motor_count: compiled.motors.length, port_info: { ...info } };
    } catch (error) {
      this.#lose(io, error); await this.#shutdown(io).catch(() => {}); throw error;
    }
  }

  async sample({ signal } = {}) {
    if (signal != null && (typeof signal.addEventListener !== 'function' ||
        typeof signal.removeEventListener !== 'function' || typeof signal.aborted !== 'boolean'))
      throw problem('INVALID_SIGNAL', 'sample signal must be an AbortSignal');
    if (this.#sampling) throw problem('BUSY', 'A leader sample is already in progress');
    const io = this.#session, profile = this.#profile;
    if (!io || !profile || io.closing) throw problem('NOT_VERIFIED', 'Verify the selected leader before sampling');
    const token = {}; this.#sampling = token;
    const abort = () => this.#lose(io, canceled());
    signal?.addEventListener('abort', abort, { once: true });
    try {
      if (signal?.aborted) { abort(); throw canceled(); }
      const started = now(), values = await this.#query(io, 56); this.#check(io);
      const raw = new Map([...values].map(([id, value]) => [id, value & 32768 ? -(value & 32767) : value]));
      const motors = new Map(profile.motors.map((motor) => [motor.name, motor]));
      const positions = Object.fromEntries(profile.mapping.map(([source, target]) => {
        const motor = motors.get(source);
        return [target, (raw.get(motor.id) - (motor.range_min + motor.range_max) / 2) * 2 * Math.PI / 4095];
      }));
      const jaw = motors.get(profile.gripper);
      const gripper = Math.max(0, Math.min(100, (raw.get(jaw.id) - jaw.range_min) * 100 / (jaw.range_max - jaw.range_min)));
      this.#timing = { started_ms: started, completed_ms: now() };
      return { positions, gripper, gripper_unit: 'percent' };
    } catch (error) {
      this.#lose(io, error); await this.#shutdown(io).catch(() => {}); throw error;
    } finally {
      signal?.removeEventListener('abort', abort);
      if (this.#sampling === token) this.#sampling = null;
    }
  }

  close() {
    const { closing } = this.#retire(), pending = this.#port && this.#retiring.get(this.#port);
    return pending ? bounded(Promise.all([closing.catch(() => {}), pending]), this.#lifecycleMs,
      'SERIAL_CLOSE_TIMEOUT', 'Leader USB close has not completed') : closing;
  }
  #retire() {
    const epoch = ++this.#epoch, io = this.#session;
    this.#session = null; this.#profile = null; this.#timing = null;
    const previous = this.#closing.catch(() => {});
    this.#closing = Promise.all([previous, io ? this.#shutdown(io) : undefined]).then(() => undefined);
    this.#closing.catch(() => {});
    return { epoch, closing: this.#closing };
  }
  #lose(io, error) {
    if (!io.failure) io.failure = error;
    if (this.#session === io) {
      ++this.#epoch; this.#session = null; this.#profile = null; this.#timing = null;
      const previous = this.#closing.catch(() => {});
      this.#closing = Promise.all([previous, this.#shutdown(io)]).then(() => undefined);
      this.#closing.catch(() => {});
    } else this.#shutdown(io).catch(() => {});
  }
  #check(io) {
    if (io.closing || this.#session !== io || this.#epoch !== io.epoch) throw io.failure || canceled();
  }
  #wait(io, promise) { return Promise.race([promise, io.cancel.promise]); }
  async #quiet(io) {
    const deadline = now() + PRIME_MS;
    do {
      this.#check(io); const before = io.lastByte;
      await this.#wait(io, delay(QUIET_MS)); this.#check(io);
      if (before === io.lastByte) return;
    } while (now() < deadline);
    throw problem('PROTOCOL_ERROR', 'Serial input did not become quiet before a fresh read');
  }
  async #query(io, address) {
    this.#check(io);
    if (io.pending) throw problem('BUSY', 'Concurrent servo transactions are forbidden');
    await this.#quiet(io); this.#check(io);
    const ids = io.profile.motors.map((motor) => motor.id), ready = deferred();
    const pending = { ...ready, ids: new Set(ids), bytes: new Uint8Array(ids.length * 8),
      used: 0, parsed: 0, values: new Map() };
    io.pending = pending;
    const timeout = setTimeout(() => this.#lose(io, problem('SERIAL_TIMEOUT', 'Leader USB read transaction timed out')), this.#transactionMs);
    try {
      await this.#wait(io, io.writer.write(readPacket(address, ids)));
      const values = await this.#wait(io, ready.promise); this.#check(io); return values;
    } finally {
      clearTimeout(timeout);
      if (io.pending === pending) io.pending = null;
    }
  }
  async #pump(io) {
    try {
      while (!io.closing) {
        const { value, done } = await io.reader.read();
        if (io.closing) break;
        if (done) throw problem('SERIAL_DISCONNECTED', 'Leader serial stream ended');
        if (!(value instanceof Uint8Array)) throw problem('PROTOCOL_ERROR', 'Serial stream returned non-byte data');
        if (!value.length) continue;
        io.lastByte = now();
        const pending = io.pending;
        if (!pending) {
          if (!io.priming) throw problem('PROTOCOL_ERROR', 'Unexpected serial bytes outside a read transaction');
          io.idleBytes += value.length;
          if (io.idleBytes > MAX_IDLE_BYTES) throw problem('PROTOCOL_ERROR', 'Excessive residual serial data');
          continue;
        }
        if (receiveBytes(pending, value)) { io.pending = null; pending.resolve(pending.values); }
      }
    } catch (error) { if (!io.closing) this.#lose(io, error); }
    finally { try { io.reader?.releaseLock(); } catch {} }
  }
  #shutdown(io) {
    if (io.closed) return io.closed;
    io.closing = true;
    const reason = io.failure || canceled(); io.cancel.reject(reason); io.pending?.reject(reason); io.pending = null;
    // Cancel and release JS stream locks immediately. Awaiting writer.abort()
    // first could hang behind a broken in-flight USB write.
    try { io.reader?.cancel().catch(() => {}); } catch {}
    try { io.writer?.abort(reason).catch(() => {}); } catch {}
    try { io.reader?.releaseLock(); } catch {}
    try { io.writer?.releaseLock(); } catch {}
    io.cleanup = (async () => {
      // Web Serial has no open() AbortSignal. A late open must still be closed,
      // and this exact port remains quarantined until that cleanup completes.
      await io.opening?.catch(() => {});
      if (io.opened) {
        try { await io.port.close(); }
        catch (error) {
          if (error.name !== 'InvalidStateError' || io.port.readable || io.port.writable) throw error;
        }
        io.opened = false;
      }
    })();
    this.#retiring.set(io.port, io.cleanup);
    io.cleanup.then(() => { if (this.#retiring.get(io.port) === io.cleanup) this.#retiring.delete(io.port); }, () => {});
    io.closed = bounded(io.cleanup, this.#lifecycleMs, 'SERIAL_CLOSE_TIMEOUT', 'Leader USB close has not completed');
    io.closed.catch(() => {}); return io.closed;
  }
}
