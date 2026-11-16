// SPDX-FileCopyrightText: 2026 Joseph Zhou
// SPDX-License-Identifier: MIT
import test from 'node:test';
import assert from 'node:assert/strict';
import { createBridge, accountRpc, createEnsureBackoff, createInstalledClientVersion, filterNativeEvent, parseClientVersion, validateBody, isRoutineStreamEnd } from './gbot_voice_session_bridge.mjs';
import { createServer } from 'node:net';
import { request } from 'node:http';
import { existsSync, mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

async function freePort() {
  const s = createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r));
  const port = s.address().port; await new Promise(r => s.close(r)); return port;
}

test('loopback bridge rejects cross origin, wrong host, unknown RPC without consulting login', async () => {
  let calls = 0; const port = await freePort();
  const server = createBridge({port, agentId: 'a', rpc: async (method, body) => { calls++; return { instructions: 'test', method, body }; }});
  await new Promise(r => server.listen(port, '127.0.0.1', r));
  try {
    const url = `http://127.0.0.1:${port}`;
    assert.equal((await fetch(url + '/credential', {headers:{Origin:'https://example.com'}})).status, 403);
    const wrongHostStatus = await new Promise((resolve, reject) => { const req = request(url + '/credential', {headers:{Host:`localhost:${port}`}}, res => {res.resume(); resolve(res.statusCode);}); req.on('error', reject); req.end(); });
    assert.equal(wrongHostStatus, 403);
    assert.equal((await fetch(url + '/api/sendPrompt', {method:'POST'})).status, 404);
    assert.equal((await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"a","agentName":"b","rpc":"DeleteAgent"}'})).status, 400);
    assert.equal((await fetch(url + '/tool', {method:'POST'})).status, 403);
    assert.equal((await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"other","agentName":"b"}'})).status, 403);
    assert.equal(calls, 0);
    const response = await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"a","agentName":"b"}'});
    assert.equal(response.status, 200); assert.equal((await response.json()).method, 'VoiceCallHarnessSession'); assert.equal(calls, 1);
  } finally { await new Promise(r => server.close(r)); }
});

test('account RPC admits only fixed methods and never leaks upstream response body', async () => {
  let loads = 0; const accountLoader = () => {loads++; return {token:'FAKE_TEST',machineId:'FAKE_MACHINE'};};
  await assert.rejects(accountRpc('SendPrompt', {}, {accountLoader}), /rpc_forbidden/); assert.equal(loads, 0);
  const fetcher = async (url, options) => {
    assert.equal(url, 'https://api2.cursor.sh/aiserver.v1.GrokBotService/MintSandVoiceCallSecret');
    assert.equal(options.redirect, 'error'); return {ok:false,status:403,json:async()=>({token:'DO_NOT_LEAK'})};
  };
  await assert.rejects(accountRpc('MintSandVoiceCallSecret', {}, {accountLoader, fetcher, clientVersion: '0.79.3'}), /^Error: upstream_http_403$/);
});

test('account RPC 401 and 403 are login_rejected and EnsureSandBox backs off until success or a new secrets file', async () => {
  const forbidden = async () => ({ok: false, status: 403, json: async () => ({token: 'DO_NOT_LEAK'})});
  await assert.rejects(
    accountRpc('MintSandVoiceCallSecret', {}, {accountLoader: () => ({token: 'FAKE_TEST', machineId: 'FAKE_MACHINE'}), fetcher: forbidden, clientVersion: '0.79.3'}),
    (error) => error.message === 'upstream_http_403' && error.reason === 'login_rejected',
  );
  assert.equal(parseClientVersion('0.79.3'), '0.79.3');
  assert.equal(parseClientVersion('0.79.3-dev'), '0.79.3');
  assert.equal(parseClientVersion('0.71.2'), '0.71.2');
  assert.equal(parseClientVersion('sand'), null);
  let now = 1_000_000;
  let mtime = 10;
  let calls = 0;
  const backoff = createEnsureBackoff({now: () => now, secretsMtime: () => mtime, startMs: 5000, maxMs: 60000});
  const accountLoader = () => ({token: 'FAKE_TEST', machineId: 'FAKE_MACHINE'});
  const fetcher = async (_url, options) => {
    calls++;
    assert.equal(options.headers['x-cursor-client-version'], '0.79.3');
    assert.equal(options.headers['x-cursor-client-version'] === '0.71.2', false);
    const status = calls === 4 ? 200 : (calls === 3 ? 417 : 401);
    return {ok: status === 200, status, json: async () => ({ok: true})};
  };
  const opts = {accountLoader, fetcher, clientVersion: '0.79.3', backoff};
  const body = {wake: false, supportsAsleepBox: true};
  await assert.rejects(accountRpc('EnsureSandBox', body, opts), (error) => error.message === 'upstream_http_401' && error.reason === 'login_rejected');
  assert.equal(calls, 1);
  now += 4999;
  await assert.rejects(accountRpc('EnsureSandBox', body, opts), (error) => error.reason === 'login_rejected');
  assert.equal(calls, 1);
  now += 1;
  await assert.rejects(accountRpc('EnsureSandBox', body, opts), (error) => error.message === 'upstream_http_401' && error.reason === 'login_rejected');
  assert.equal(calls, 2);
  // 417 is the asleep box. It is not a rejected login and it does not extend the wait.
  now += 10000;
  await assert.rejects(accountRpc('EnsureSandBox', body, opts), (error) => error.message === 'upstream_http_417' && error.reason === undefined);
  assert.equal(calls, 3);
  await accountRpc('EnsureSandBox', body, opts);
  assert.equal(calls, 4);
  await assert.rejects(accountRpc('EnsureSandBox', body, opts), (error) => error.reason === 'login_rejected');
  assert.equal(calls, 5);
  now += 1000;
  mtime = 11;
  await assert.rejects(accountRpc('EnsureSandBox', body, opts), (error) => error.reason === 'login_rejected');
  assert.equal(calls, 6);
});

test('the update hint is client_update_required for 401 and 403, and a missing hint stays login_rejected', async () => {
  const accountLoader = () => ({token: 'FAKE_TEST', machineId: 'FAKE_MACHINE'});
  const body = {wake: false, supportsAsleepBox: true};
  const cases = [
    [401, 'SAND_CLIENT_UPDATE_REQUIRED', 'client_update_required'],
    [403, 'SAND_CLIENT_UPDATE_REQUIRED', 'client_update_required'],
    [401, null, 'login_rejected'],
    [403, null, 'login_rejected'],
  ];
  for (const [status, hint, reason] of cases) {
    let bodyReads = 0;
    let now = 1_000;
    let calls = 0;
    const backoff = createEnsureBackoff({now: () => now, secretsMtime: () => 1, startMs: 5000, maxMs: 60000});
    const fetcher = async () => {
      calls++;
      return {
        ok: false,
        status,
        headers: {get: (name) => name === 'x-automation-failure-hint' ? hint : null},
        json: () => { bodyReads++; return {secret: 'DO_NOT_LEAK'}; },
        text: () => { bodyReads++; return 'DO_NOT_LEAK'; },
      };
    };
    const opts = {accountLoader, fetcher, clientVersion: '0.71.2', backoff};
    await assert.rejects(accountRpc('EnsureSandBox', body, opts), (error) => error.message === `upstream_http_${status}` && error.reason === reason);
    now += 1000;
    await assert.rejects(accountRpc('EnsureSandBox', body, opts), (error) => error.reason === reason);
    assert.equal(calls, 1);
    assert.equal(bodyReads, 0);
  }
});

test('the installed client version is read again only when the plist mtime changes', () => {
  let mtime = 10;
  let reads = 0;
  const version = createInstalledClientVersion({
    stat: () => mtime,
    read: () => { reads++; return reads === 1 ? '0.79.3' : '0.79.4-dev'; },
  });
  assert.equal(version(), '0.79.3');
  assert.equal(version(), '0.79.3');
  assert.equal(reads, 1);
  mtime = 11;
  assert.equal(version(), '0.79.4');
  assert.equal(version(), '0.79.4');
  assert.equal(reads, 2);
});

test('presence response names login_rejected and leaves an asleep box unnamed', async () => {
  const port = await freePort();
  let mode = 'login';
  const sandbox = {
    callerAuthId: async () => 'owner',
    presence: async () => {
      if (mode === 'login') {
        const error = new Error('upstream_http_401');
        error.reason = 'login_rejected';
        throw error;
      }
      throw new Error('upstream_http_417');
    },
  };
  const server = createBridge({port, agentId: 'a', desktopEnabled: true, sandbox});
  await new Promise(r => server.listen(port, '127.0.0.1', r));
  const post = () => fetch(`http://127.0.0.1:${port}/native/presence`, {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({agentId: 'a', callId: 'call-c', isOnTheLine: true, acceptsOverheard: false}),
  });
  try {
    const denied = await post();
    assert.equal(denied.status, 502);
    assert.deepEqual(await denied.json(), {ok: false, error: 'upstream_http_401', reason: 'login_rejected'});
    mode = 'asleep';
    const asleep = await post();
    assert.equal(asleep.status, 502);
    assert.deepEqual(await asleep.json(), {ok: false, error: 'upstream_http_417'});
  } finally { await new Promise(r => server.close(r)); }
});

test('tool invocation requires complete bounded line state', () => {
  assert.throws(() => validateBody('POST /tool', {agentId:'a',callId:'c',name:'x',input:{},line:{}}), /invalid_line/);
});

test('native bootstrap uses fixed read-only request and cancels bodies without reading snapshots', async () => {
  const {NativeSandboxClient, validateSandboxUrl} = await import('./gbot_voice_session_bridge.mjs');
  for (const url of ['http://a.cursor.sh', 'https://cursor.sh.evil.test', 'https://u@a.cursor.sh', 'https://a.cursor.sh/?token=x']) assert.throws(() => validateSandboxUrl(url));
  let cancelled = 0; const paths = [];
  const client = new NativeSandboxClient({accountLoader:()=>({authId:'owner'}), rpc:async(method,body)=>{
    assert.equal(method,'EnsureSandBox'); assert.deepEqual(body,{wake:false,supportsAsleepBox:true});
    return {gatewayUrl:'https://pod.cursorvm.com',gatewayToken:'FAKE',networkToken:'FAKE_NETWORK'};
  },fetcher:async(url,options)=>{
    paths.push(new URL(url).pathname); assert.equal(options.redirect,'error');
    return {ok:true,status:200,headers:new Headers({'content-type':'text/event-stream'}),body:{cancel:async()=>{cancelled++;}},json:()=>assert.fail('must not read body')};
  }});
  const result = await client.probe(); assert.equal(result.ok,true); assert.deepEqual(paths,['/health','/events']); assert.equal(cancelled,2);
  assert.equal(JSON.stringify(result).includes('FAKE'),false);
  paths.length = 0;
  await client.warm();
  assert.deepEqual(paths, ['/health']);
});

test('a stale sandbox connection is dropped and re-ensured once instead of failing forever', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  let ensures = 0; const seen = [];
  const outcomes = [
    () => { throw new TypeError('fetch failed'); },
    () => ({ok:true,status:200,body:{cancel:async()=>{}}}),
    () => ({ok:false,status:502,body:{cancel:async()=>{}}}),
    () => ({ok:true,status:200,body:{cancel:async()=>{}}}),
    () => ({ok:false,status:401,body:{cancel:async()=>{}}}),
    () => ({ok:false,status:401,body:{cancel:async()=>{}}}),
    () => ({ok:false,status:400,body:{cancel:async()=>{}}}),
  ];
  const client = new NativeSandboxClient({accountLoader:()=>({authId:'owner'}), rpc:async()=>{
    ensures++;
    return {gatewayUrl:`https://pod${ensures}.cursorvm.com`,gatewayToken:'FAKE'};
  },fetcher:async(url)=>{ seen.push(new URL(url).host); return outcomes.shift()(); }});
  const call = {agentId:'a',callId:'c',isOnTheLine:true,acceptsOverheard:false};
  await client.presence(call);
  assert.deepEqual(seen, ['pod1.cursorvm.com','pod2.cursorvm.com']);
  await client.presence(call);
  assert.equal(ensures, 3);
  await assert.rejects(client.presence(call), /upstream_http_401/);
  assert.equal(ensures, 4);
  // A 400 is the request, not the sandbox: no re-ensure.
  await assert.rejects(client.presence(call), /upstream_http_400/);
  assert.equal(ensures, 5);
  assert.equal(seen.length, 7);
});

test('native event filtering excludes history, other callers, sessions and duplicate entries', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',sessionId:'',authId:'owner',sinceMs:100,seen:new Set()};
  const entry = {id:'e',kind:'message',role:'user',content:'FAKE TEXT',timestampMs:101,author:{kind:'cursor_user',id:'owner'}};
  const event = {channel:'transcript',payload:{type:'appended',entry,ordered:{replicaKey:'transcript:a',sequence:1}}};
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,type:'snapshot'}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,entry:{...entry,author:{kind:'cursor_user',id:'other'}}}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,entry:{...entry,timestampMs:99}}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,ordered:{replicaKey:'transcript:a\0other',sequence:1}}},context),null);
  assert.equal(filterNativeEvent(event,context).channel,'typed-message'); assert.equal(filterNativeEvent(event,context),null);
  assert.equal(filterNativeEvent({channel:'voice-call-nudge',payload:{agentId:'a',callId:'other',kind:'outcome',update:'fake'}},context),null);
  const reply = {channel:'transcript',payload:{type:'appended',entry:{id:'box1',kind:'send-message',timestampMs:150,message:{type:'text',channel:'voice:call-c',content:'Saturday market'}},ordered:{replicaKey:'transcript:a',sequence:2}}};
  assert.equal(filterNativeEvent(reply,context).channel,'box-reply');
  assert.equal(filterNativeEvent(reply,context),null);
  assert.equal(filterNativeEvent({...reply,payload:{...reply.payload,entry:{...reply.payload.entry,id:'other-call',message:{type:'text',channel:'voice:someone-else',content:'no'}}}},context),null);
});

test('desktop opt-in admits native send and search scoped to active call while unknown tools remain off', async () => {
  const port = await freePort(); let toolCalls=0, presenceCalls=0;
  const sandbox={callerAuthId:async()=> 'owner', presence:async()=>{presenceCalls++;}};
  const server=createBridge({port,agentId:'a',desktopEnabled:true,toolsEnabled:false,sandbox,rpc:async()=>{toolCalls++;return {kind:1};}});
  await new Promise(r=>server.listen(port,'127.0.0.1',r));
  const post=async(path,body)=>fetch(`http://127.0.0.1:${port}`+path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const tool={agentId:'a',callId:'call-c',name:'send_task',input:{request:'FAKE'},line:{theyJustTalked:false,newsOwed:false,receiptOwed:false,alreadySpokeThisTurn:false}};
  try {
    assert.equal((await post('/tool',tool)).status,403);
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-c',isOnTheLine:true,acceptsOverheard:false})).status,200);
    assert.equal((await post('/tool',{...tool,name:'unknown_native_tool'})).status,403);
    assert.equal((await post('/tool',{...tool,name:'search_conversations',input:{query:'fixture'}})).status,200);
    assert.equal((await post('/tool',{...tool,callId:'other'})).status,403);
    assert.equal((await post('/tool',tool)).status,200); assert.equal(toolCalls,2);
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-c',isOnTheLine:false,acceptsOverheard:false})).status,200);
    assert.equal((await post('/tool',tool)).status,403); assert.equal(presenceCalls,2);
  } finally {await new Promise(r=>server.close(r));}
});

test('server transcript stream drops replay and pre-connection entries and requires rebaseline after clear', async()=>{
  const {filterTranscriptFrame,NativeTranscriptClient}=await import('./gbot_voice_session_bridge.mjs');
  const context={agentId:'a',callId:'call-c',sessionId:'',authId:'owner',sinceMs:100,seen:new Set()};
  const state={};
  assert.deepEqual(filterTranscriptFrame({connected:{serverTimeMs:'110'}},context,state),[]);
  const row=(time,id='e')=>({body:Buffer.from(JSON.stringify({id,kind:'message',role:'user',content:'FAKE',timestampMs:time,author:{kind:'cursor_user',id:'owner'}})).toString('base64')});
  const rows={agentId:'a',sessionId:'',generation:1,entries:[row(120)]};
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,replay:true}},context,state),[]);
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,entries:[row(109)]}},context,state),[]);
  assert.equal(filterTranscriptFrame({rows},context,state).length,1);
  assert.deepEqual(filterTranscriptFrame({rows},context,state),[]);
  assert.throws(()=>filterTranscriptFrame({cleared:{agentId:'a',newGeneration:2}},context,state),/rebaseline/);
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,entries:[row(130,'e2')]}},context,state),[]);
  let fetched=false;
  const client=new NativeTranscriptClient({fetcher:async()=>{fetched=true;},accountLoader:()=>({})});
  await assert.rejects(async()=>{for await(const ignored of client.events({...context,sessionId:'unverified'})) {}},/session_binding/);
  assert.equal(fetched,false);
});

test('transcript stream stops on abort even when the fetch and its body ignore it', async () => {
  const {NativeTranscriptClient} = await import('./gbot_voice_session_bridge.mjs');
  const hello = Buffer.from(JSON.stringify({connected:{serverTimeMs:1}}));
  const prefix = Buffer.alloc(5); prefix.writeUInt32BE(hello.length, 1);
  let sent = false;
  // Neither the fetch nor the body source honours abort or cancel: the read only ends if we cancel it.
  const body = new ReadableStream({
    pull(controller) { if (!sent) { sent = true; controller.enqueue(Buffer.concat([prefix, hello])); return; } return new Promise(() => {}); },
    cancel() { return new Promise(() => {}); },
  });
  const client = new NativeTranscriptClient({accountLoader: () => ({token:'t', machineId:'m'}),
    fetcher: async () => new Response(body, {headers:{'content-type':'application/connect+json'}})});
  const controller = new AbortController();
  const started = Date.now();
  const done = (async () => { for await (const ignored of client.events({agentId:'a', callId:'c', sessionId:'', sinceMs:0}, controller.signal)) {} })();
  await new Promise(resolve => setTimeout(resolve, 50));
  controller.abort();
  const result = await Promise.race([done.then(() => 'ended'), new Promise(resolve => setTimeout(() => resolve('hung'), 1000))]);
  assert.equal(result, 'ended');
  assert.ok(Date.now() - started < 1000);
});

test('a nudge is a signal with no answer text, even when the text matches', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',seen:new Set()};
  const event = {channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'SAME FIXTURE RESULT'}};
  const first = filterNativeEvent(event,context);
  const second = filterNativeEvent(event,context);
  assert.equal(first.channel,'box-signal');
  assert.equal(second.channel,'box-signal');
  assert.equal(first.payload.update, undefined);
  assert.equal(JSON.stringify(first).includes('SAME FIXTURE'), false);
});

test('native event IDs deduplicate replays without conflating separate same-text results', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',seen:new Set()};
  const event = {eventId:'event-1',channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'SAME FIXTURE RESULT'}};
  assert.ok(filterNativeEvent(event,context));
  assert.equal(filterNativeEvent(event,context),null);
  assert.ok(filterNativeEvent({...event,eventId:'event-2'},context));
});

test('HTTP: typed EOF or error reconnects independently and box results still arrive', async () => {
  for (const failure of ['eof', 'error']) {
    const port = await freePort();
    let box, attempts = 0, typedCancelled = false;
    const logs = [];
    const sandbox = {
      presence: async () => {}, callerAuthId: async () => 'owner',
      openEvents: async signal => new Response(new ReadableStream({start(controller) {
        box = controller;
        signal.addEventListener('abort', () => controller.close(), {once:true});
      }}), {headers:{'content-type':'text/event-stream'}}),
    };
    const transcript = {async *events(_context, signal, onOpen) {
      attempts++; onOpen(200);
      if (attempts === 1) {
        if (failure === 'error') throw new TypeError('SECRET_FIXTURE');
        return;
      }
      await new Promise(resolve => signal.addEventListener('abort', resolve, {once:true}));
      typedCancelled = true;
    }};
    const server = createBridge({port, agentId:'a', desktopEnabled:true, toolsEnabled:false, sandbox, transcript, nativeRetryDelayMs:10, nativeLog:event=>logs.push(event)});
    await new Promise(resolve=>server.listen(port,'127.0.0.1',resolve));
    let reader;
    try {
      await fetch(`http://127.0.0.1:${port}/native/presence`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({agentId:'a',callId:'call-c',isOnTheLine:true,acceptsOverheard:false})});
      const response=await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-c`);
      reader=response.body.getReader(); await reader.read();
      await new Promise(resolve=>setTimeout(resolve,30));
      assert.ok(attempts>=2);
      box.enqueue(new TextEncoder().encode('data: '+JSON.stringify({channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'FIXTURE RESULT'}})+'\n\n'));
      const result=await reader.read();
      assert.equal(result.done,false);
      const body = new TextDecoder().decode(result.value);
      assert.match(body, /box-signal/);
      assert.equal(body.includes('FIXTURE RESULT'), false);
      assert.ok(logs.some(event=>event.source==='typed'&&event.state===failure));
      assert.equal(JSON.stringify(logs).includes('SECRET_FIXTURE'),false);
      await reader.cancel(); reader=null;
      await new Promise(resolve=>setTimeout(resolve,20));
      assert.equal(typedCancelled,true);
    } finally {await reader?.cancel();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
  }
});

test('presence with a new callId on the line takes over a stale call; foreign hangup still conflicts', async () => {
  const port = await freePort(); const presence=[]; const logs=[];
  const sandbox={callerAuthId:async()=> 'owner', presence:async body=>{presence.push(`${body.callId}:${body.isOnTheLine}`);}};
  const server=createBridge({port,agentId:'a',desktopEnabled:true,toolsEnabled:false,sandbox,rpc:async()=>({kind:1}),nativeLog:event=>logs.push(event)});
  await new Promise(r=>server.listen(port,'127.0.0.1',r));
  const post=async(path,body)=>fetch(`http://127.0.0.1:${port}`+path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const pres=(callId,isOnTheLine)=>post('/native/presence',{agentId:'a',callId,isOnTheLine,acceptsOverheard:false});
  const tool=callId=>post('/tool',{agentId:'a',callId,name:'send_task',input:{request:'FAKE'},line:{theyJustTalked:false,newsOwed:false,receiptOwed:false,alreadySpokeThisTurn:false}});
  try {
    assert.equal((await pres('call-old',true)).status,200);
    assert.equal((await tool('call-old')).status,200);
    assert.equal((await pres('call-new',true)).status,200);
    assert.equal((await tool('call-new')).status,200);
    const old=await tool('call-old'); assert.equal(old.status,403); assert.equal((await old.json()).error,'call_not_allowed');
    const foreign=await pres('call-old',false); assert.equal(foreign.status,409); assert.equal((await foreign.json()).error,'call_conflict');
    assert.equal((await tool('call-new')).status,200);
    assert.deepEqual(presence,['call-old:true','call-old:false','call-new:true']);
    assert.deepEqual(logs.filter(e=>e.state==='stale_hangup').map(e=>e.http_status),[200]);
  } finally {await new Promise(r=>server.close(r));}
});

test('a failed stale hangup is logged and the new call still registers', async () => {
  const port = await freePort(); const presence=[]; const logs=[];
  const sandbox={callerAuthId:async()=> 'owner', presence:async body=>{
    presence.push(`${body.callId}:${body.isOnTheLine}`);
    if (!body.isOnTheLine) throw new Error('upstream_http_502');
  }};
  const server=createBridge({port,agentId:'a',desktopEnabled:true,toolsEnabled:false,sandbox,rpc:async()=>({kind:1}),nativeLog:event=>logs.push(event)});
  await new Promise(r=>server.listen(port,'127.0.0.1',r));
  const pres=(callId,isOnTheLine)=>fetch(`http://127.0.0.1:${port}/native/presence`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({agentId:'a',callId,isOnTheLine,acceptsOverheard:false})});
  try {
    assert.equal((await pres('call-old',true)).status,200);
    assert.equal((await pres('call-new',true)).status,200);
    assert.deepEqual(presence,['call-old:true','call-old:false','call-new:true']);
    const stale = logs.find(e=>e.state==='stale_hangup');
    assert.equal(stale.error_class, 'upstream_http_502');
    assert.equal(stale.http_status, 502);
    assert.equal(stale.step, 'presence_hangup');
    assert.match(stale.at, /^\d{4}-\d{2}-\d{2}T/);
  } finally {await new Promise(r=>server.close(r));}
});

test('HTTP 417 drops the cached sandbox and wakes it with a fresh EnsureSandBox', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  let ensures = 0;
  const bodies = [];
  const client = new NativeSandboxClient({
    connectMode: 'normal',
    accountLoader: () => ({authId: 'owner'}),
    rpc: async (method, body) => {
      assert.equal(method, 'EnsureSandBox');
      bodies.push(body);
      ensures++;
      return {gatewayUrl: `https://pod${ensures}.cursorvm.com`, gatewayToken: 'FAKE'};
    },
    fetcher: async () => ensures === 1
      ? {ok: false, status: 417, body: {cancel: async () => {}}}
      : {ok: true, status: 200, body: {cancel: async () => {}}},
  });
  await client.presence({agentId: 'a', callId: 'c', isOnTheLine: true, acceptsOverheard: false});
  assert.equal(ensures, 2);
  assert.deepEqual(bodies, [{}, {}]);
});

test('a non-routine box TypeError still drops the sandbox', async () => {
  const port = await freePort();
  let invalidated = 0;
  const sandbox = {
    invalidate: () => { invalidated++; },
    presence: async () => {},
    callerAuthId: async () => 'owner',
    openEvents: async () => new Response(new ReadableStream({start(controller) {
      controller.error(new TypeError('socket hang up'));
    }}), {headers: {'content-type': 'text/event-stream'}}),
  };
  const server = createBridge({
    port, agentId: 'a', desktopEnabled: true, toolsEnabled: false, sandbox,
    transcript: {async *events() {}}, nativeLog: () => {},
  });
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  try {
    await fetch(`http://127.0.0.1:${port}/native/presence`, {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({agentId: 'a', callId: 'call-c', isOnTheLine: true, acceptsOverheard: false}),
    });
    await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-c`).then(r => r.text()).catch(() => {});
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(invalidated, 1);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});

test('a terminated box stream is logged and does not drop the sandbox', async () => {
  const port = await freePort();
  let invalidated = 0;
  const logs = [];
  const sandbox = {
    invalidate: () => { invalidated++; },
    presence: async () => {},
    callerAuthId: async () => 'owner',
    openEvents: async () => new Response(new ReadableStream({start(controller) {
      controller.error(new TypeError('terminated'));
    }}), {headers: {'content-type': 'text/event-stream'}}),
  };
  const server = createBridge({
    port, agentId: 'a', desktopEnabled: true, toolsEnabled: false, sandbox,
    transcript: {async *events() {}}, nativeLog: event => logs.push(event),
  });
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  try {
    assert.equal((await fetch(`http://127.0.0.1:${port}/native/presence`, {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({agentId: 'a', callId: 'call-c', isOnTheLine: true, acceptsOverheard: false}),
    })).status, 200);
    const response = await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-c`);
    await response.text().catch(() => {});
    await new Promise(resolve => setTimeout(resolve, 30));
    const error = logs.find(event => event.source === 'box' && event.state === 'error');
    assert.equal(error.error_class, 'TypeError');
    assert.equal(error.error_message, 'terminated');
    assert.equal(error.frame, 'gbot_voice_session_bridge.mjs:box');
    assert.match(error.at, /^\d{4}-\d{2}-\d{2}T/);
    assert.equal(invalidated, 0);
    assert.equal(isRoutineStreamEnd(Object.assign(new TypeError('terminated'), {cause: {code: 'UND_ERR_SOCKET'}})), true);
    assert.ok(logs.some(event => event.state === 'call_start' && event.call_id === 'call-c'));
    assert.equal(JSON.stringify(logs).includes('SECRET'), false);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});

test('a call left on disk is hung up before the next call registers', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'koji-call-'));
  const callStore = join(dir, 'call.json');
  const presence = [];
  const sandbox = {callerAuthId: async () => 'owner', presence: async body => { presence.push(`${body.callId}:${body.isOnTheLine}`); }};
  const firstPort = await freePort();
  const first = createBridge({port: firstPort, agentId: 'a', desktopEnabled: true, toolsEnabled: false, sandbox, callStore});
  await new Promise(resolve => first.listen(firstPort, '127.0.0.1', resolve));
  const pres = (port, callId, isOnTheLine) => fetch(`http://127.0.0.1:${port}/native/presence`, {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({agentId: 'a', callId, isOnTheLine, acceptsOverheard: false}),
  });
  try {
    assert.equal((await pres(firstPort, 'call-old', true)).status, 200);
    assert.equal(JSON.parse(readFileSync(callStore, 'utf8')).callId, 'call-old');
  } finally { await new Promise(resolve => first.close(resolve)); }
  const secondPort = await freePort();
  const second = createBridge({port: secondPort, agentId: 'a', desktopEnabled: true, toolsEnabled: false, sandbox, callStore});
  await new Promise(resolve => second.listen(secondPort, '127.0.0.1', resolve));
  try {
    assert.equal((await pres(secondPort, 'call-new', true)).status, 200);
    assert.deepEqual(presence, ['call-old:true', 'call-old:false', 'call-new:true']);
    assert.equal(JSON.parse(readFileSync(callStore, 'utf8')).callId, 'call-new');
    await second.hangupActiveCall();
    assert.deepEqual(presence.at(-1), 'call-new:false');
  } finally { await new Promise(resolve => second.close(resolve)); }
});

test('a progress nudge is only a signal, and the same event id is not forwarded twice', () => {
  const context = {agentId: 'a', callId: 'call-c', seen: new Set(), acceptsOverheard: false};
  const progress = {eventId: 'evt-1', channel: 'voice-call-nudge', payload: {agentId: 'a', callId: 'call-c', kind: 'progress', update: 'Warm and dry.'}};
  const signal = filterNativeEvent(progress, context);
  assert.equal(signal?.channel, 'box-signal');
  assert.equal(signal?.payload.kind, 'progress');
  assert.equal(signal?.payload.update, undefined);
  assert.equal(filterNativeEvent(progress, context), null);
  assert.equal(filterNativeEvent({channel: 'voice-call-nudge', payload: {agentId: 'a', callId: 'call-c', kind: 'ack', update: 'got it'}}, context), null);
});

test('a replayed transcript send-message is backfilled once and a user replay is not', async () => {
  const {filterTranscriptFrame} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a', callId:'call-c', sessionId:'', authId:'owner', sinceMs:100, seen: new Set()};
  const state = {};
  filterTranscriptFrame({connected:{serverTimeMs:'110'}}, context, state);
  const body = (entry) => Buffer.from(JSON.stringify(entry)).toString('base64');
  const reply = {id:'gap-1', kind:'send-message', timestampMs:50, message:{type:'text', channel:'voice:call-c', content:'From the gap'}};
  const user = {id:'old-user', kind:'message', role:'user', content:'earlier', timestampMs:200, author:{kind:'cursor_user', id:'owner'}};
  // Live replay rows carry only agentId, entries and replay (no sessionId, no generation).
  const rows = {agentId:'a', replay:true, entries:[{body: body(reply)}, {body: body(user)}]};
  const first = filterTranscriptFrame({rows}, context, state);
  assert.equal(first.length, 1);
  assert.equal(first[0].channel, 'box-reply');
  assert.equal(first[0].payload.text, 'From the gap');
  assert.equal(first[0].payload.entryId, 'gap-1');
  assert.deepEqual(filterTranscriptFrame({rows}, context, state), []);
});

test('the 16:52 replay frames as received backfill both gap replies, in order, and no request', async () => {
  const {filterTranscriptFrame} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a', callId:'40907d56', sessionId:'', authId:'owner', sinceMs:1794174777000, seen: new Set()};
  const state = {connected:false, sinceMs:context.sinceMs, generation:null};
  const body = (entry) => Buffer.from(JSON.stringify(entry)).toString('base64');
  const chan = 'voice:40907d56';
  const frames = [
    {connected:{streamId:'s', serverTimeMs:'1794174785984', absoluteLifetimeMs:'1182927'}},
    {agentState:{snapshot:true, client:[]}},
    {boxState:{state:{runState:'SAND_BOX_RUN_STATE_RUNNING'}}},
    {rows:{agentId:'a', replay:true, entries:[
      {body: body({id:'t43u', kind:'message', role:'user', channel:chan, content:'Find indoor events in Cary', timestampMs:1794174726958, author:{kind:'cursor_user', id:'owner'}})},
      {body: body({id:'t43s0', kind:'send-message', timestampMs:1794174734042, message:{type:'text', channel:chan, content:"I'm looking up indoor events in Cary now."}})},
      {body: body({id:'t43s1', kind:'send-message', timestampMs:1794174750739, message:{type:'text', channel:chan, content:'I found eight indoor options in Cary.'}})},
      {body: body({id:'t44s0', kind:'send-message', timestampMs:1794174761368, message:{type:'text', channel:'voice:other-call', content:'Not this call.'}})},
      {body: body({id:'t44s1', kind:'send-message', timestampMs:1794174761368, message:{type:'text', channel:chan, content:'Durham has a busy weekend.'}})},
    ]}},
  ];
  const out = frames.flatMap(frame => filterTranscriptFrame(frame, context, state));
  assert.deepEqual(out.map(event => event.channel), ['box-reply', 'box-reply', 'box-reply']);
  assert.deepEqual(out.map(event => event.payload.entryId), ['t43s0', 't43s1', 't44s1']);
  assert.equal(frames.flatMap(frame => filterTranscriptFrame(frame, context, state)).length, 0);
});

test('unversioned live rows pass box replies but never a typed user message', async () => {
  const {filterTranscriptFrame} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a', callId:'call-c', sessionId:'', authId:'owner', sinceMs:100, seen: new Set()};
  const state = {};
  filterTranscriptFrame({connected:{serverTimeMs:'110'}}, context, state);
  const body = (entry) => Buffer.from(JSON.stringify(entry)).toString('base64');
  const user = {id:'u1', kind:'message', role:'user', content:'typed', timestampMs:200, author:{kind:'cursor_user', id:'owner'}};
  const reply = {id:'r1', kind:'send-message', timestampMs:200, message:{type:'text', channel:'voice:call-c', content:'Answer'}};
  const out = filterTranscriptFrame({rows:{agentId:'a', entries:[{body: body(user)}, {body: body(reply)}]}}, context, state);
  assert.deepEqual(out.map(event => event.channel), ['box-reply']);
  assert.equal(filterTranscriptFrame({rows:{agentId:'a', generation:1, entries:[{body: body({...user, id:'u2'})}]}}, context, state).length, 1);
});

test('HTTP: a box error ends downstream at once even when the transcript loop is slow to stop', async () => {
  const port = await freePort();
  let box, release;
  const logs = [];
  const stuck = new Promise(resolve => { release = resolve; });
  const sandbox = {
    presence: async () => {}, callerAuthId: async () => 'owner', invalidate() {},
    openEvents: async () => new Response(new ReadableStream({start(controller) { box = controller; }}), {headers:{'content-type':'text/event-stream'}}),
  };
  // Ignores abort: models the 21:51:57 hold, where the gateway saw no EOF for 60s.
  const transcript = {async *events(_context, _signal, onOpen) { onOpen(200); await stuck; }};
  const server = createBridge({port, agentId:'a', desktopEnabled:true, toolsEnabled:false, sandbox, transcript, nativeRetryDelayMs:10, nativeLog:event=>logs.push(event)});
  await new Promise(resolve=>server.listen(port,'127.0.0.1',resolve));
  try {
    await fetch(`http://127.0.0.1:${port}/native/presence`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({agentId:'a',callId:'call-c',isOnTheLine:true,acceptsOverheard:false})});
    const response = await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-c`);
    const reader = response.body.getReader(); await reader.read();
    const started = Date.now();
    box.error(Object.assign(new TypeError('terminated'), {cause:{code:'UND_ERR_SOCKET'}}));
    let done = false;
    while (!done) ({done} = await reader.read());
    assert.ok(Date.now() - started < 1000);
    assert.ok(logs.some(event => event.source === 'box' && event.state === 'error'));
  } finally { release(); server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)); }
});

test('warm runs once while another warm is in flight, and the call record answer is returned', async () => {
  const port = await freePort();
  let warmStarted = 0;
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const sandbox = {
    callerAuthId: async () => 'owner',
    presence: async () => {},
    warm: () => { warmStarted += 1; return gate; },
  };
  const server = createBridge({port, agentId: 'a', desktopEnabled: true, toolsEnabled: false, sandbox});
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  const post = (path, body) => fetch(`http://127.0.0.1:${port}${path}`, {
    method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body),
  });
  try {
    const first = post('/native/warm', {});
    await new Promise(resolve => setTimeout(resolve, 20));
    const second = await post('/native/warm', {});
    assert.equal(second.status, 200);
    assert.equal((await second.json()).state, 'in_flight');
    assert.equal(warmStarted, 1);
    release();
    const done = await first;
    assert.equal((await done.json()).state, 'ready');
    assert.equal((await post('/native/presence', {agentId: 'a', callId: 'call-c', isOnTheLine: true, acceptsOverheard: false})).status, 200);
    assert.equal((await post('/native/call', {agentId: 'a', callId: 'call-c'})).status, 404);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

const SECRET = 'sk-LIVE-9f3c2a-DO-NOT-LEAK';

test('a 404 body is classified without keeping any other text', async () => {
  const {classifyNotFoundBody} = await import('./gbot_voice_session_bridge.mjs');
  const unknown = classifyNotFoundBody(`{"message":"unknown gateway method: setVoiceCallPresence","token":"${SECRET}"}`);
  assert.deepEqual(unknown, {body_kind: 'box_unknown_method', method: 'setVoiceCallPresence'});
  const prefixed = classifyNotFoundBody(`noise unknown gateway method: EnsureSandBox ${SECRET}`);
  assert.deepEqual(prefixed, {body_kind: 'box_unknown_method', method: 'EnsureSandBox'});
  const unsafe = classifyNotFoundBody(`unknown gateway method: Foo-bar ${SECRET}`);
  assert.deepEqual(unsafe, {body_kind: 'box_unknown_method'});
  assert.equal(unsafe.method, undefined);
  const notFound = classifyNotFoundBody(`not found: POST /api/${SECRET}`);
  assert.deepEqual(notFound, {body_kind: 'box_not_found'});
  const contained = classifyNotFoundBody(`{"message":"not found: GET /health ${SECRET}"}`);
  assert.deepEqual(contained, {body_kind: 'box_not_found'});
  const other = classifyNotFoundBody(`{"error":"missing","token":"${SECRET}"}`);
  assert.deepEqual(other, {body_kind: 'other'});
  const late = 'x'.repeat(520) + `unknown gateway method: HiddenMethod ${SECRET}`;
  assert.deepEqual(classifyNotFoundBody(late), {body_kind: 'other'});
  const dumped = JSON.stringify({unknown, prefixed, unsafe, notFound, contained, other});
  assert.equal(dumped.includes(SECRET), false);
  assert.equal(dumped.includes('Foo-bar'), false);
  assert.equal(dumped.includes('HiddenMethod'), false);
});

test('a 403 never reads the upstream body, and a 404 stops after 512 bytes', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  const accountLoader = () => ({token: 't', machineId: 'm'});
  const body = {wake: false, supportsAsleepBox: true};
  let reads = 0;
  const forbidden = async () => ({
    ok: false, status: 403, headers: {get: () => null},
    body: {getReader() { reads++; }, cancel() { reads++; }},
    json() { reads++; }, text() { reads++; },
  });
  await assert.rejects(accountRpc('EnsureSandBox', body, {
    accountLoader, fetcher: forbidden, clientVersion: '1.1.0', backoff: createEnsureBackoff({now: () => 1_000, secretsMtime: () => 1}),
  }), (error) => error.message === 'upstream_http_403' && error.step === 'ensure_sandbox' && error.target_host === 'api2.cursor.sh' && error.body_kind === undefined);
  assert.equal(reads, 0);
  let pulled = 0;
  const stream = new ReadableStream({
    pull(controller) {
      if (pulled >= 4000) { controller.close(); return; }
      const chunk = Buffer.alloc(300, 121);
      pulled += chunk.length;
      controller.enqueue(chunk);
    },
  });
  const client = new NativeSandboxClient({
    connectMode: 'normal',
    accountLoader: () => ({authId: 'owner', token: 't', machineId: 'm'}),
    rpc: (method, rpcBody, options) => accountRpc(method, rpcBody, {
      ...options, clientVersion: '1.1.0', backoff: createEnsureBackoff({now: () => 5_000, secretsMtime: () => 1}),
    }),
    fetcher: async () => new Response(stream, {status: 404}),
  });
  await assert.rejects(client.presence({agentId: 'a', callId: 'c', isOnTheLine: true, acceptsOverheard: false}), (error) => {
    assert.equal(error.step, 'ensure_sandbox');
    assert.equal(error.body_kind, 'other');
    assert.equal(error.target_host, 'api2.cursor.sh');
    assert.equal(JSON.stringify(error).includes(SECRET), false);
    return true;
  });
  assert.ok(pulled < 1000, `read ${pulled} bytes`);
});

function pres(port, callId, isOnTheLine) {
  return fetch(`http://127.0.0.1:${port}/native/presence`, {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({agentId: 'a', callId, isOnTheLine, acceptsOverheard: false}),
  });
}

test('EnsureSandBox 404 is step ensure_sandbox and the body never reaches the log', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  const logs = [];
  const port = await freePort();
  const sandbox = new NativeSandboxClient({
    connectMode: 'normal',
    accountLoader: () => ({token: 't', machineId: 'm', authId: 'owner'}),
    rpc: (method, body, options) => accountRpc(method, body, {
      ...options, clientVersion: '1.1.0', backoff: createEnsureBackoff({now: () => 9_000, secretsMtime: () => 1}),
    }),
    fetcher: async () => new Response(`{"message":"unknown gateway method: EnsureSandBox","token":"${SECRET}"}`, {status: 404}),
  });
  const server = createBridge({port, agentId: 'a', desktopEnabled: true, sandbox, nativeLog: event => logs.push(event)});
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  try {
    const response = await pres(port, 'call-c', true);
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), {ok: false, error: 'upstream_http_404', step: 'ensure_sandbox', body_kind: 'box_unknown_method'});
    const failure = logs.find(event => event.state === 'error');
    assert.equal(failure.source, 'presence');
    assert.equal(failure.step, 'ensure_sandbox');
    assert.equal(failure.http_status, 404);
    assert.equal(failure.target_host, 'api2.cursor.sh');
    assert.equal(failure.body_kind, 'box_unknown_method');
    assert.equal(failure.method, 'EnsureSandBox');
    assert.equal(JSON.stringify(logs).includes(SECRET), false);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('sandbox presence 404 is step presence with body_kind box_not_found', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  const logs = [];
  const port = await freePort();
  const sandbox = new NativeSandboxClient({
    connectMode: 'normal',
    accountLoader: () => ({authId: 'owner'}),
    rpc: async () => ({gatewayUrl: 'https://pod.cursorvm.com', gatewayToken: 'SECRET_TOKEN_VALUE'}),
    fetcher: async () => new Response(`not found: POST /api/${SECRET}`, {status: 404}),
  });
  const server = createBridge({port, agentId: 'a', desktopEnabled: true, sandbox, nativeLog: event => logs.push(event)});
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  try {
    const response = await pres(port, 'call-c', true);
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), {ok: false, error: 'upstream_http_404', step: 'presence', body_kind: 'box_not_found'});
    const failure = logs.find(event => event.state === 'error');
    assert.equal(failure.step, 'presence');
    assert.equal(failure.http_status, 404);
    assert.equal(failure.target_host, 'pod.cursorvm.com');
    assert.equal(failure.body_kind, 'box_not_found');
    assert.equal(failure.method, undefined);
    const ensured = logs.filter(event => event.state === 'ensure_ok');
    assert.equal(ensured.length, 1);
    assert.equal(ensured[0].target_host, 'pod.cursorvm.com');
    assert.equal(JSON.stringify(logs).includes(SECRET), false);
    assert.equal(JSON.stringify(logs).includes('SECRET_TOKEN_VALUE'), false);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('a box stream 404 is step box_stream with body_kind other', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  const logs = [];
  const port = await freePort();
  const sandbox = new NativeSandboxClient({
    connectMode: 'normal',
    accountLoader: () => ({authId: 'owner'}),
    rpc: async () => ({gatewayUrl: 'https://pod.cursorvm.com', gatewayToken: 'tok'}),
    fetcher: async (url) => new URL(url).pathname === '/events'
      ? new Response(`{"error":"missing","token":"${SECRET}"}`, {status: 404, headers: {'content-type': 'application/json'}})
      : new Response('', {status: 200}),
  });
  const server = createBridge({
    port, agentId: 'a', desktopEnabled: true, sandbox,
    transcript: {async *events() {}}, nativeLog: event => logs.push(event),
  });
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  try {
    assert.equal((await pres(port, 'call-c', true)).status, 200);
    const response = await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-c`);
    assert.equal(response.status, 502);
    const failure = logs.find(event => event.source === 'box' && event.state === 'error');
    assert.equal(failure.step, 'box_stream');
    assert.equal(failure.http_status, 404);
    assert.equal(failure.target_host, 'pod.cursorvm.com');
    assert.equal(failure.body_kind, 'other');
    assert.equal(JSON.stringify(logs).includes(SECRET), false);
    assert.equal(JSON.stringify(await response.json()).includes(SECRET), false);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});

test('a typed stream 404 is step typed_stream and the body stays out of the log', async () => {
  const {NativeTranscriptClient} = await import('./gbot_voice_session_bridge.mjs');
  const logs = [];
  const port = await freePort();
  const transcript = new NativeTranscriptClient({
    accountLoader: () => ({token: 't', machineId: 'm'}),
    clientVersion: '1.1.0',
    fetcher: async () => new Response(`not found: POST /WatchGrokBotTranscripts ${SECRET}`, {status: 404}),
  });
  const sandbox = {
    presence: async () => {}, callerAuthId: async () => 'owner',
    openEvents: async (signal) => new Response(new ReadableStream({
      start(controller) { signal.addEventListener('abort', () => controller.close(), {once: true}); },
    }), {headers: {'content-type': 'text/event-stream'}}),
  };
  const server = createBridge({
    port, agentId: 'a', desktopEnabled: true, sandbox, transcript,
    nativeRetryDelayMs: 20, nativeLog: event => logs.push(event),
  });
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  let reader;
  try {
    assert.equal((await pres(port, 'call-c', true)).status, 200);
    const response = await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-c`);
    reader = response.body.getReader();
    await reader.read();
    const started = Date.now();
    while (!logs.some(event => event.source === 'typed' && event.state === 'error')) {
      if (Date.now() - started > 1000) break;
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    const failure = logs.find(event => event.source === 'typed' && event.state === 'error');
    assert.ok(failure);
    assert.equal(failure.step, 'typed_stream');
    assert.equal(failure.http_status, 404);
    assert.equal(failure.target_host, 'api2.cursor.sh');
    assert.equal(failure.body_kind, 'box_not_found');
    assert.equal(JSON.stringify(logs).includes(SECRET), false);
  } finally {
    await reader?.cancel();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});

test('a 404 hanging up the stored call forgets it, and a 502 or the live call does not', async () => {
  const {rememberCall} = await import('./gbot_voice_session_bridge.mjs');
  const logs = [];
  const seen = [];
  const dir = mkdtempSync(join(tmpdir(), 'koji-call-'));
  const callStore = join(dir, 'call.json');
  rememberCall(callStore, {agentId: 'a', callId: 'call-old', acceptsOverheard: false});
  const port = await freePort();
  let failNew = true;
  const sandbox = {
    callerAuthId: async () => 'owner',
    presence: async body => {
      seen.push(`${body.callId}:${body.isOnTheLine}`);
      if (!body.isOnTheLine) {
        throw Object.assign(new Error('upstream_http_404'), {
          step: 'presence_hangup', body_kind: 'box_not_found', target_host: 'pod.cursorvm.com', method: 'setVoiceCallPresence',
        });
      }
      if (failNew) throw Object.assign(new Error('upstream_http_500'), {step: 'presence', target_host: 'pod.cursorvm.com'});
    },
  };
  const server = createBridge({
    port, agentId: 'a', desktopEnabled: true, sandbox, callStore, nativeLog: event => logs.push(event),
  });
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  try {
    const failed = await pres(port, 'call-new', true);
    assert.equal(failed.status, 502);
    assert.equal(existsSync(callStore), false);
    const hangup = logs.find(event => event.state === 'stale_hangup');
    assert.equal(hangup.step, 'presence_hangup');
    assert.equal(hangup.http_status, 404);
    assert.equal(hangup.error_class, 'upstream_http_404');
    assert.equal(hangup.body_kind, 'box_not_found');
    assert.equal(hangup.target_host, 'pod.cursorvm.com');
    assert.equal(hangup.method, 'setVoiceCallPresence');
    failNew = false;
    assert.equal((await pres(port, 'call-new', true)).status, 200);
    assert.deepEqual(seen, ['call-old:false', 'call-new:true', 'call-new:true']);
  } finally { await new Promise(resolve => server.close(resolve)); }

  const kept = join(dir, 'kept.json');
  rememberCall(kept, {agentId: 'a', callId: 'call-old', acceptsOverheard: false});
  const port502 = await freePort();
  const stillThere = createBridge({
    port: port502, agentId: 'a', desktopEnabled: true, callStore: kept,
    sandbox: {
      callerAuthId: async () => 'owner',
      presence: async body => { if (!body.isOnTheLine) throw new Error('upstream_http_502'); throw new Error('upstream_http_500'); },
    },
  });
  await new Promise(resolve => stillThere.listen(port502, '127.0.0.1', resolve));
  try {
    assert.equal((await pres(port502, 'call-new', true)).status, 502);
    assert.equal(JSON.parse(readFileSync(kept, 'utf8')).callId, 'call-old');
  } finally { await new Promise(resolve => stillThere.close(resolve)); }

  const livePath = join(dir, 'live.json');
  const portLive = await freePort();
  const live = createBridge({
    port: portLive, agentId: 'a', desktopEnabled: true, callStore: livePath,
    sandbox: {
      callerAuthId: async () => 'owner',
      presence: async body => {
        if (!body.isOnTheLine) throw Object.assign(new Error('upstream_http_404'), {step: 'presence_hangup', body_kind: 'other', target_host: 'pod.cursorvm.com'});
      },
    },
  });
  await new Promise(resolve => live.listen(portLive, '127.0.0.1', resolve));
  try {
    assert.equal((await pres(portLive, 'call-live', true)).status, 200);
    const hangup = await pres(portLive, 'call-live', false);
    assert.equal(hangup.status, 502);
    assert.deepEqual(await hangup.json(), {ok: false, error: 'upstream_http_404', step: 'presence_hangup', body_kind: 'other'});
    assert.equal(JSON.parse(readFileSync(livePath, 'utf8')).callId, 'call-live');
  } finally { await new Promise(resolve => live.close(resolve)); }
});

test('ensure_ok is logged only when the host or the field names change', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  const logs = [];
  let mode = 'same';
  const client = new NativeSandboxClient({
    connectMode: 'normal',
    accountLoader: () => ({authId: 'owner'}),
    log: event => logs.push(event),
    rpc: async () => {
      if (mode === 'fields') return {gatewayUrl: 'https://pod.cursorvm.com', gatewayToken: 'SECRET_TOKEN_VALUE', extra: true};
      if (mode === 'host') return {gatewayUrl: 'https://other.cursorvm.com', gatewayToken: 'SECRET_TOKEN_VALUE'};
      return {gatewayUrl: 'https://pod.cursorvm.com', gatewayToken: 'SECRET_TOKEN_VALUE', networkToken: 'SECRET_NET'};
    },
    fetcher: async () => ({ok: true, status: 200, body: {cancel: async () => {}}}),
  });
  const call = {agentId: 'a', callId: 'c', isOnTheLine: true, acceptsOverheard: false};
  await client.presence(call);
  await client.presence(call);
  client.invalidate();
  await client.presence(call);
  assert.equal(logs.length, 1);
  assert.equal(logs[0].source, 'ensure');
  assert.equal(logs[0].state, 'ensure_ok');
  assert.equal(logs[0].target_host, 'pod.cursorvm.com');
  assert.deepEqual(logs[0].response_fields, ['gatewayToken', 'gatewayUrl', 'networkToken']);
  client.invalidate();
  mode = 'fields';
  await client.presence(call);
  client.invalidate();
  mode = 'host';
  await client.presence(call);
  assert.equal(logs.length, 3);
  assert.deepEqual(logs[1].response_fields, ['extra', 'gatewayToken', 'gatewayUrl']);
  assert.equal(logs[1].target_host, 'pod.cursorvm.com');
  assert.equal(logs[2].target_host, 'other.cursorvm.com');
  assert.deepEqual(logs[2].response_fields, ['gatewayToken', 'gatewayUrl']);
  assert.equal(JSON.stringify(logs).includes('SECRET_TOKEN_VALUE'), false);
  assert.equal(JSON.stringify(logs).includes('SECRET_NET'), false);
});
