// SPDX-FileCopyrightText: 2026 Joseph Zhou
// SPDX-License-Identifier: MIT
import test from 'node:test';
import assert from 'node:assert/strict';
import { createBridge, accountRpc, filterNativeEvent, validateBody, isRoutineStreamEnd } from './gbot_voice_session_bridge.mjs';
import { createServer } from 'node:net';
import { request } from 'node:http';
import { mkdtempSync, readFileSync } from 'node:fs';
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
  await assert.rejects(accountRpc('MintSandVoiceCallSecret', {}, {accountLoader,fetcher}), /^Error: upstream_http_403$/);
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
    assert.deepEqual(logs.filter(e=>e.state==='stale_hangup').map(e=>e.error_class),['upstream_http_502']);
    assert.match(logs.find(e=>e.state==='stale_hangup').at, /^\d{4}-\d{2}-\d{2}T/);
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
