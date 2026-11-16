#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 Joseph Zhou
// SPDX-License-Identifier: MIT
/** Local validation bridge for the installed Grok Bot app's existing macOS login.
 * Never persists credentials or refreshes/modifies the client's account state.
 * The account RPC rejects a client version older than the installed app.
 * Internal APIs are version dependent; expired sessions must be renewed in the app.
 */
import { readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { pbkdf2Sync, createDecipheriv, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createServer } from 'node:http';

const BACKEND = 'https://api2.cursor.sh/aiserver.v1.GrokBotService/';
export const RPC_ROUTES = Object.freeze({
  'GET /credential': 'MintSandVoiceCallSecret',
  'POST /session': 'VoiceCallHarnessSession',
  'POST /tool': 'VoiceCallHarnessTool',
});
// 417 is pod_hibernated: the box is asleep or mid-transition. Grok Bot's own
// gateway client treats that status as "wake and reconnect". A cached URL
// answers 417 forever until EnsureSandBox runs again.
const STALE_SANDBOX_STATUS = new Set([401, 403, 404, 410, 417, 502, 503, 504]);
const STEPS = new Set(['ensure_sandbox', 'presence', 'presence_hangup', 'box_stream', 'typed_stream']);
const BODY_KINDS = new Set(['box_unknown_method', 'box_not_found', 'other']);
const BACKEND_HOST = 'api2.cursor.sh';
const HTTP_METHOD_RE = '(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)';
const NOT_FOUND_RE = new RegExp('not found:\\s*' + HTTP_METHOD_RE + '\\b');
// The whole token has to be letters. A prefix of Foo-bar is not a method name.
const UNKNOWN_METHOD_RE = /unknown gateway method:\s*([^\s"',}]{1,80})/;

function hostnameOf(raw) {
  try { return new URL(raw).hostname; } catch { return null; }
}

function safeHost(value) {
  return typeof value === 'string' && /^[A-Za-z0-9.-]{1,253}$/.test(value) && !value.includes('..') ? value : null;
}

// A 404 body is classified and discarded. Nothing else from it is kept.
export function classifyNotFoundBody(chunk) {
  const bytes = typeof chunk === 'string' ? Buffer.from(chunk) : (chunk instanceof Uint8Array ? Buffer.from(chunk) : Buffer.alloc(0));
  const text = bytes.subarray(0, 512).toString('utf8');
  if (text.includes('unknown gateway method:')) {
    const kind = {body_kind: 'box_unknown_method'};
    const found = UNKNOWN_METHOD_RE.exec(text);
    if (found && /^[A-Za-z]{1,60}$/.test(found[1])) kind.method = found[1];
    return kind;
  }
  if (NOT_FOUND_RE.test(text)) return {body_kind: 'box_not_found'};
  return {body_kind: 'other'};
}

function withStep(error, step, fields = {}) {
  if (!error || typeof error !== 'object') return error;
  if (!STEPS.has(error.step) && STEPS.has(step)) error.step = step;
  const host = safeHost(fields.target_host);
  if (!safeHost(error.target_host) && host) error.target_host = host;
  if (!BODY_KINDS.has(error.body_kind) && BODY_KINDS.has(fields.body_kind)) error.body_kind = fields.body_kind;
  if (typeof fields.method === 'string' && /^[A-Za-z]{1,60}$/.test(fields.method) && (typeof error.method !== 'string' || !/^[A-Za-z]{1,60}$/.test(error.method))) error.method = fields.method;
  return error;
}

async function readNotFoundBody(response) {
  if (response?.status !== 404) return Buffer.alloc(0);
  const reader = response.body?.getReader?.();
  if (!reader) {
    await Promise.resolve(response.body?.cancel?.()).catch(() => {});
    return Buffer.alloc(0);
  }
  const chunks = [];
  let total = 0;
  try {
    while (total < 512) {
      const next = await reader.read();
      if (next.done) break;
      const bytes = asBytes(next.value);
      if (!bytes || bytes.byteLength === 0) continue;
      const take = bytes.subarray(0, 512 - total);
      chunks.push(Buffer.from(take));
      total += take.byteLength;
      if (bytes.byteLength > take.byteLength) break;
    }
  } catch { /* an unreadable 404 is body_kind other */ }
  finally { await reader.cancel().catch(() => {}); }
  return Buffer.concat(chunks);
}

async function sandboxHttpError(response, step, targetHost) {
  const fields = {target_host: targetHost};
  if (response?.status === 404) Object.assign(fields, classifyNotFoundBody(await readNotFoundBody(response)));
  else await Promise.resolve(response?.body?.cancel?.()).catch(() => {});
  return withStep(new Error(`upstream_http_${response.status}`), step, fields);
}

export function loadAccount() {
  const data = JSON.parse(readFileSync(join(homedir(), 'Library/Application Support/Grok Bot/sand-secrets.json'), 'utf8'));
  const accounts = JSON.parse(data['cursor-accounts']);
  const active = accounts.accounts[accounts.active];
  if (!active) throw new Error('login_unavailable');
  const password = execFileSync('/usr/bin/security', ['find-generic-password', '-w', '-s', 'Grok Bot Safe Storage'], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 120000,
  }).trimEnd();
  const key = pbkdf2Sync(password, 'saltysalt', 1003, 16, 'sha1');
  const decrypt = (value) => {
    const blob = Buffer.from(value, 'base64');
    if (blob.subarray(0, 3).toString() !== 'v10') throw new Error('unsupported_storage');
    const cipher = createDecipheriv('aes-128-cbc', key, Buffer.alloc(16, 32));
    return Buffer.concat([cipher.update(blob.subarray(3)), cipher.final()]).toString('utf8');
  };
  const token = decrypt(active['cursor-access-token']);
  const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
  if (!Number.isFinite(claims.exp) || claims.exp * 1000 < Date.now() + 30000) throw new Error('login_expired_open_grok_bot');
  const machineId = decrypt(data['cursor-machine-id']);
  const teamId = active['cursor-selected-team-id'] ? decrypt(active['cursor-selected-team-id']) : undefined;
  return { token, machineId, teamId, authId: claims.sub };
}

function checksum(machineId) {
  const t = Math.floor(Date.now() / 1e6);
  const bytes = Buffer.from([t >> 40 & 255, t >> 32 & 255, t >> 24 & 255, t >> 16 & 255, t >> 8 & 255, t & 255]);
  let previous = 165;
  for (let i = 0; i < bytes.length; i++) { bytes[i] = (bytes[i] ^ previous) + i % 256; previous = bytes[i]; }
  return bytes.toString('base64url') + machineId;
}

const APP_PLIST = '/Applications/Grok Bot.app/Contents/Info.plist';
const SECRETS_PATH = () => join(homedir(), 'Library/Application Support/Grok Bot/sand-secrets.json');

// Prod builds send the semver prefix. A prerelease suffix is not part of the header.
export function parseClientVersion(raw) {
  const match = /^(\d+\.\d+\.\d+)(?:-.+)?$/.exec(String(raw ?? '').trim());
  return match ? match[1] : null;
}

function readInstalledVersionText() {
  return execFileSync('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleShortVersionString', APP_PLIST], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 5000,
  });
}

export function createInstalledClientVersion({ stat, read } = {}) {
  let seenMtime;
  let cached = null;
  return function clientVersion() {
    let mtime;
    try { mtime = stat(); } catch { throw new Error('client_version_unavailable'); }
    if (cached !== null && mtime === seenMtime) return cached;
    let raw;
    try { raw = read(); } catch { throw new Error('client_version_unavailable'); }
    const version = parseClientVersion(raw);
    if (!version) throw new Error('client_version_unavailable');
    seenMtime = mtime;
    cached = version;
    return version;
  };
}

const installedClientVersionCached = createInstalledClientVersion({
  stat: () => statSync(APP_PLIST).mtimeMs,
  read: readInstalledVersionText,
});

export function installedClientVersion() {
  return installedClientVersionCached();
}

const ACCOUNT_FAILURE_REASONS = new Set(['login_rejected', 'client_update_required']);

function accountFailure(status, reason, fields = {}) {
  const error = new Error(`upstream_http_${status}`);
  error.reason = reason;
  return withStep(error, fields.step, fields);
}

function accountFailureReason(response) {
  const headers = response?.headers;
  const hint = typeof headers?.get === 'function' ? headers.get('x-automation-failure-hint') : null;
  return hint === 'SAND_CLIENT_UPDATE_REQUIRED' ? 'client_update_required' : 'login_rejected';
}

// EnsureSandBox 401/403 waits 5s, then 10, 20, 40, and 60. A success or a
// rewritten sand-secrets.json tries again at once. Other failures do not wait.
export function createEnsureBackoff({ now = Date.now, secretsMtime = defaultSecretsMtime, startMs = 5000, maxMs = 60000 } = {}) {
  let delay = startMs;
  let until = 0;
  let seenMtime = null;
  let lastStatus = 401;
  let lastReason = 'login_rejected';
  const noteSecrets = () => {
    let mtime = null;
    try { mtime = secretsMtime(); } catch { mtime = null; }
    if (seenMtime !== null && mtime !== null && mtime !== seenMtime) {
      delay = startMs;
      until = 0;
    }
    if (mtime !== null) seenMtime = mtime;
  };
  return {
    blocked() {
      noteSecrets();
      return now() < until;
    },
    reject(status, reason = 'login_rejected') {
      noteSecrets();
      lastStatus = status;
      lastReason = reason;
      until = now() + delay;
      delay = Math.min(delay * 2, maxMs);
    },
    succeed() {
      delay = startMs;
      until = 0;
      noteSecrets();
    },
    error() { return accountFailure(lastStatus, lastReason, {step: 'ensure_sandbox', target_host: BACKEND_HOST}); },
  };
}

function defaultSecretsMtime() {
  try { return statSync(SECRETS_PATH()).mtimeMs; } catch { return null; }
}

const defaultEnsureBackoff = createEnsureBackoff();

async function accountRequest(method, body, { accountLoader = loadAccount, fetcher = fetch, signal, streaming = false, clientVersion, backoff } = {}) {
  if (!Object.values(RPC_ROUTES).includes(method) && method !== 'EnsureSandBox' && method !== 'WatchGrokBotTranscripts') throw new Error('rpc_forbidden');
  if (method === 'EnsureSandBox' && ![JSON.stringify({wake: false, supportsAsleepBox: true}), '{}'].includes(JSON.stringify(body))) throw new Error('rpc_forbidden');
  if (method === 'WatchGrokBotTranscripts' && (!streaming || body.includeUnlistedAgents !== false || body.inlineBodyMaxBytes !== 65536 || body.cursors?.length !== 1 || typeof body.cursors[0].agentId !== 'string' || Object.keys(body.cursors[0]).some(k => !['agentId', 'generation', 'afterUpdatedSeq'].includes(k)) || body.cursors[0].generation !== 0 || body.cursors[0].afterUpdatedSeq !== '0')) throw new Error('rpc_forbidden');
  const ensure = method === 'EnsureSandBox';
  const step = ensure ? 'ensure_sandbox' : (method === 'WatchGrokBotTranscripts' ? 'typed_stream' : null);
  const gate = backoff !== undefined ? backoff : (ensure ? defaultEnsureBackoff : null);
  if (ensure && gate?.blocked()) throw gate.error();
  const account = accountLoader();
  const version = clientVersion ?? installedClientVersion();
  const headers = {
    authorization: `Bearer ${account.token}`, 'content-type': 'application/json',
    'connect-protocol-version': '1', 'x-cursor-client-type': 'sand',
    'x-cursor-client-version': version, 'x-cursor-client-source': 'sand-desktop',
    'x-cursor-client-os': 'darwin', 'x-sand-box-namespace': 'prod',
    'x-ghost-mode': 'true', 'x-cursor-checksum': checksum(account.machineId),
    'x-request-id': randomUUID(), 'x-cursor-timezone': Intl.DateTimeFormat().resolvedOptions().timeZone,
  };
  if (account.teamId) headers['x-cursor-team-id'] = account.teamId;
  let payload = Buffer.from(JSON.stringify(body));
  if (streaming) {
    headers['content-type'] = 'application/connect+json';
    const prefix = Buffer.alloc(5); prefix.writeUInt32BE(payload.length, 1);
    payload = Buffer.concat([prefix, payload]);
  }
  const response = await fetcher(BACKEND + method, { method: 'POST', headers,
    body: payload, redirect: 'error', signal: signal ?? AbortSignal.timeout(20000) });
  if (!response.ok) {
    // Upstream bodies can contain user content or credentials. Never read or log
    // them, except a 404 on EnsureSandBox or the transcript watch: at most 512
    // bytes are classified into a fixed body_kind and then discarded.
    // The update hint is a fixed header. Any other 401/403 is a rejected login.
    // 417 stays a hibernated box.
    const fields = step ? {step, target_host: BACKEND_HOST} : {};
    if (response.status === 404 && step) Object.assign(fields, classifyNotFoundBody(await readNotFoundBody(response)));
    if (response.status === 401 || response.status === 403) {
      const reason = accountFailureReason(response);
      if (ensure && gate) gate.reject(response.status, reason);
      throw accountFailure(response.status, reason, fields);
    }
    throw withStep(new Error(`upstream_http_${response.status}`), step, fields);
  }
  if (ensure && gate) gate.succeed();
  return response;
}

export async function accountRpc(method, body, options = {}) {
  if (method === 'WatchGrokBotTranscripts') throw new Error('rpc_forbidden');
  return (await accountRequest(method, body, options)).json();
}

const PORT_HEADERS = new Set(['network_token', 'gateway_port_token', 'none']);

// A non-empty networkToken is the legacy header. The current EnsureSandBox
// response leaves that empty and sends gatewayPortToken on a different header.
function sandboxPortHeader(raw) {
  if (typeof raw?.networkToken === 'string' && raw.networkToken) {
    return {name: 'x-anyrun-network-token', value: raw.networkToken, port_header: 'network_token'};
  }
  if (typeof raw?.gatewayPortToken === 'string' && raw.gatewayPortToken) {
    return {name: 'x-anyrun-port-token', value: raw.gatewayPortToken, port_header: 'gateway_port_token'};
  }
  return {name: '', value: '', port_header: 'none'};
}

function refreshDelayMs(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return null;
  return Math.min(seconds * 1000, 2 ** 31 - 1);
}

function defaultSchedule(fn, ms) {
  const timer = setTimeout(fn, ms);
  timer.unref?.();
  return timer;
}

export function validateSandboxUrl(raw) {
  const url = new URL(raw);
  const host = url.hostname.toLowerCase();
  const allowed = host === 'cursor.sh' || host === 'cursor.com' || host.endsWith('.cursor.sh') || host.endsWith('.cursor.com') || host.endsWith('.cursorvm.com');
  if (url.protocol !== 'https:' || !allowed || url.username || url.password || url.search || url.hash) throw new Error('sandbox_url_forbidden');
  return url.toString().replace(/\/$/, '');
}

export class NativeSandboxClient {
  #connection;
  #loading;
  #log;
  #lastEnsure;
  #gatewayHost;
  #refreshTimer;
  #schedule;
  #cancelSchedule;
  constructor({rpc = accountRpc, fetcher = fetch, accountLoader = loadAccount, connectMode = 'locate', log = null, schedule = defaultSchedule, cancelSchedule = clearTimeout} = {}) {
    if (!['locate', 'normal'].includes(connectMode)) throw new Error('invalid_connect_mode');
    this.connectMode = connectMode;
    this.rpc = rpc; this.fetcher = fetcher; this.accountLoader = accountLoader;
    this.#log = log;
    this.#schedule = schedule;
    this.#cancelSchedule = cancelSchedule;
  }
  attachLog(log) { this.#log = log; }
  gatewayHost() { return this.#gatewayHost ?? null; }
  #noteEnsure(host, fields, tokenSource) {
    const target = safeHost(host);
    const names = (Array.isArray(fields) ? fields : []).filter(name => typeof name === 'string' && /^[A-Za-z0-9_]{1,64}$/.test(name));
    const portHeader = PORT_HEADERS.has(tokenSource) ? tokenSource : 'none';
    if (!target) return;
    const signature = target + '\0' + names.join('\n') + '\0' + portHeader;
    if (this.#lastEnsure === signature) return;
    this.#lastEnsure = signature;
    if (typeof this.#log !== 'function') return;
    this.#log({source: 'ensure', state: 'ensure_ok', target_host: target, response_fields: names, port_header: portHeader});
  }
  #clearRefresh() {
    if (this.#refreshTimer === undefined) return;
    this.#cancelSchedule(this.#refreshTimer);
    this.#refreshTimer = undefined;
  }
  #armRefresh(connection, seconds) {
    this.#clearRefresh();
    const ms = refreshDelayMs(seconds);
    if (ms === null) return;
    let handle;
    const fire = () => {
      if (this.#refreshTimer !== handle) return;
      this.#refreshTimer = undefined;
      this.#drop(connection);
    };
    handle = this.#schedule(fire, ms);
    this.#refreshTimer = handle;
  }
  async #connect() {
    if (this.#connection) return this.#connection;
    if (!this.#loading) this.#loading = (async () => {
      const account = this.accountLoader();
      const raw = await this.rpc('EnsureSandBox', this.connectMode === 'normal' ? {} : {wake: false, supportsAsleepBox: true}, {accountLoader: () => account, fetcher: this.fetcher});
      if (typeof raw.gatewayToken !== 'string' || !raw.gatewayToken) throw new Error('sandbox_connection_unavailable');
      const port = sandboxPortHeader(raw);
      // authorization stays the gateway token. TODO: if presence still 404s, the
      // app's next step is an account-token wrap for this gateway base URL: set
      // authorization to "<scheme> <accountAccessToken>" and move gatewayToken
      // into x-sand-gateway-token, refreshing before the access token expires.
      const headers = {authorization: `Bearer ${raw.gatewayToken}`};
      if (port.port_header !== 'none') headers[port.name] = port.value;
      const responseFields = Object.keys(raw).sort();
      const baseUrl = validateSandboxUrl(raw.gatewayUrl);
      this.#connection = {baseUrl, headers, authId: account.authId, responseFields};
      this.#gatewayHost = hostnameOf(baseUrl);
      this.#noteEnsure(this.#gatewayHost, responseFields, port.port_header);
      this.#armRefresh(this.#connection, raw.credentialsRefreshAfterSeconds);
      return this.#connection;
    })().finally(() => { this.#loading = undefined; });
    return this.#loading;
  }
  // The cached sandbox URL and token go stale (box asleep or token rotated) while this process lives on,
  // and every later call then fails until a fresh EnsureSandBox. Drop the cache and retry once.
  async #fetch(path, options = {}, retry = true) {
    const connection = await this.#connect();
    let response;
    try {
      response = await this.fetcher(connection.baseUrl + path, {...options, redirect: 'error', headers: {...connection.headers, ...options.headers}});
    } catch (error) {
      if (options.signal?.aborted) throw error;
      this.#drop(connection);
      if (!retry) throw error;
      return this.#fetch(path, options, false);
    }
    if (STALE_SANDBOX_STATUS.has(response.status)) {
      this.#drop(connection);
      if (retry && !options.signal?.aborted) {
        await response.body?.cancel();
        return this.#fetch(path, options, false);
      }
    }
    return response;
  }
  #drop(connection) {
    if (this.#connection !== connection) return;
    this.#connection = undefined;
    this.#clearRefresh();
  }
  // The box stream died under us. Forget the URL so the next presence wakes a pod.
  invalidate() { this.#connection = undefined; this.#clearRefresh(); }
  // Wake a hibernated pod. /health goes through #fetch, so a 417 still drops the
  // cache and runs EnsureSandBox. Do not open /events: a second stream makes the
  // pod close the one this process is already reading.
  async warm() {
    try {
      const health = await this.#fetch('/health', {signal: AbortSignal.timeout(20000)});
      if (!health.ok) throw await sandboxHttpError(health, 'box_stream', this.#gatewayHost);
      await health.body?.cancel();
    } catch {
      this.invalidate();
      await this.#connect();
    }
  }
  async openEvents(signal) {
    const deadline = new AbortController();
    const timer = setTimeout(() => deadline.abort(), 20000);
    try {
      let response;
      try {
        response = await this.#fetch('/events', {headers: {accept: 'text/event-stream'}, signal: signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal});
      } catch (error) {
        throw withStep(error, 'box_stream', {target_host: this.#gatewayHost});
      }
      if (!response.ok || !response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream')) {
        throw await sandboxHttpError(response, 'box_stream', this.#gatewayHost);
      }
      return response;
    } finally { clearTimeout(timer); }
  }
  async probe() {
    const connection = await this.#connect();
    const health = await this.#fetch('/health', {signal: AbortSignal.timeout(20000)});
    const healthStatus = health.status;
    if (!health.ok) throw await sandboxHttpError(health, 'box_stream', this.#gatewayHost);
    await health.body?.cancel();
    const events = await this.openEvents();
    const result = {ok: true, responseFields: connection.responseFields, https: true, hostnameAllowed: true, gatewayTokenPresent: true, networkHeaderPresent: Boolean(connection.headers['x-anyrun-network-token']), healthStatus, eventsStatus: events.status, eventStreamContentTypeOk: true};
    // Handshake only. Do not parse/read the initial snapshot or any messages.
    await events.body?.cancel();
    return result;
  }
  async presence({agentId, callId, isOnTheLine, acceptsOverheard}) {
    const step = isOnTheLine ? 'presence' : 'presence_hangup';
    let response;
    try {
      response = await this.#fetch('/api/setVoiceCallPresence', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({id: agentId, callId, isOnTheLine, acceptsOverheard}), signal: AbortSignal.timeout(20000),
      });
    } catch (error) {
      throw withStep(error, step, {target_host: this.#gatewayHost});
    }
    if (!response.ok) throw await sandboxHttpError(response, step, this.#gatewayHost);
    await response.body?.cancel();
  }
  async callerAuthId() { return (await this.#connect()).authId; }
}

export class NativeTranscriptClient {
  constructor({accountLoader = loadAccount, fetcher = fetch, clientVersion} = {}) {
    this.accountLoader = accountLoader; this.fetcher = fetcher; this.clientVersion = clientVersion;
  }
  async probe(agentId) {
    const response = await accountRequest('WatchGrokBotTranscripts', {cursors: [{agentId, generation: 0, afterUpdatedSeq: '0'}], includeUnlistedAgents: false, inlineBodyMaxBytes: 65536}, {accountLoader: this.accountLoader, fetcher: this.fetcher, clientVersion: this.clientVersion, signal: AbortSignal.timeout(20000), streaming: true});
    const ok = response.headers.get('content-type')?.startsWith('application/connect+json') === true;
    await response.body?.cancel();
    return {ok, status: response.status, connectJson: ok};
  }
  async *events(context, signal, onOpen = () => {}) {
    // Empty session is the native owner slot. Non-owner sessions require a
    // separately verified metadata binding and are deliberately unsupported.
    if (context.sessionId) throw new Error('session_binding_unavailable');
    const response = await accountRequest('WatchGrokBotTranscripts', {cursors: [{agentId: context.agentId, generation: 0, afterUpdatedSeq: '0'}], includeUnlistedAgents: false, inlineBodyMaxBytes: 65536}, {accountLoader: this.accountLoader, fetcher: this.fetcher, clientVersion: this.clientVersion, signal, streaming: true});
    if (!response.headers.get('content-type')?.startsWith('application/connect+json')) { await response.body?.cancel(); throw new Error('native_stream_format'); }
    onOpen(response.status);
    let pending = Buffer.alloc(0);
    const state = {connected: false, sinceMs: context.sinceMs, generation: null};
    // An aborted fetch can leave a body read pending for minutes. Cancelling the
    // reader settles a pending read at once, whatever the socket does.
    const reader = response.body?.getReader?.();
    if (!reader) throw new Error('native_stream_format');
    const stop = () => { reader.cancel().catch(() => {}); };
    if (signal?.aborted) stop(); else signal?.addEventListener('abort', stop, {once: true});
    try {
      while (true) {
        const next = await reader.read();
        if (next.done) break;
        const chunk = asBytes(next.value);
        if (!chunk) continue;
        pending = Buffer.concat([pending, chunk]);
        if (pending.length > 2 * 1024 * 1024) throw new Error('event_too_large');
        while (pending.length >= 5) {
          const size = pending.readUInt32BE(1), flags = pending[0];
          if (size > 1024 * 1024) throw new Error('event_too_large');
          if (pending.length < size + 5) break;
          const data = pending.subarray(5, size + 5); pending = pending.subarray(size + 5);
          if (flags === 2) return; // End-stream envelope; never expose its text.
          if (flags !== 0) throw new Error('native_stream_format');
          const event = filterTranscriptFrame(JSON.parse(data.toString('utf8')), context, state);
          for (const item of event) yield item;
        }
      }
    } finally {
      signal?.removeEventListener('abort', stop);
      stop();
    }
  }
}

export function filterTranscriptFrame(frame, context, state) {
  if (frame.connected) {
    const at = Number(frame.connected.serverTimeMs);
    state.connected = Number.isFinite(at); state.sinceMs = Math.max(context.sinceMs, at); state.generation = null;
    return [];
  }
  const reset = frame.cleared ?? frame.cursorTooOld;
  if (reset && reset.agentId === context.agentId && !reset.sessionId) {
    state.connected = false; state.generation = null;
    // Fail closed: caller reconnects and establishes a new server-time baseline.
    throw new Error('transcript_rebaseline_required');
  }
  const rows = frame.rows;
  // Replay frames still carry box replies from a gap. User messages reject replay below.
  if (!state.connected || !rows || rows.agentId !== context.agentId || (rows.sessionId || '') !== context.sessionId) return [];
  // Replay rows arrive with only agentId, entries and replay: no generation.
  // Without one, only box replies pass; user messages stay fail-closed.
  const versioned = Number.isSafeInteger(rows.generation);
  if (versioned) {
    if (state.generation !== null && rows.generation !== state.generation) throw new Error('transcript_rebaseline_required');
    state.generation = rows.generation;
  }
  const replay = rows.replay === true || !versioned;
  const result = [];
  for (const row of rows.entries ?? []) {
    // No blob reads, omitted-body reads or history snapshots are performed.
    if (typeof row.body !== 'string' || row.bodyOmitted) continue;
    let entry;
    try { entry = JSON.parse(Buffer.from(row.body, 'base64').toString('utf8')); } catch { continue; }
    const event = filterNativeEvent({channel: 'transcript', payload: {type: 'appended', entry, ordered: {replicaKey: 'transcript:' + context.agentId, sequence: 0}}}, {...context, sinceMs: state.sinceMs, replay});
    if (event) result.push(event);
  }
  return result;
}

const NUDGE_KINDS = new Set(['ack', 'progress', 'outcome', 'overheard']);

// The pod closes the SSE socket on a fixed clock. That is not a stale sandbox.
export function isRoutineStreamEnd(error) {
  const message = String(error?.message ?? '');
  const cause = String(error?.cause?.code ?? error?.cause?.message ?? '');
  return message === 'terminated' || cause === 'UND_ERR_SOCKET' || message.includes('UND_ERR_SOCKET');
}

function boxReply(entry, context) {
  if (!entry || entry.kind !== 'send-message' || typeof entry.id !== 'string' || !entry.id) return null;
  const message = entry.message;
  if (!message || message.type !== 'text' || typeof message.channel !== 'string' || typeof message.content !== 'string') return null;
  if (!message.channel.startsWith('voice:')) return null;
  const callId = message.channel.slice('voice:'.length).trim();
  if (!callId || callId !== context.callId) return null;
  const text = message.content.trim();
  if (!text || !Number.isFinite(entry.timestampMs)) return null;
  const key = 'entry:' + entry.id;
  if (context.seen.has(key)) return null;
  context.seen.add(key);
  return {channel: 'box-reply', payload: {agentId: context.agentId, callId, entryId: entry.id, text: text.slice(0, 16000), atMs: entry.timestampMs}};
}

export function filterNativeEvent(event, context) {
  const p = event?.payload;
  if (!p || typeof p !== 'object') return null;
  if (event.channel === 'voice-call-nudge') {
    // A nudge is only "something landed, read the transcript". The text is not spoken.
    if (p.agentId !== context.agentId || p.callId !== context.callId || !['progress', 'outcome', 'overheard'].includes(p.kind)) return null;
    if (p.kind === 'overheard' && !context.acceptsOverheard) return null;
    const eventId = typeof event.eventId === 'string' && event.eventId.length > 0 && event.eventId.length <= 256
      ? event.eventId : undefined;
    if (eventId !== undefined) {
      const key = 'nudge-id:' + eventId;
      if (context.seen.has(key)) return null;
      context.seen.add(key);
    }
    const payload = {agentId: p.agentId, callId: p.callId, kind: p.kind};
    if (eventId !== undefined) payload.eventId = eventId;
    return {channel: 'box-signal', payload};
  }
  if (event.channel !== 'transcript' || p.type !== 'appended') return null;
  // Ordered replica keys are the authoritative agent/session scope in 0.71.2.
  const replica = p.ordered?.replicaKey;
  const expected = 'transcript:' + context.agentId + (context.sessionId ? '\0' + context.sessionId : '');
  if (replica !== expected || !Number.isSafeInteger(p.ordered?.sequence)) return null;
  const entry = p.entry;
  const reply = boxReply(entry, context);
  if (reply) return reply;
  // Replayed user turns are not new speech. Box replies above are, so a gap can be filled.
  if (context.replay) return null;
  if (!entry || entry.kind !== 'message' || entry.role !== 'user' || typeof entry.id !== 'string' || !entry.id || typeof entry.content !== 'string') return null;
  if ('channel' in entry || 'fromAgent' in entry || 'channelSender' in entry || 'fromUser' in entry || entry.author?.kind !== 'cursor_user' || entry.author.id !== context.authId) return null;
  // No baseline/snapshot is ever forwarded. Timestamp bounds also reject replay
  // of old appended frames after a transport reconnect; missing time is closed.
  if (!Number.isFinite(entry.timestampMs) || entry.timestampMs < context.sinceMs || !entry.content.trim()) return null;
  const key = 'entry:' + entry.id;
  if (context.seen.has(key)) return null;
  context.seen.add(key);
  return {channel: 'typed-message', payload: {agentId: context.agentId, callId: context.callId, entryId: entry.id, text: entry.content.slice(0, 16000), atMs: entry.timestampMs}};
}

export function validateBody(route, body) {
  if (route === 'GET /credential') return {};
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('invalid_body');
  const fields = route === 'POST /session'
    ? ['agentId', 'agentName', 'userName', 'description', 'spokenLanguage']
    : ['agentId', 'callId', 'name', 'input', 'line'];
  if (Object.keys(body).some(k => !fields.includes(k))) throw new Error('invalid_body');
  if (typeof body.agentId !== 'string' || !body.agentId || body.agentId.length > 200) throw new Error('invalid_agent');
  if (route === 'POST /session') {
    if (typeof body.agentName !== 'string') throw new Error('invalid_name');
    for (const k of fields.slice(2)) if (body[k] !== undefined && typeof body[k] !== 'string') throw new Error('invalid_body');
  } else {
    if (typeof body.callId !== 'string' || !body.callId || typeof body.name !== 'string' || !body.name) throw new Error('invalid_tool');
    const lineKeys = ['theyJustTalked', 'newsOwed', 'receiptOwed', 'alreadySpokeThisTurn'];
    if (!body.line || Object.keys(body.line).some(k => !lineKeys.includes(k)) || lineKeys.some(k => typeof body.line[k] !== 'boolean')) throw new Error('invalid_line');
  }
  return body;
}

function asBytes(value) {
  if (value instanceof Uint8Array) return value;
  if (typeof value === 'string') return Buffer.from(value);
  return null;
}

function safeErrorMessage(error) {
  const message = String(error?.message ?? '');
  if (message === 'terminated' || message === 'fetch failed') return message;
  if (/^upstream_http_\d{3}$/.test(message)) return message;
  if (/^The ".{1,40}" argument must be/.test(message)) return message.slice(0, 120);
  return null;
}

function streamFailure(error, where, defaults = {}) {
  const status = /^upstream_http_(\d{3})$/.exec(error?.message ?? '');
  const name = ['Error', 'TypeError', 'AbortError', 'TimeoutError'].includes(error?.name) ? error.name : 'other';
  const failure = {http_status: status ? Number(status[1]) : null, error_class: name};
  const message = safeErrorMessage(error);
  if (message) failure.error_message = message;
  if (where) failure.frame = where;
  const cause = error?.cause?.code;
  if (typeof cause === 'string' && /^[A-Z0-9_]{1,40}$/.test(cause)) failure.cause = cause;
  const step = STEPS.has(error?.step) ? error.step : (STEPS.has(defaults.step) ? defaults.step : null);
  if (step) failure.step = step;
  const host = safeHost(error?.target_host) || safeHost(defaults.target_host);
  if (host) failure.target_host = host;
  if (BODY_KINDS.has(error?.body_kind)) failure.body_kind = error.body_kind;
  if (typeof error?.method === 'string' && /^[A-Za-z]{1,60}$/.test(error.method)) failure.method = error.method;
  return failure;
}

function hangupFailure(error) {
  const status = /^upstream_http_(\d{3})$/.exec(String(error?.message ?? ''));
  const httpStatus = status ? Number(status[1]) : null;
  const fields = {
    http_status: httpStatus,
    error_class: status ? `upstream_http_${status[1]}` : 'failed',
    step: STEPS.has(error?.step) ? error.step : 'presence_hangup',
  };
  const host = safeHost(error?.target_host);
  if (host) fields.target_host = host;
  if (BODY_KINDS.has(error?.body_kind)) fields.body_kind = error.body_kind;
  if (typeof error?.method === 'string' && /^[A-Za-z]{1,60}$/.test(error.method)) fields.method = error.method;
  return {httpStatus, fields};
}

export function loadCall(path) {
  if (!path) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8'));
    if (!parsed || typeof parsed.agentId !== 'string' || typeof parsed.callId !== 'string') return null;
    if (!/^[A-Za-z0-9_-]{1,120}$/.test(parsed.callId)) return null;
    return {agentId: parsed.agentId, callId: parsed.callId, acceptsOverheard: parsed.acceptsOverheard === true};
  } catch { return null; }
}

export function rememberCall(path, call) {
  if (!path || !call) return;
  try {
    writeFileSync(path, JSON.stringify({agentId: call.agentId, callId: call.callId, acceptsOverheard: call.acceptsOverheard === true}));
  } catch { /* the call is still registered in memory */ }
}

export function forgetCall(path) {
  if (!path) return;
  try { unlinkSync(path); } catch { /* already gone */ }
}

function waitForNativeRetry(milliseconds, signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    signal.addEventListener('abort', finish, {once: true});
  });
}

export function createBridge({ port = 18771, rpc = accountRpc, agentId = process.env.STACKCHAN_GBOT_VOICE_BOT_ID || process.env.STACKCHAN_GBOT_VOICE_AGENT_ID || process.env.STACKCHAN_TOOL_BOT_ID, toolsEnabled = process.env.STACKCHAN_GBOT_VOICE_TOOLS === '1', desktopEnabled = Boolean(process.env.STACKCHAN_TOOL_BOT?.trim()) || process.env.STACKCHAN_GBOT_VOICE_DESKTOP === '1', sandbox = new NativeSandboxClient({connectMode: 'normal'}), transcript = new NativeTranscriptClient(), nativeRetryDelayMs = 5000, nativeLog = event => console.log(JSON.stringify({event: 'gbot_native_stream', ...event})), sessionId = process.env.STACKCHAN_GBOT_VOICE_SESSION_ID || '', callStore = null } = {}) {
  if (!Number.isFinite(nativeRetryDelayMs) || nativeRetryDelayMs < 10 || nativeRetryDelayMs > 30000) throw new Error('invalid_native_retry_delay');
  const logStream = (source, state, extra = {}) => nativeLog({source, state, at: new Date().toISOString(), ...extra});
  if (typeof sandbox.attachLog === 'function') {
    sandbox.attachLog(event => logStream(event.source, event.state, {
      target_host: event.target_host, response_fields: event.response_fields, port_header: event.port_header,
    }));
  }
  let activeCall = null;
  let presenceBusy = false;
  let warmFlight = null;
  const streams = new Set();
  const hangupActiveCall = async () => {
    const call = activeCall || loadCall(callStore);
    for (const controller of streams) controller.abort();
    activeCall = null;
    if (!call) return;
    try {
      await sandbox.presence({agentId: call.agentId, callId: call.callId, isOnTheLine: false, acceptsOverheard: !!call.acceptsOverheard});
      logStream('presence', 'shutdown_hangup', {http_status: 200, error_class: null});
      forgetCall(callStore);
    } catch (error) {
      logStream('presence', 'shutdown_hangup', streamFailure(error, 'gbot_voice_session_bridge.mjs:shutdown', {step: 'presence_hangup', target_host: error?.target_host}));
    }
  };
  const server = createServer(async (req, res) => {
    const send = (status, value) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' }); res.end(JSON.stringify(value)); };
    const address = req.socket.remoteAddress;
    if (address !== '127.0.0.1' || req.headers.origin !== undefined || req.headers['sec-fetch-site'] && req.headers['sec-fetch-site'] !== 'none' || req.headers.host !== `127.0.0.1:${port}`) return send(403, { ok: false, error: 'local_client_only' });
    const route = `${req.method} ${req.url}`;
    if (route === 'GET /health') {
      let clientVersion = null;
      try { clientVersion = installedClientVersion(); } catch { clientVersion = null; }
      return send(200, { ok: true, source: 'grok-bot-existing-login', clientVersion, agentId: agentId || null, toolsEnabled, desktopEnabled });
    }
    if (route === 'POST /native/warm') {
      if (!desktopEnabled && !toolsEnabled) return send(403, {ok: false, error: 'desktop_disabled'});
      if (warmFlight) return send(200, {ok: true, state: 'in_flight', ms: 0});
      const started = Date.now();
      logStream('warm', 'start', {});
      const run = typeof sandbox.warm === 'function' ? sandbox.warm() : Promise.resolve();
      warmFlight = Promise.resolve(run).then(
        () => {
          const ms = Date.now() - started;
          logStream('warm', 'ready', {ms});
          return {ok: true, state: 'ready', ms};
        },
        () => {
          const ms = Date.now() - started;
          logStream('warm', 'ready', {ms, state: 'failed'});
          return {ok: true, state: 'failed', ms};
        },
      ).finally(() => { warmFlight = null; });
      return send(200, await warmFlight);
    }
    if (route === 'GET /native/health') {
      if (!desktopEnabled && !toolsEnabled) return send(403, {ok: false, error: 'desktop_disabled'});
      try { return send(200, await sandbox.probe()); }
      catch { return send(502, {ok: false, error: 'native_handshake_failed'}); }
    }
    const eventUrl = new URL(req.url, 'http://127.0.0.1');
    if (req.method === 'GET' && eventUrl.pathname === '/native/events') {
      if (!desktopEnabled && !toolsEnabled) return send(403, {ok: false, error: 'desktop_disabled'});
      if ([...eventUrl.searchParams.keys()].some(k => !['agentId', 'callId'].includes(k)) || !activeCall || eventUrl.searchParams.get('agentId') !== agentId || eventUrl.searchParams.get('callId') !== activeCall.callId) return send(403, {ok: false, error: 'call_not_allowed'});
      const context = activeCall, controller = new AbortController();
      streams.add(controller); res.on('close', () => controller.abort());
      let typedTask;
      try {
        const upstream = await sandbox.openEvents(controller.signal);
        logStream('box', 'open', {http_status: upstream.status, error_class: null});
        res.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
        res.write(': connected\n\n');
        typedTask = (async () => {
          while (activeCall === context && !controller.signal.aborted) {
            try {
              for await (const event of transcript.events(context, controller.signal, status => logStream('typed', 'open', {http_status: status, error_class: null}))) {
                if (activeCall !== context || controller.signal.aborted) break;
                if (!res.write('data: ' + JSON.stringify(event) + '\n\n')) throw new Error('slow_consumer');
              }
              if (!controller.signal.aborted) logStream('typed', 'eof', {http_status: null, error_class: null});
            } catch (error) {
              if (controller.signal.aborted) break;
              if (error?.message === 'slow_consumer') {
                logStream('downstream', 'slow_consumer', {http_status: null, error_class: 'Error'});
                controller.abort();
                break;
              }
              logStream('typed', 'error', streamFailure(error, 'gbot_voice_session_bridge.mjs:typed', {step: 'typed_stream', target_host: error?.target_host || BACKEND_HOST}));
              // A routine socket close is the same clock as the box stream. Don't sit on the 5s timer.
              if (isRoutineStreamEnd(error)) {
                await waitForNativeRetry(0, controller.signal);
                continue;
              }
            }
            await waitForNativeRetry(nativeRetryDelayMs, controller.signal);
          }
        })();
        let pending = '';
        const decoder = new TextDecoder();
        const reader = upstream.body?.getReader?.();
        if (!reader) throw new TypeError('terminated');
        try {
        while (true) {
          const next = await reader.read();
          if (next.done || activeCall !== context) break;
          const chunk = asBytes(next.value);
          if (!chunk || chunk.byteLength === 0) continue;
          pending += decoder.decode(chunk, {stream: true}).replace(/\r\n/g, '\n');
          if (pending.length > 2 * 1024 * 1024) throw new Error('event_too_large');
          let end;
          while ((end = pending.indexOf('\n\n')) !== -1) {
            const block = pending.slice(0, end); pending = pending.slice(end + 2);
            const data = block.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('\n');
            if (!data) {
              // Preserve upstream heartbeat liveness without forwarding contents.
              if (block.split('\n').some(line => line.startsWith(':')) && !res.write(': upstream\n\n')) throw new Error('slow_consumer');
              continue;
            }
            let event;
            let parsed;
            try {
              parsed = JSON.parse(data);
              const id = block.split('\n').find(line => line.startsWith('id:'))?.slice(3).trim();
              event = filterNativeEvent(id ? {...parsed, eventId: id} : parsed, context);
              const kind = NUDGE_KINDS.has(parsed?.payload?.kind) ? parsed.payload.kind : 'other';
              const channel = typeof parsed?.channel === 'string' && /^[a-z0-9-]{1,40}$/.test(parsed.channel) ? parsed.channel : 'other';
              if (!context.notedChannels) context.notedChannels = new Set();
              const firstOther = channel !== 'voice-call-nudge' && !context.notedChannels.has(channel);
              if (firstOther && context.notedChannels.size < 30) context.notedChannels.add(channel);
              if (channel === 'voice-call-nudge' || firstOther) {
                logStream('box', 'event', {
                  channel, kind, forwarded: event?.channel === 'box-signal',
                  event_id: typeof id === 'string' && id.length > 0 && id.length <= 80 ? id : null,
                });
              }
            } catch { continue; }
            if (event?.channel === 'typed-message') event = null;
            if (event && !res.write('data: ' + JSON.stringify(event) + '\n\n')) throw new Error('slow_consumer');
            if (context.seen.size > 2000) context.seen.delete(context.seen.values().next().value);
          }
        }
        } finally { reader.releaseLock?.(); }
        if (!controller.signal.aborted) logStream('box', 'eof', {http_status: null, error_class: null});
      } catch (error) {
        if (!controller.signal.aborted) {
          logStream('box', 'error', streamFailure(error, 'gbot_voice_session_bridge.mjs:box', {step: 'box_stream', target_host: error?.target_host || sandbox.gatewayHost?.()}));
          // undici raises TypeError "terminated" / UND_ERR_SOCKET when the pod closes
          // the SSE socket on its clock. The cached sandbox is still the right one.
          // 417 and the other stale statuses still re-ensure inside #fetch.
          if (error?.name === 'TypeError' && !isRoutineStreamEnd(error)) sandbox.invalidate?.();
        }
        if (!res.headersSent) send(502, {ok: false, error: 'native_stream_failed'});
      } finally {
        controller.abort();
        streams.delete(controller);
        // End downstream first: the gateway reconnects (and replays the gap) on EOF,
        // and must not sit on its 60s read timeout while the transcript loop stops.
        res.end();
        const stopping = Date.now();
        let slowLogged = false;
        const slow = setTimeout(() => { slowLogged = true; logStream('typed', 'slow_stop', {http_status: null, error_class: null}); }, 5000);
        await typedTask;
        clearTimeout(slow);
        if (slowLogged) logStream('typed', 'stopped', {ms: Date.now() - stopping});
      }
      return;
    }
    const nativePresence = route === 'POST /native/presence';
    const method = nativePresence ? 'setVoiceCallPresence' : RPC_ROUTES[route];
    if (!method) return send(404, { ok: false, error: 'route_not_found' });
    if ((route === 'POST /tool' || nativePresence) && !toolsEnabled && !desktopEnabled) return send(403, { ok: false, error: 'tools_disabled' });
    try {
      let raw = '';
      for await (const chunk of req) { raw += chunk; if (Buffer.byteLength(raw) > 65536) return send(413, { ok: false, error: 'body_too_large' }); }
      if (req.method === 'POST' && req.headers['content-type']?.split(';')[0] !== 'application/json') return send(415, { ok: false, error: 'json_required' });
      let body;
      try {
        const parsed = raw ? JSON.parse(raw) : {};
        if (nativePresence) {
          if (Object.keys(parsed).some(k => !['agentId', 'callId', 'isOnTheLine', 'acceptsOverheard', 'sessionId'].includes(k)) || typeof parsed.agentId !== 'string' || typeof parsed.callId !== 'string' || !/^[A-Za-z0-9_-]{1,120}$/.test(parsed.callId) || typeof parsed.isOnTheLine !== 'boolean' || typeof parsed.acceptsOverheard !== 'boolean' || (parsed.sessionId !== undefined && parsed.sessionId !== sessionId)) throw new Error('invalid_body');
          body = parsed;
        } else body = validateBody(route, parsed);
      } catch { return send(400, { ok: false, error: 'invalid_body' }); }
      if (req.method === 'POST' && (!agentId || body.agentId !== agentId)) return send(403, { ok: false, error: 'agent_not_allowed' });
      if (route === 'POST /tool') {
        if (!toolsEnabled && !['send_task', 'search_conversations'].includes(body.name)) return send(403, {ok: false, error: 'tools_disabled'});
        if (!activeCall || body.callId !== activeCall.callId) return send(403, {ok: false, error: 'call_not_allowed'});
      }
      if (nativePresence) {
        if (presenceBusy) return send(409, {ok: false, error: 'presence_busy'});
        if (!activeCall && !body.isOnTheLine) return send(403, {ok: false, error: 'call_not_allowed'});
        let stale = null;
        if (activeCall && body.callId !== activeCall.callId) {
          // A foreign callId may only hang up its own call; never clear someone else's.
          if (!body.isOnTheLine) return send(409, {ok: false, error: 'call_conflict'});
          // New gateway session (new callId) taking over a call this process still has.
          for (const controller of streams) controller.abort();
          stale = activeCall;
          activeCall = null;
        } else if (!activeCall && body.isOnTheLine) {
          // SIGKILL of the gateway or SIGTERM of this helper drops the in-memory call.
          // The call id on disk is the one to hang up before the new call registers.
          const stored = loadCall(callStore);
          if (stored && stored.callId !== body.callId) stale = stored;
        }
        presenceBusy = true;
        try {
        // HTTP 417 is a hibernated box, not a second voice call. Hanging up still
        // matters for a call left on the line after -9/-15. The hangup itself
        // re-ensures when the cached pod answers 417.
        if (stale) {
          try {
            await sandbox.presence({agentId: stale.agentId, callId: stale.callId, isOnTheLine: false, acceptsOverheard: stale.acceptsOverheard});
            logStream('presence', 'stale_hangup', {http_status: 200, error_class: null, step: 'presence_hangup'});
          } catch (error) {
            const failure = hangupFailure(error);
            logStream('presence', 'stale_hangup', failure.fields);
            // Only a box 404 on the hang-up itself means Grok no longer has this
            // call. An EnsureSandBox failure or an edge 404 (body_kind other)
            // leaves the stored id, so a later attempt can still hang it up.
            const boxGone = failure.httpStatus === 404
              && failure.fields.step === 'presence_hangup'
              && (failure.fields.body_kind === 'box_not_found' || failure.fields.body_kind === 'box_unknown_method');
            if (boxGone) forgetCall(callStore);
          }
        }
        const authId = await sandbox.callerAuthId();
        await sandbox.presence(body);
        if (body.isOnTheLine) {
          if (!activeCall) {
            activeCall = {agentId, callId: body.callId, acceptsOverheard: body.acceptsOverheard, sessionId, authId, sinceMs: Date.now(), seen: new Set()};
            logStream('presence', 'call_start', {call_id: body.callId});
          } else activeCall.acceptsOverheard = body.acceptsOverheard;
          rememberCall(callStore, activeCall);
        } else {
          activeCall = null; for (const controller of streams) controller.abort();
          forgetCall(callStore);
        }
        return send(200, {ok: true});
        } finally { presenceBusy = false; }
      }
      const result = await rpc(method, body);
      if (route === 'GET /credential') return send(200, { ok: true, credential: {
        clientSecret: result.clientSecret, expiresAtUnixSeconds: Number(result.expiresAtUnixSeconds),
        model: result.model, websocketUrl: result.websocketUrl,
      } });
      if (route === 'POST /tool') {
        const kinds = { VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_SERVED: 'served', VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_UNKNOWN_TOOL: 'unknown_tool', VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_INVALID_INPUT: 'invalid_input', 1: 'served', 2: 'unknown_tool', 3: 'invalid_input' };
        return send(200, { ...result, kind: kinds[result.kind] ?? result.kind });
      }
      return send(200, result);
    } catch (error) {
      const safe = /^(upstream_http_\d{3}|login_unavailable|login_expired_open_grok_bot|unsupported_storage|client_version_unavailable)$/.test(error.message) ? error.message : 'bridge_request_failed';
      const payload = { ok: false, error: safe };
      if (ACCOUNT_FAILURE_REASONS.has(error?.reason)) payload.reason = error.reason;
      if (STEPS.has(error?.step)) payload.step = error.step;
      if (BODY_KINDS.has(error?.body_kind)) payload.body_kind = error.body_kind;
      if (nativePresence) logStream('presence', 'error', streamFailure(error, 'gbot_voice_session_bridge.mjs:presence', {step: 'presence', target_host: error?.target_host}));
      return send(502, payload);
    }
  });
  server.hangupActiveCall = hangupActiveCall;
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const port = Number(process.env.GBOT_VOICE_BRIDGE_PORT || 18771);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('invalid_port');
  const callStore = join(homedir(), 'Library', 'Logs', 'groki-native-call.json');
  const server = createBridge({ port, callStore });
  server.requestTimeout = 25000;
  let shuttingDown = false;
  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    Promise.resolve(server.hangupActiveCall()).finally(() => server.close(() => process.exit(0)));
  };
  process.once('SIGTERM', shutdown);
  process.once('SIGINT', shutdown);
  // A previous helper exited -15 without this handler, or the gateway was -9.
  // Hang that call up (a 417 here re-ensures the asleep pod) before listening.
  server.hangupActiveCall().finally(() => {
    server.listen(port, '127.0.0.1', () => console.log(`Grok Bot voice session bridge listening on 127.0.0.1:${port}`));
  });
}
