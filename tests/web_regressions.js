// Execute the shipped rendering code with a tiny, layout-clamping DOM double.
// Actual layout and scroll anchoring are also checked in a disposable browser instance.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {File} = require('node:buffer');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const elements = new Map(), listeners = {}, timers = new Map();
let timerId=0;
const streams=[];
class TestSource {
  static CLOSED=2;
  constructor(url) { this.url=url; this.readyState=0; this.events={}; streams.push(this); }
  addEventListener(name,fn) { this.events[name]=fn; }
  close() { this.readyState=2; }
}
class Element {
  constructor(id) {
    this.id=id; this.scrollTop=0; this.dataset={}; this._html=''; this.children=[];
    const classes=new Set();
    this.classList={contains:name=>classes.has(name),add:name=>classes.add(name),remove:name=>classes.delete(name),
      toggle(name,on=!classes.has(name)){on?classes.add(name):classes.delete(name);return on;}};
  }
  get nodeType() { return this.id==='text'?3:1; }
  get nodeName() { return this.id==='text'?'#text':'DIV'; }
  get nodeValue() { return this._html; }
  set nodeValue(value) { this._html=value; }
  get attributes() { return Object.entries(this.dataset).map(([key,value])=>({name:'data-'+key,value})); }
  getAttribute(name) { return name.startsWith('data-')?this.dataset[name.slice(5)] ?? null:null; }
  hasAttribute(name) { return this.getAttribute(name)!==null; }
  removeAttribute(name) { if(name.startsWith('data-')) delete this.dataset[name.slice(5)]; }
  get childNodes() { return this.children; }
  isEqualNode(other) { return this.innerHTML===other.innerHTML && JSON.stringify(this.dataset)===JSON.stringify(other.dataset) && this.children.length===other.children.length && this.children.every((n,i)=>n.isEqualNode(other.children[i])); }
  appendChild(node) { this.insertBefore(node,null); }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() { return this.parent?.children[this.parent.children.indexOf(this)+1] || null; }
  insertBefore(node,cursor) {
    if(node===cursor) return;
    node.remove(); const index=cursor?this.children.indexOf(cursor):this.children.length;
    this.children.splice(index,0,node); node.parent=this;
  }
  remove() { if(this.parent) { this.parent.children.splice(this.parent.children.indexOf(this),1); this.parent=null; } }
  replaceWith(node) { const parent=this.parent, next=this.nextSibling; this.remove(); if(parent) parent.insertBefore(node,next); }
  contains() { return false; }
  querySelectorAll() { return []; }
  closest(selector) { return selector==='#view-tasks'?tasksPage:null; }
  addEventListener(name,fn) { (this.events ||= {})[name]=fn; }
  click() { return this.onclick?.(); }
  focus() { document.activeElement=this; }
  scrollIntoView() { this.scrolled=true; }
  getBoundingClientRect() { return {top:0,bottom:this.id==='thread'?10000:(this.clientHeight || 0)}; }
  setAttribute(name,value) { if(name.startsWith('data-')) this.dataset[name.slice(5)]=value; }
  append(child) { this._html+=child.innerHTML; }
  set innerHTML(value) {
    this._html=value;
    if(this.id==='template') { const node=new Element('fragment'), text=new Element('text'); text._html=value; node.appendChild(text); this.content={firstElementChild:node}; }
    // Removing the reminders collapses the scroll range, just as browser layout does.
    if(this.id==='reminders') tasksPage.scrollTop=Math.min(tasksPage.scrollTop, 100);
  }
  get innerHTML() { return this._html+this.children.map(n=>n.innerHTML).join(''); }
}
const tasksPage = new Element('view-tasks'); elements.set('view-tasks',tasksPage);
const document = {
  activeElement:null,
  getElementById(id) { if(!elements.has(id)) elements.set(id,new Element(id)); return elements.get(id); },
  querySelector(selector) { return selector==='#view-tasks'?tasksPage:new Element('other'); },
  querySelectorAll() { return []; },
  createElement(tag) { return new Element(tag); },
  addEventListener(name,fn) { listeners[name]=fn; },
};
const context = vm.createContext({document,console,Date,Map,Set,JSON,Math,encodeURIComponent,
  EventSource:TestSource, AbortController,
  setTimeout(fn,delay) { const id=++timerId; timers.set(id,{fn,delay}); return id; },
  clearTimeout(id) { timers.delete(id); },
});
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].split("$('send').onclick")[0];
vm.runInContext(script,context);
const run = code => vm.runInContext(code,context);
run(`data.reminders={items:Array.from({length:30},(_,i)=>({id:'r'+(i+1),name:'Review '+i,next_time:'Tomorrow',kind:'notify'}))};`);
// Image tests exercise the shipped handlers and fetch path, not a parallel implementation.
const imageFile=(name='screen.png',type='image/png',size=4)=>new File([new Uint8Array(size)],name,{type});
const imageMetadata=(file,id='image-1')=>({id,filename:file.name,mime:file.type,size:file.size,sha256:'a'.repeat(64)});
const flushImages=async()=>{for(let i=0;i<60;i++) await Promise.resolve();};
function imageHarness() {
  const created=[], revoked=[], requests=[], pageEvents={}; let sequence=0;
  context.URL={createObjectURL(file){const url='blob:test-'+(created.length+1);created.push({file,url});return url;},revokeObjectURL(url){revoked.push(url);}};
  context.crypto={randomUUID:()=> 'message-'+(++sequence)};
  context.window={addEventListener:(name,fn)=>pageEvents[name]=fn};
  context.fetch=async(path,options)=>{
    requests.push({path,options});const id='image-'+requests.length;
    return {ok:true,json:async()=>path.startsWith('/api/attachments?')?imageMetadata(options.body,id):{ok:true}};
  };
  run(`$('input').value='';sync=async()=>{};bindComposerImages();`);
  const add=files=>{context.imageTestFiles=files;run('addImages(imageTestFiles);');};
  return {created,revoked,requests,pageEvents,add};
}
(async()=>{
  switch(process.argv[3]) {
    case 'b26-image-inputs': {
      const h=imageHarness(), file=imageFile('screen & <one>.png');
      assert.match(html,/<input id="imageFiles"[^>]*accept="image\/png,image\/jpeg,image\/gif,image\/webp"[^>]*multiple hidden>/);
      let picked=0;elements.get('imageFiles').click=()=>picked++;elements.get('chooseImages').onclick();assert.equal(picked,1);
      elements.get('imageFiles').files=[file,imageFile('second.jpg','image/jpeg')];elements.get('imageFiles').value='selected';elements.get('imageFiles').onchange();
      assert.equal(elements.get('imageFiles').value,'','Same file can be selected again');
      let prevented=0;
      const paste=elements.get('input').events.paste;
      paste({clipboardData:{files:[],items:[],getData:()=> 'ordinary text'},preventDefault(){prevented++;}});
      assert.equal(prevented,0);assert.equal(run('draftImages.size'),2);
      paste({clipboardData:{files:[imageFile('paste.gif','image/gif')],getData:()=>''},preventDefault(){prevented++;}});
      assert.equal(prevented,1);
      paste({clipboardData:{files:[],items:[{kind:'file',getAsFile:()=>imageFile('mixed.webp','image/webp')}],getData:()=> 'keep this text'},preventDefault(){prevented++;}});
      assert.equal(prevented,1,'Mixed paste preserves native text insertion');
      const box=elements.get('composerBox'), transfer={types:['Files'],files:[imageFile('drop.png')]};
      box.events.dragover({dataTransfer:transfer,preventDefault(){prevented++;}});assert.ok(box.classList.contains('drag-over'));assert.equal(transfer.dropEffect,'copy');
      box.events.dragleave({relatedTarget:null});assert.ok(!box.classList.contains('drag-over'));
      box.events.drop({dataTransfer:transfer,preventDefault(){prevented++;}});assert.ok(!box.classList.contains('drag-over'));
      box.events.drop({dataTransfer:{files:[]},preventDefault(){throw Error('Text drag should remain native');}});
      await flushImages();assert.equal(run('draftImages.size'),5);assert.equal(h.requests.length,5);
      assert.equal(h.requests[0].path,'/api/attachments?filename='+encodeURIComponent(file.name));
      assert.equal(h.requests[0].options.body,file,'Raw File body, not JSON or multipart');assert.equal(h.requests[0].options.method,'POST');assert.equal(h.requests[0].options.headers['Content-Type'],'image/png');
      assert.match(elements.get('draftImages').innerHTML,/blob:test-1/);assert.match(elements.get('draftImages').innerHTML,/screen &amp; &lt;one&gt;\.png/);assert.doesNotMatch(elements.get('draftImages').innerHTML,/<one>/);
      assert.equal(elements.get('send').disabled,false);
      break;
    }
    case 'b26-image-limits': {
      const h=imageHarness();assert.deepEqual(JSON.parse(run('JSON.stringify(imageLimits())')),{max_bytes:10485760,max_count:6});
      run('data.image_limits={max_bytes:4,max_count:2};');
      h.add([imageFile('fake.png','image/svg+xml'),imageFile('empty.png','image/png',0),imageFile('big.png','image/png',5),imageFile('one.png'),imageFile('two.png'),imageFile('three.png')]);
      await flushImages();assert.equal(h.requests.length,2);assert.equal(h.created.length,2);assert.equal(run('draftImages.size'),2);
      const notice=elements.get('imageNotice').textContent;assert.match(notice,/Only PNG, JPEG, GIF and WebP/);assert.match(notice,/empty.png/);assert.match(notice,/big.png/);assert.match(notice,/at most 2 images/);
      assert.ok(h.requests.every(r=>r.options.body.size===4),'Exact size boundary is accepted');
      run(`data.image_limits.max_count=1;$('input').value='Keep text';`);await run('send()');assert.equal(h.requests.length,2);assert.equal(elements.get('input').value,'Keep text');
      await listeners.click({target:{closest:()=>({dataset:{imageRemove:'2'}})}});assert.equal(h.revoked.length,1);
      run('data.image_limits.max_bytes=3;');await run('send()');assert.equal(h.requests.length,2,'Changed server limits are enforced again before sending');
      run('data.image_limits.max_bytes=4;');await run('send()');assert.equal(h.requests.length,3);assert.equal(run('draftImages.size'),0);
      assert.equal(new Set(h.revoked).size,2);
      break;
    }
    case 'b26-image-upload-retry': {
      const h=imageHarness(), file=imageFile();let attempt=0;
      context.fetch=async(path,options)=>{h.requests.push({path,options});attempt++;return attempt<3?{ok:false,status:attempt===1?415:429,json:async()=>({error:attempt===1?'Invalid image header <unsafe>':'Another upload is active'})}:{ok:true,json:async()=>imageMetadata(file)};};
      h.add([file]);await flushImages();
      assert.equal(run('draftImages.get("1").file'),file);assert.equal(run('draftImages.get("1").metadata'),null);assert.equal(elements.get('send').disabled,true);
      assert.match(elements.get('draftImages').innerHTML,/Upload failed: Invalid image header &lt;unsafe&gt;/);assert.match(elements.get('draftImages').innerHTML,/data-image-retry="1"/);
      run(`$('input').value='Do not lose me';`);await run('send()');assert.equal(h.requests.length,1);assert.equal(elements.get('input').value,'Do not lose me');
      await listeners.click({target:{closest:()=>({dataset:{imageRetry:'1'}})}});
      assert.equal(h.requests.length,2);assert.equal(h.requests[1].options.body,file);assert.match(elements.get('draftImages').innerHTML,/Another upload is active/);assert.equal(elements.get('send').disabled,true,'HTTP 429 keeps the draft retryable');
      await listeners.click({target:{closest:()=>({dataset:{imageRetry:'1'}})}});
      assert.equal(h.requests.length,3);assert.equal(h.requests[2].options.body,file);assert.equal(h.created.length,1);assert.equal(h.revoked.length,0);assert.equal(elements.get('send').disabled,false);
      assert.equal(run('draftImages.get("1").metadata.id'),'image-1');
      await listeners.click({target:{closest:()=>({dataset:{imageRemove:'1'}})}});assert.deepEqual(h.revoked,['blob:test-1']);
      break;
    }
    case 'b26-image-send-retry': {
      const h=imageHarness();h.add([imageFile('one.png'),imageFile('two.jpg','image/jpeg')]);await flushImages();
      const posts=[];let release;
      context.fetch=async(path,options)=>{posts.push({path,body:options.body});if(posts.length===1) throw Error('Lost acknowledgement');return new Promise(resolve=>{release=resolve;});};
      // Image-only messages are valid, and the failed pending row keeps both images.
      assert.equal(await run('send()'),false);assert.equal(run('draftImages.size'),0);assert.equal(h.revoked.length,2);
      assert.match(elements.get('thread').innerHTML,/Send failed: Lost acknowledgement/);assert.match(elements.get('thread').innerHTML,/\/api\/attachments\/image-1/);assert.match(elements.get('thread').innerHTML,/\/api\/attachments\/image-2/);
      assert.deepEqual(JSON.parse(posts[0].body),{text:'',request_id:'message-1',attachments:['image-1','image-2']});
      run(`$('input').value='Next draft';data.image_limits={max_count:1,max_bytes:1};`);
      const retry=run(`send('message-1')`);await flushImages();await run(`send('message-1')`);assert.equal(posts.length,2,'Double retry cannot send twice concurrently');
      assert.equal(posts[1].body,posts[0].body,'Retry preserves exact serialized payload despite edited draft and changed limits');
      release({ok:true,json:async()=>({ok:true})});assert.equal(await retry,true);assert.equal(run('pending.size'),0);assert.equal(elements.get('input').value,'Next draft');
      const textOnly=run('send()');assert.deepEqual(JSON.parse(posts[2].body),{text:'Next draft',request_id:'message-2',attachments:[]});
      release({ok:true,json:async()=>({ok:true})});assert.equal(await textOnly,true);assert.equal(elements.get('input').value,'');
      await run('send()');assert.equal(posts.length,3,'An empty composer still sends nothing');
      break;
    }
    case 'b26-image-races': {
      const h=imageHarness(), requests=[];
      context.fetch=(path,options)=>new Promise(resolve=>requests.push({path,options,resolve}));
      h.add([imageFile('removed.png'),imageFile('queued.png'),imageFile('kept.png')]);await flushImages();
      run(`$('input').value='First';`);await run('send()');assert.equal(run('pending.size'),0);assert.equal(requests.length,1,'Uploads are sequential and Send cannot silently omit queued images');
      assert.match(elements.get('draftImages').innerHTML,/Waiting to upload/);
      run(`uploadImage('3');removeDraftImage('1');removeDraftImage('2');`);await flushImages();assert.equal(requests.length,1,'Removing an active upload does not release its slot early');
      requests[0].resolve({ok:true,json:async()=>imageMetadata(requests[0].options.body,'removed')});await flushImages();assert.equal(run('draftImages.size'),1);assert.equal(requests.length,2);
      assert.equal(requests[1].options.body.name,'kept.png','Removed queued image never reaches the API');
      requests[1].resolve({ok:true,json:async()=>imageMetadata(requests[1].options.body,'kept')});await flushImages();
      const sending=run('send()');assert.equal(requests.length,3);
      assert.deepEqual(JSON.parse(requests[2].options.body),{text:'First',request_id:'message-1',attachments:['kept']});
      h.add([imageFile('next.png')]);run(`$('input').value='Next';`);await flushImages();
      requests[3].resolve({ok:true,json:async()=>imageMetadata(requests[3].options.body,'next')});await flushImages();
      requests[2].resolve({ok:true,json:async()=>({ok:true})});await sending;
      assert.equal(run('draftImages.size'),1);assert.equal(run('[...draftImages.values()][0].metadata.id'),'next');assert.equal(elements.get('input').value,'Next');
      assert.deepEqual(h.revoked,['blob:test-1','blob:test-2','blob:test-3']);
      const second=run('send()');assert.deepEqual(JSON.parse(requests[4].options.body),{text:'Next',request_id:'message-2',attachments:['next']});
      requests[4].resolve({ok:true,json:async()=>({ok:true})});await second;assert.equal(h.revoked.length,4);
      break;
    }
    case 'b26-image-rendering': {
      const attachment={id:'image?x="<bad>/#',filename:'<img src=x onerror="bad">.png',mime:'image/png',size:4,sha256:'hash'};
      context.imageAttachment=attachment;
      run(`messages=new Map([[1,{id:1,role:'user',text:'',status:'done',attachments:[imageAttachment]}]]);renderThread(true);`);
      let markup=elements.get('thread').innerHTML;
      assert.match(markup,/target="_blank" rel="noopener noreferrer"/);assert.ok(markup.includes('/api/attachments/'+encodeURIComponent(attachment.id)));assert.match(markup,/&lt;img src=x onerror=&quot;bad&quot;&gt;\.png/);assert.doesNotMatch(markup,/<bad>|<img src=x/);
      assert.match(markup,/loading="lazy" width="96" height="80"/,'Reserve thumbnail geometry to avoid image-load scroll jumps');
      run(`messages.get(1).attachments=[{id:'new',filename:'new.webp',mime:'image/webp'}];renderThread();`);markup=elements.get('thread').innerHTML;assert.match(markup,/\/api\/attachments\/new/);assert.doesNotMatch(markup,/image%3F/,'Attachments participate in thread reconciliation');
      assert.equal(run(`messageImages([{id:'x',filename:'x.svg',mime:'image/svg+xml'},null])`),'');
      run(`messages.get(1).attachments=[];renderThread();`);assert.doesNotMatch(elements.get('thread').innerHTML,/class="image-list"/);
      break;
    }
    case 'b26-image-auth-lifecycle': {
      const h=imageHarness();
      context.fetch=async()=>({status:401,ok:false,json:async()=>{throw Error('Never read login body');}});
      h.add([imageFile()]);await flushImages();assert.equal(run('authRequired'),true);assert.equal(run('draftImages.size'),1);assert.match(elements.get('imageNotice').textContent,/Retry or remove/);assert.match(elements.get('error').textContent,/login expired/);
      run(`authRequired=false;`);
      context.fetch=(path,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(Error('timeout'),{name:'AbortError'}))));
      const retry=run(`uploadImage('1')`);await flushImages();const timeout=[...timers.values()].find(t=>t.delay===60000);assert.ok(timeout);timeout.fn();await retry;
      assert.match(elements.get('draftImages').innerHTML,/Upload timed out/);assert.equal(h.revoked.length,0,'Failed uploads keep their local preview for retry');
      h.pageEvents.pagehide({persisted:true});assert.equal(h.revoked.length,0,'Back-forward cache keeps the live draft');
      h.pageEvents.pagehide({persisted:false});assert.deepEqual(h.revoked,['blob:test-1']);assert.equal(run('draftImages.size'),0);
      break;
    }
    case 'observations': {
      run(`observations=[{created:1,source:'github',status:'noted',text:'<script>event</script>',url:'https://github.com/org/repo/pull/1',would_do:'Read <unsafe>',route:'<worker>',used_builtin_tools:true,builtin_tools:['Bash','NotebookEdit','Read','Agent','Task','Skill','terminal','mcp__other__read','<unknown>'],mixed_turn:true},{created:2,source:'github',status:'limited',text:'surplus',url:'javascript:alert(1)',would_do:'',route:'',used_builtin_tools:false,builtin_tools:[],mixed_turn:false}];`);
      const markup=run('observationList()');
      assert.match(markup,/&lt;script&gt;event/); assert.doesNotMatch(markup,/<script>|<unsafe>|<worker>|<unknown>|javascript:/);
      assert.match(markup,/https:\/\/github.com\/org\/repo\/pull\/1/); assert.match(markup,/Not observed \(hourly limit\)/);
      assert.match(markup,/used_builtin_tools: Bash, NotebookEdit, Read, Agent, Task, Skill, terminal, mcp__other__read, &lt;unknown&gt;/);
      assert.match(markup,/rel="noopener noreferrer"/);
      assert.equal((markup.match(/mixed_turn: true/g) || []).length,1);
      assert.match(markup,/tool use is not automatically a violation/);
      assert.match(markup,/Every reported tool name except known service Router MCP tools is audited/);
      run(`observations[0].mixed_turn=false;`);
      assert.doesNotMatch(run('observationList()'),/mixed_turn:/);
      run(`observations[0].mixed_turn=true; observations[0].used_builtin_tools=false;`);
      assert.match(run('observationList()'),/mixed_turn: true/);
      assert.doesNotMatch(run('observationList()'),/used_builtin_tools:/);
      break;
    }
    case 'routing-layout': {
      run(`data.items=[{code:'one',name:'Web <tag>'},{code:'two',name:'Research'}];data.tasks=Array.from({length:6},(_,i)=>({id:'t'+(i+1),code_name:'Name '+i,name:'Task <tag>',item:i<3?'one':'two',model:'gpt-6-astra',status:i===5?'waiting_deps':'running',wait_for:i===5?[{task_id:'t2',satisfied:false}]:[]}));renderCommunication();`);
      for(const panel of [true,false]) {
        const plan=JSON.parse(run(`JSON.stringify(routingPlan(data.tasks,${panel},${panel?348:1000}))`));
        assert.equal(plan.nodes.length,6);assert.equal(plan.groups.length,2);
        assert.equal(plan.wires.filter(w=>w.dep).length,1);assert.match(plan.wires.find(w=>w.dep).d,/ C/);
        for(const n of plan.nodes) assert.ok(n.x+n.width<=plan.width && n.y+n.height<=plan.height);
        const one=JSON.parse(run(`JSON.stringify(routingPlan(data.tasks.slice(0,1),${panel},348))`));
        assert.ok(one.wires.some(w=>w.key==='t1' && w.d.length>20),'One Worker keeps a visible route');
      }
      for(const id of ['commMap','chatRoutingMap']) {
        const markup=elements.get(id).innerHTML;assert.match(markup,/data-dependency="t2:t6"/);assert.match(markup,/Web &lt;tag&gt;/);assert.doesNotMatch(markup,/<tag>/);
      }
      assert.match(elements.get('commMap').innerHTML,/route-legend/);assert.match(elements.get('commMap').innerHTML,/data-comm-edge="user"/);
      assert.equal(run(`routeGroupClass('one')`),run(`routeGroupClass('one')`));
      run(`Array.from({length:30},(_,i)=>routeGroupClass('extra'+i));`);assert.equal(run(`routeGroupClass('extra29')`),'route-group-3','Additional labeled facets use neutral, never cycle colors');
      break;
    }
    case 'routing-release-preserves-flight': {
      run(`data.tasks=[{id:'t1',name:'Upstream',status:'running'},{id:'t2',name:'Downstream',status:'waiting_deps',wait_for:[{task_id:'t1',satisfied:false}]}];renderCommunication();const beforeSide=$('chatRoutingMap').innerHTML,beforeFull=$('commMap').innerHTML;let cancelled=false;traffic.particles.add({node:{remove(){cancelled=true;}},animation:{cancel(){cancelled=true;}}});data.tasks[1].status='running';data.tasks[1].wait_for[0].satisfied=true;renderCommunication();`);
      assert.equal(run('cancelled'),false,'Dependency satisfaction is content, not topology; an answer in flight must reach its Worker');
      assert.equal(run(`$('chatRoutingMap').innerHTML===beforeSide && $('commMap').innerHTML===beforeFull`),true);
      assert.equal(run('traffic.particles.size'),1);
      break;
    }
    case 'routing-resize': {
      const map=document.getElementById('chatRoutingMap');
      Object.defineProperty(map,'clientWidth',{get:()=>run(`state.view==='chat'?298:0`)});
      run(`data.tasks=[{id:'t1',name:'One',status:'running'}];navigate('chat');`);
      assert.equal(run('routeWidths.panel'),298);assert.match(map.innerHTML,/viewBox="0 0 298 /);
      run(`navigate('live');renderCommunication();`);assert.equal(run('routeWidths.panel'),298,'Hidden snapshots retain last measured width');
      run(`routeWidths.panel=348;renderCommunication();navigate('chat');`);
      assert.equal(run('routeWidths.panel'),298,'Returning to Chat measures after it becomes visible');assert.match(map.innerHTML,/viewBox="0 0 298 /);
      break;
    }
    case 'routing-renamed-item': {
      run(`data.items=[{code:'a',name:'Old title'}];data.tasks=[{id:'t1',name:'Worker',status:'running',item:'a'}];renderCommunication();data.items[0].name='Renamed <item>';renderCommunication();`);
      for(const id of ['commMap','chatRoutingMap']) {
        const markup=elements.get(id).innerHTML;assert.match(markup,/Renamed &lt;item&gt;/);assert.doesNotMatch(markup,/Old title|<item>/);
      }
      break;
    }
    case 'routing-signals': {
      const root=new Element('root'), running=new Element('path'), waiting=new Element('path'), you=new Element('path'), router=new Element('router');
      running.dataset.current='t1';waiting.dataset.current='t2';you.dataset.current='user';
      root.querySelectorAll=sel=>sel==='[data-current]'?[running,waiting,you]:[router];context.root=root;
      run(`connected=true;data.router={busy:true};data.tasks=[{id:'t1',status:'running'},{id:'t2',status:'waiting'}];updateRouteSignals(root);`);
      assert.ok(running.classList.contains('on'));assert.ok(!running.classList.contains('ask'));assert.ok(waiting.classList.contains('ask'));assert.ok(you.classList.contains('on'));assert.ok(router.classList.contains('router-busy'));
      document.hidden=true;run('updateRouteSignals(root);');assert.ok(root.classList.contains('route-paused'));assert.ok(!running.classList.contains('on'));
      assert.match(html,/route-current 2\.6s linear infinite/);assert.match(html,/route-current.ask[^}]*1\.4s/);assert.match(html,/route-beat 1\.1s/);
      assert.match(html,/prefers-reduced-motion:reduce\) \{\s*\.route-current.on[^}]*animation:none !important/);
      break;
    }
    case 'routing-events': {
      run(`const flights=[];flow=(...args)=>{flights.push(args);return true;};reactReport=()=>{};connected=true;data.time=100;data.tasks=[{id:'t1',status:'running'},{id:'t2',status:'waiting_deps',wait_for:[{task_id:'t1',satisfied:false}]}];reconcileTraffic([],new Set(),traffic.epoch);`);
      run(`data.tasks[0].status='waiting';data.tasks[0].question='Continue?';reconcileTraffic([],new Set(['state']),traffic.epoch);flights.find(f=>f[2]==='question')[3]();`);
      assert.equal(run(`flights.filter(f=>f[0]==='user' && f[1] && f[2]==='question').length`),1);
      run(`data.tasks[0].status='completed';data.tasks[1].status='running';data.tasks[1].wait_for[0].satisfied=true;reconcileTraffic([],new Set(['state']),traffic.epoch);`);
      assert.equal(run(`flights.filter(f=>f[2]==='release').length`),0,'Result waits for dependency arrival');
      run(`flights.find(f=>f[2]==='dependency')[3]();flights.find(f=>f[2]==='dependency')[3]();`);
      assert.equal(run(`flights.filter(f=>f[2]==='release').length`),1);
      for(const k of ['dispatch','message','answer','question','done','release','plain']) assert.equal(run(`trafficKinds.${k}.mail`),true,k+' is an envelope');
      for(const k of ['progress','dependency']) assert.equal(run(`Boolean(trafficKinds.${k}.mail)`),false,k+' is a light dot');
      run(`sync=async()=>{};loadDetail=async()=>{};api=async()=>({});data.tasks=[{id:'t3',status:'waiting',question_id:5}];`);
      await run(`taskAction('t3','send',{text:'Yes'});`);run(`flights.find(f=>f[0]==='user' && f[2]==='answer')[3]();`);
      assert.equal(run(`flights.filter(f=>f[0]==='t3' && f[2]==='answer').length`),1);
      break;
    }
    case 'routing-lifecycle': {
      const animations=[], nodes=[], svg={append(...n){nodes.push(...n);}};
      const target=new Element('target');target.dataset.task='t1';
      const link=new Element('link');link.style={setProperty(){}};
      const paths=Array.from({length:50},(_,i)=>({dataset:{commEdge:'t'+i},ownerSVGElement:svg,getAttribute:()=> 'M0 0 L100 0',getTotalLength:()=>100,getPointAtLength:x=>({x,y:10}),getBoundingClientRect:()=>({top:0,bottom:100}),closest:()=>link}));
      document.getElementById('commMap').querySelectorAll=sel=>sel==='[data-comm-edge]'?paths:sel==='[data-task]'?[target]:[];
      document.getElementById('view-live').getBoundingClientRect=()=>({top:0,bottom:100});
      document.createElementNS=tag=>{const n=new Element(tag);n.attrs={};n.setAttribute=(k,v)=>{n.attrs[k]=v;};n.append=(...children)=>n.children.push(...children);n.remove=()=>n.removed=true;n.animate=(frames,options)=>{let resolve;const a={finished:new Promise(r=>resolve=r),resolve:()=>resolve(),cancel(){this.cancelled=true;},frames,options,node:n};animations.push(a);return a;};return n;};
      run(`const reducedMotion={matches:false};connected=true;state.view='live';$('communication').open=true;for(let i=0;i<50;i++) flow('t'+i,false,'dispatch');`);
      assert.equal(run('traffic.particles.size'),8);assert.equal(run('traffic.queue.length'),16);
      assert.equal(nodes.filter(n=>n.dataset.kind==='dispatch').length,8);
      assert.equal(run(`flow('t1',false,'dispatch')`),false,'Bursts coalesce per edge and kind');
      const envelope=animations.find(a=>a.node.dataset.edge==='t1');envelope.resolve();await Promise.resolve();await Promise.resolve();
      assert.ok(link.classList.contains('comm-arrival'));assert.ok(target.classList.contains('comm-arrival'));
      assert.equal(run('traffic.particles.size'),8,'Queue drains after arrival, not all at once');assert.equal(run('traffic.queue.length'),15);
      const hold=[...timers.values()].find(t=>t.delay===1500);assert.ok(hold);hold.fn();assert.ok([...timers.values()].some(t=>t.delay===2500));
      run(`clearTraffic();`);assert.equal(run('traffic.particles.size+traffic.sparks.size+traffic.queue.length'),0);assert.ok(nodes.every(n=>n.removed));
      document.hidden=true;assert.equal(run(`flow('t40',false,'done')`),false);document.hidden=false;
      run(`reducedMotion.matches=true;flow('t1',false,'done');`);assert.equal(run('traffic.particles.size'),0);assert.ok(link.classList.contains('comm-arrival'));
      run(`clearTraffic();reducedMotion.matches=false;for(let i=0;i<10;i++) flow('t'+i,false,'dispatch');traffic.queue.forEach(q=>q.time-=6000);`);
      const live=run('[...traffic.particles][0]');live.animation.resolve();await Promise.resolve();await Promise.resolve();assert.equal(run('traffic.queue.length'),0,'Expired jobs never replay');
      assert.equal(run('traffic.particles.size'),7);run('clearTraffic(true);');assert.equal(run('traffic.ready'),false);
      break;
    }
    case 'routing-summary': {
      run(`data.tasks=Array.from({length:7},(_,i)=>({id:'t'+i,name:'Task',status:'running'}));data.tasks.push({id:'t8',name:'Question <tag>',status:'waiting',question:'Keep <script> original?'});renderActivity();`);
      const summary=elements.get('progressSummary').innerHTML;
      assert.match(summary,/7 Workers running/);assert.match(summary,/data-view="live">View/);assert.doesNotMatch(summary,/progress-line|data-task/);
      assert.match(elements.get('confirmations').innerHTML,/Keep &lt;script&gt; original\?/);assert.doesNotMatch(elements.get('confirmations').innerHTML,/<script>/);
      run(`data.tasks=[];renderActivity();`);assert.equal(elements.get('progressSummary').innerHTML,'');
      run(`data.counts.running=12;data.tasks_truncated=true;renderActivity();`);assert.match(elements.get('progressSummary').innerHTML,/12 Workers running/);
      break;
    }
    case 'routing-inspect': {
      for(const [tool,input,text] of [['WebSearch',{query:'routing <script>'},'routing &lt;script&gt;'],['Read',{file_path:'/src/full/path.py'},'/src/full/path.py'],['Bash',{command:'python3 -m unittest'},'python3 -m unittest']]) {
        const markup=run(`transcriptMarkup(${JSON.stringify({kind:'tool_call',tool,text:JSON.stringify(input),time:1})},0)`);
        assert.ok(markup.split('</summary>')[0].includes(text));assert.doesNotMatch(markup,/<script>|&quot;command&quot;|&quot;query&quot;/);
      }
      const result=run(`transcriptMarkup({kind:'tool_result',text:'First <tag>\\nSecond & line\\nThird line',time:1},0)`);
      assert.match(result,/class="result-preview">First &lt;tag&gt;\nSecond &amp; line/);assert.doesNotMatch(result.split('</summary>')[0],/Third line/);assert.match(result,/Third line/);
      assert.match(html,/\.session-entry \.tag, \.session-entry time \{ flex:none; white-space:nowrap; overflow-wrap:normal;/);
      assert.ok(run(`inspectTool('Bash',JSON.stringify({command:'a'.repeat(400)})).length`)<160);
      break;
    }
    case 'routing-models': {
      for(const [full,short] of [['claude/claude-opus-5-5[1m]','opus 5.5'],['claude-sonnet-4-5-20250929','sonnet 4.5'],['claude-haiku-4-5','haiku 4.5'],['claude-opus-4-20250514','opus 4'],['claude-sonnet-4-20250514','sonnet 4'],['gpt-6-astra','gpt-6-astra'],['provider/gpt-6-astra[1m]','gpt-6-astra']]) {
        assert.equal(run(`shortModel(${JSON.stringify(full)})`),short);
        const markup=run(`modelTag(${JSON.stringify(full)})`);assert.ok(markup.includes(`title="${full}"`));assert.ok(markup.includes(`>${short}</span>`));
      }
      assert.equal(run(`modelTag('<script>"')`),'<span class="model-tag" title="&lt;script&gt;&quot;">&lt;script&gt;&quot;</span>');
      break;
    }
    case 'v21-upcoming': {
      run(`data.reminders={items:[8,2,6,1,7,3,5,4].map(n=>({id:'r'+n,name:'Reminder '+n,text:'内容 <tag> & '+n,next_at:n,next_time:'Time '+n,kind:n%2?'notify':'run',status:n===8?'cancelled':'active',cron:n===7?'0 9 * * *':null}))};renderReminders();`);
      const markup=elements.get('upcoming').innerHTML, first=markup.split('<details')[0];
      assert.equal(elements.get('upcoming').hidden,false);
      assert.deepEqual([...first.matchAll(/data-cancel-reminder="(r\d+)"/g)].map(m=>m[1]),['r1','r2','r3','r4','r5']);
      assert.match(markup,/>\+2 more<\/summary>/);assert.match(markup,/Repeats · 0 9 \* \* \*/);assert.doesNotMatch(markup,/r8|<tag>/);
      assert.match(markup,/内容 &lt;tag&gt; &amp;/);assert.match(markup,/Time 1 · notify/);assert.match(markup,/Time 2 · run/);
      for(const row of JSON.parse(run('JSON.stringify(upcomingReminders())'))) {
        const rendered=run(`reminderRow(${JSON.stringify(row)})`);
        assert.ok(markup.includes(rendered));assert.ok(elements.get('reminders').innerHTML.includes(rendered),'Both views share each row');
      }
      run(`$('upcomingMore').open=true;renderReminders();`);assert.match(elements.get('upcoming').innerHTML,/id="upcomingMore" open/);
      const posts=[];context.confirm=()=>true;context.posts=posts;
      run(`api=async(path,body)=>{posts.push({path,body});data.reminders.items=data.reminders.items.filter(r=>r.id!==body.reminder_id);};sync=async()=>renderReminders();`);
      await listeners.click({target:{closest:()=>({dataset:{cancelReminder:'r1'}})}});
      assert.deepEqual(JSON.parse(JSON.stringify(posts)),[{path:'reminders/cancel',body:{reminder_id:'r1'}}]);
      for(const id of ['upcoming','reminders']) assert.doesNotMatch(elements.get(id).innerHTML,/data-cancel-reminder="r1"/);
      run('data.reminders.items=[];renderReminders();');assert.equal(elements.get('upcoming').hidden,true);assert.equal(elements.get('upcoming').innerHTML,'');
      break;
    }
    case 'v21-cards-and-chips': {
      run(`data.tasks=[];messages=new Map([[1,{id:1,role:'router',text:'Confirmed <tag>',tasks:['t9'],workers:[{id:'t9',code_name:'Backend <name>',name:'System <pages>',status:'running'}],steps:[{name:'mcp__router__reminder_add',hidden:true},{name:'mcp__router__reply'}],reminders:[{id:'r1',kind:'notify',next_time:'Tomorrow <time>',text:'原文 <script> & details',cron:'0 9 * * *'}]}]]);renderThread(true);`);
      const markup=elements.get('thread').innerHTML;
      assert.match(markup,/class="chip worker-chip" data-task="t9"/);assert.match(markup,/<svg class="robot/);
      assert.match(markup,/<b>Backend &lt;name&gt;<\/b><small>· System &lt;pages&gt;<\/small>/);
      assert.match(markup,/class="reminder-card"/);assert.match(markup,/⏰ Tomorrow &lt;time&gt; · 原文 &lt;script&gt; &amp; details/);
      assert.match(markup,/Added a reminder · notify/);assert.doesNotMatch(markup,/Using reminder_add|tool calls|actions ▸|<script>|<name>|<pages>/);
      const opened=[];context.opened=opened;run('openTask=id=>opened.push(id);');
      await listeners.click({target:{closest:()=>({dataset:{task:'t9'}})}});assert.deepEqual(opened,['t9']);
      run(`data.tasks=[{id:'t9',code_name:'Updated identity',name:'System pages'}];renderThread();`);
      assert.match(elements.get('thread').innerHTML,/Updated identity/,'Identity participates in the render fingerprint');
      break;
    }
    case 'v21-tool-summaries': {
      for(const summary of ['Added a reminder','Started Pixel','Sent a message to Pixel','Created item Web','Updated item Web']) assert.equal(run(`routerToolSentence({summary:${JSON.stringify(summary)},name:'event'})`),summary);
      assert.equal(run(`routerToolSentence({name:'mcp__router__reply'})`),'');
      assert.equal(run(`routerToolSentence({name:'mcp__router__reminder_add'})`),'Used reminder_add','Unconfirmed or failed calls must not claim success');
      assert.equal(run(`routerToolSentence({name:'mcp__router__unknown'})`),'Used unknown');
      run(`messages=new Map([[1,{id:1,role:'router',text:'Done',steps:[{name:'task.started',summary:'Started <name>'},{name:'mcp__router__reply'}]}]]);state.open.add(1);renderThread(true);`);
      assert.match(elements.get('thread').innerHTML,/1 actions ▸/);assert.match(elements.get('thread').innerHTML,/Started &lt;name&gt;/);assert.doesNotMatch(elements.get('thread').innerHTML,/<name>|Used reply/);
      break;
    }
    case 'v21-name-priority': {
      run(`data.tasks=[{id:'t9',name:'Task',code_name:'Server identity',model:'opus',status:'running'}];renderActivity();renderTasks();renderCommunication();`);
      assert.equal(run(`workerName('t9')`),'Server identity');
      for(const id of ['taskList','chatRoutingRows','routeLanes']) assert.match(elements.get(id).innerHTML,/Server identity/);
      const markup=run(`drawerOverview({upstream:[{id:'t1001',code_name:'Remote <name>',name:'Outside state'}]})`);
      assert.match(markup,/Remote &lt;name&gt;/);assert.doesNotMatch(markup,/<name>/);
      break;
    }
    case 'v2-identity': {
      for(const id of ['t1','t24','t25']) assert.equal(run(`workerName('${id}')`),'Unknown Worker');
      assert.equal(run("workerName({id:'t9',code_name:'Chosen'})"),'Chosen');
      assert.equal(run("elapsed({status:'waiting',state_since:1000,activity:{run_started:100,run_ended:160}})"),'1m 00s','Needs-you duration is the actual run, not time waiting for the user');
      assert.equal(run("elapsed({status:'queued',created:1,activity:{run_started:null,run_ended:null}})"),'0s');
      for(const [model,harness,expected] of [['Claude Opus','claude-code','claude'],['sonnet','claude-code','claude'],['haiku','claude-code','claude'],['gpt-6-astra','claude-code','chatgpt'],['sol','claude-code','chatgpt'],['gpt-6','codex','codex'],['Muse','claude-code','muse'],['unknown','other',null]]) {
        assert.equal(run(`workerCharacter(${JSON.stringify({id:'t9',model,harness})})`),expected);
        assert.equal(run(`workerCharacter(${JSON.stringify({id:'t1',model,harness})})`),expected,'Task sequence never chooses the character');
      }
      run("data.characters=[{id:'claude',frameWidth:256,frameHeight:256}];data.harness_icon='/api/characters/harness-claude-code.png?v=1';data.tasks=[{id:'t9',code_name:'Iris',name:'Pages',model:'opus',harness:'claude-code',status:'running'}];renderActivity();renderTasks();renderCommunication();");
      for(const id of ['taskList','chatRoutingRows','routeLanes']) {
        const markup=elements.get(id).innerHTML;assert.match(markup,/Iris/);assert.match(markup,/opus/);assert.match(markup,/Claude Code/);assert.match(markup,/data-character="claude"/);assert.match(markup,/data-harness-icon/);
      }
      assert.match(run("workerAvatar({id:'t2',model:'unknown',status:'running'})"),/^<svg/);
      run('data.characters=[];data.harness_icon=null;');assert.match(run("workerAvatar({id:'t1',model:'opus',status:'running'})"),/^<svg/);
      assert.doesNotMatch(run("harnessTag({harness:'claude-code'})"),/<img/);
      assert.equal(run("workerCard(data.tasks[0],'lane')===laneMarkup(data.tasks[0])"),true,'Both Routing views share the card renderer');
      run("state.tableFilter='ended';data.tasks=[{id:'t9',code_name:'Sleeper',status:'dormant',model:'opus'}];renderTasks();");
      assert.equal(run("safeStatus('dormant')"),'dormant');assert.match(elements.get('taskList').innerHTML,/Dormant/);
      assert.equal(run("robotState({status:'dormant'})"),'dormant');
      assert.match(html,/\.robot\[data-robot="dormant"\], \.worker-sprite\[data-robot="dormant"\] \{ opacity:\.5; \}/);
      assert.equal(run("workerSub({id:'t9',name:'Pages',role:'Entries <safe>'})"),'Pages · Entries &lt;safe&gt;');
      assert.equal(run("workerSub({id:'t9',name:'Pages'})"),'Pages');
      for(const id of ['taskList','chatRoutingRows','routeLanes']) assert.doesNotMatch(elements.get(id).innerHTML.replace(/<[^>]*>/g,''),/\bt\d+\b/);
      assert.doesNotMatch(script,/workerNumber|WORKER_NAMES/);
      const requests=[...script.matchAll(/api\(`workers\/[^`]+`/g)].map(match=>match[0]);
      assert.equal(requests.length,4,'Detail, question preflight, actions and diff all use immutable ids');
      assert.ok(requests.every(request=>request.includes('by=id')));
      break;
    }
    case 'v2-sprites': {
      run("const art={id:'claude',atlas:'/api/characters/claude.atlas',frameWidth:16,frameHeight:16,columns:2,rows:4,animations:Object.fromEntries(['idle','working','waiting','complete'].map((key,row)=>[key,{row,frames:2,durations:[100,200]}]))};data.characters=[art];");
      assert.equal(run("spriteFrame(art,'working',0).row"),1);assert.equal(run("spriteFrame(art,'working',150).frame"),1);
      assert.equal(run("spriteFrame(art,'complete',299).row"),3);assert.equal(run("spriteFrame(art,'complete',300).row"),0);assert.equal(run("spriteFrame(art,'complete',1000).row"),0);
      const draws=[], canvas={dataset:{},width:16,height:16,getContext:()=>({clearRect(){},drawImage(...args){draws.push(args);}})};
      const classes=new Set(), root={dataset:{character:'claude',worker:'t1',ended:'10',robot:'done'},classList:{toggle:(c,on)=>on?classes.add(c):classes.delete(c),remove:c=>classes.delete(c)},getClientRects:()=>[{}],getBoundingClientRect:()=>({top:0,bottom:32}),querySelector:()=>canvas};
      document.querySelectorAll=sel=>sel==='.worker-sprite'?[root]:[];context.window={innerHeight:800};context.requestAnimationFrame=()=>1;
      run("spriteImages.set(art.atlas,{complete:true,naturalWidth:32,naturalHeight:64});const reducedMotion={matches:false};paintWorkerSprites(1000);");
      assert.ok(classes.has('sprite-ready'));assert.equal(draws.at(-1)[2],48);
      run('paintWorkerSprites(1350);');assert.equal(draws.at(-1)[2],0,'Complete returns to idle without replay');
      canvas.dataset={};run('paintWorkerSprites(1600);');assert.equal(draws.at(-1)[2],0,'Repainting another surface does not restart completion');
      root.dataset.robot='running';run('reducedMotion.matches=true;paintWorkerSprites(1700);');assert.equal(draws.at(-1)[2],0);assert.equal(run('spriteRequest'),0,'Reduced motion schedules no animation loop');
      run('spriteImages.get(art.atlas).naturalWidth=0;paintWorkerSprites(1800);');assert.ok(!classes.has('sprite-ready'),'Failed or invalid images keep SVG fallback');
      document.hidden=true;const count=draws.length;run('paintWorkerSprites(2000);');assert.equal(draws.length,count);
      break;
    }
    case 'v2-inspect': {
      run(`state.task='t9';detail={task:{id:'t9',code_name:'Iris',name:'Pages <script>',model:'gpt-6-astra',harness:'claude-code',session_id:'session-123',status:'running',upstream:[],downstream:[],activity:{run_started:100}},timeline:[{kind:'received',text:'First message',time:100},{kind:'tool_call',tool:'Bash',text:'{"command":"printf safe"}',time:101},{kind:'said',text:'**Result** <script>bad</script>',time:102}],total:3,previous_offset:null,offset:0};renderModal(true);`);
      const markup=elements.get('modalBox').innerHTML;
      for(const label of ['Iris','gpt-6-astra','Claude Code','session-123','Resume in terminal','More','Interrupt','Stop','Message Iris']) assert.ok(markup.includes(label),label);
      assert.ok(markup.indexOf('First message')<markup.indexOf('Bash · printf safe'));assert.ok(markup.indexOf('Bash · printf safe')<markup.indexOf('<strong>Result</strong>'));
      assert.match(markup,/<details class="session-entry session-tool"[^>]* >/, 'Tools begin folded');
      assert.match(markup,/&lt;script&gt;/);assert.doesNotMatch(markup,/<script>|&quot;command&quot;|claude --resume/);
      const copied=[];context.navigator={clipboard:{writeText:async text=>copied.push(text)}};
      await run('copyWorkerSession()');await run('copyWorkerSession(true)');assert.deepEqual(copied,['session-123','supersuperharness open Iris']);
      assert.match(elements.get('sessionNotice').textContent,/Exit to hand it back/);
      context.navigator.clipboard.writeText=async()=>{throw Error('Denied');};await run('copyWorkerSession(true)');assert.match(elements.get('sessionNotice').textContent,/Copy manually: supersuperharness open Iris/);
      run(`state.diff={scope:'committed',text:'<script>diff</script>'};detail.task.progress='New progress';renderModal();`);
      assert.match(elements.get('modalBox').innerHTML,/id="workerDiff" ><h4>committed/);assert.match(elements.get('modalBox').innerHTML,/&lt;script&gt;diff&lt;\/script&gt;/);
      const more=new Element('drawerMenuToggle'), close=new Element('drawerClose'), send=new Element('taskSend');
      for(const node of [more,close,send]) node.getClientRects=()=>[{}];
      const box=elements.get('modalBox');box.getAttribute=()=> 'true';box.querySelectorAll=selector=>selector.includes('summary')?[more,close,send]:[close,send];
      let prevented=false;listeners.keydown({key:'Tab',target:send,preventDefault(){prevented=true;}});
      assert.ok(prevented);assert.equal(document.activeElement,more,'Overlay Tab wraps to the More summary');
      assert.equal(run("transcriptText({kind:'event',tool:'report.progress',text:'{\"text\":\"Checked it\"}'})"),'Checked it');
      assert.equal(run("routerToolSentence({name:'Read',text:'{\"input\":\"{\\\"file_path\\\":\\\"/a/b.md\\\"}\"}'})"),'Reading b.md');
      break;
    }
    case 'v2-completion': {
      run("connected=true;traffic.ready=true;data.tasks=[{id:'t1',status:'running'}];retainCompletedRoutes({tasks:[{id:'t1',status:'completed'}]});data.tasks=[{id:'t1',status:'completed'}];renderCommunication();");
      assert.match(elements.get('chatRoutingMap').innerHTML,/data-comm-edge="t1"/,'Completion still has a return wire');
      const timer=[...timers.values()].find(t=>t.delay>=9900 && t.delay<=10000);assert.ok(timer);run("routingDepartures.set('t1',Date.now()-1)");timer.fn();
      assert.equal(run('liveTasks().length'),0,'Finished lane folds away after the flight');
      run("clearTraffic(true);data.tasks=[{id:'t1',status:'running'}];retainCompletedRoutes({tasks:[{id:'t1',status:'completed'}]});");
      assert.equal(run('routingDepartures.size'),0,'Reconnect never replays historical completion');
      break;
    }
    case 'v2-navigation': {
      const nav=html.slice(html.indexOf('<nav class="rail">'),html.indexOf('</nav>'));
      assert.deepEqual([...nav.matchAll(/data-view="([^"]+)"/g)].map(m=>m[1]),['chat','tasks','files','models']);
      assert.doesNotMatch(html,/对话|运行现场/);assert.doesNotMatch(nav,/Developer|Usage/);
      run('let modelLoads=0;function loadModels(){modelLoads++;}navigate("models");');assert.equal(run('state.view'),'models');assert.equal(run('modelLoads'),1);
      assert.equal(elements.get('viewTitle').textContent,'Models');run('navigate("developer");');assert.equal(run('state.view'),'models');
      assert.match(html,/max-width:760px/);assert.match(html,/state.chatRoutingOpen=.*max-width:760px/);
      assert.match(html,/#taskList \{ position:relative; overflow:auto;/,'The absolute screen-reader heading stays inside the table scroll container');
      assert.match(html,/#taskList table \{ table-layout:fixed; min-width:920px;/);
      assert.match(html,/#taskList thead th:nth-child\(4\) \{ width:16%;/,'Updated has its own column, not zero width behind Inspect');
      break;
    }

    case 'c3-dispatch': {
      run("const flights=[];flow=(...args)=>flights.push(args);reactReport=()=>{};connected=true;data.time=100;data.tasks=[];reconcileTraffic([],new Set(),traffic.epoch);");
      run("data.tasks=[{id:'t1',created:101,status:'running',progress:'',question:''}];reconcileTraffic([],new Set(['state']),traffic.epoch);");
      assert.equal(run("flights.filter(f=>f[0]==='t1' && f[1]===false && f[2]==='dispatch').length"),1);
      run("const rows=[{id:1,role:'router',tasks:['t1'],steps:[{id:10,name:'task.message',text:JSON.stringify({text:'Continue'})}]}];reconcileTraffic(rows,new Set(['message']),traffic.epoch);reconcileTraffic(rows,new Set(['message']),traffic.epoch);");
      assert.equal(run("flights.filter(f=>f[2]==='dispatch').length"),2,'Accepted send is deduplicated by durable event ID');
      run("rows[0].steps.push({id:11,name:'task.message',text:'{bad'});rows[0].tasks=['t1','t2'];reconcileTraffic(rows,new Set(['message']),traffic.epoch);");
      assert.equal(run("flights.filter(f=>f[2]==='dispatch').length"),2,'Do not guess a destination in ambiguous turns');
      run("rows[0].workers=[{id:'t2',code_name:'Pixel'}];rows[0].steps.push({id:12,name:'mcp__router__worker_assign',text:JSON.stringify({input:JSON.stringify({worker:'pIxEl',text:'Unique'})})},{id:13,name:'task.message',text:JSON.stringify({text:'Unique'})});reconcileTraffic(rows,new Set(['message']),traffic.epoch);");
      assert.equal(run("flights.filter(f=>f[0]==='t2' && f[2]==='dispatch').length"),1);
      run("rows[0].workers[0].code_name='Arch';rows[0].steps.push({id:14,name:'mcp__router__worker_say',task_id:'t2',text:JSON.stringify({input:JSON.stringify({worker:'Pixel',text:'Renamed'})})},{id:15,name:'task.message',text:JSON.stringify({text:'Renamed'})});reconcileTraffic(rows,new Set(['message']),traffic.epoch);");
      assert.equal(run("flights.filter(f=>f[0]==='t2' && f[2]==='dispatch').length"),2,'Durable targets survive renamed tool inputs');
      break;
    }
    case 'c3-reports': {
      run("const flights=[],receipts=[];flow=(...args)=>flights.push(args);reactReport=(...args)=>receipts.push(args);connected=true;data.time=100;data.tasks=[{id:'t1',status:'running',progress:'',question:''},{id:'t2',status:'waiting_deps',progress:'',question:'',wait_for:[{task_id:'t1',satisfied:false}]}];reconcileTraffic([],new Set(),traffic.epoch);");
      run("data.tasks[0].progress='Checked';reconcileTraffic([],new Set(['state']),traffic.epoch);data.tasks[0].question='Continue?';data.tasks[0].status='waiting';reconcileTraffic([],new Set(['state']),traffic.epoch);data.tasks[0].status='completed';data.tasks[1].wait_for[0].satisfied=true;reconcileTraffic([],new Set(['state']),traffic.epoch);");
      assert.deepEqual(JSON.parse(run('JSON.stringify(receipts.map(r=>r[1]))')),['progress','question','done']);
      assert.equal(run("flights.filter(f=>f[2]==='dependency').length"),0,'Satisfied alone is not release');
      run("data.tasks[1].status='running';reconcileTraffic([],new Set(['state']),traffic.epoch);reconcileTraffic([],new Set(['state']),traffic.epoch);");
      assert.deepEqual(JSON.parse(run("JSON.stringify(flights.filter(f=>f[2]==='dependency').map(f=>f.slice(0,3)))")),[['t1:t2',false,'dependency']]);
      break;
    }
    case 'c3-history': {
      run("const flights=[];flow=(...args)=>flights.push(args);reactReport=()=>{};connected=true;data.time=100;data.tasks=[];const rows=[{id:1,role:'router',tasks:['t1'],steps:[{id:20,name:'task.message',text:'{\"text\":\"Old\"}'}]}];reconcileTraffic(rows,new Set(['message']),traffic.epoch);reconcileTraffic(rows,new Set(['message']),traffic.epoch);");
      assert.equal(run('flights.length'),0);
      run("state.historical=true;rows[0].steps.push({id:21,name:'task.message',text:'{\"text\":\"History\"}'});reconcileTraffic(rows,new Set(['message']),traffic.epoch);state.historical=false;reconcileTraffic(rows,new Set(['message']),traffic.epoch);clearTraffic(true);reconcileTraffic(rows,new Set(['message']),traffic.epoch);");
      assert.equal(run('flights.length'),0);
      run("data.tasks=[{id:'t9',created:50,status:'running',progress:'Old'}];reconcileTraffic([],new Set(['state']),traffic.epoch);");
      assert.equal(run('flights.length'),0,'Older tasks entering a bounded snapshot are not new dispatches');
      break;
    }
    case 'c3-motion': {
      const svg={append(){}}, effects=[];
      const path={dataset:{commEdge:'t1'},ownerSVGElement:svg,getAttribute:()=> 'M0 0 L100 0',getTotalLength:()=>100,getPointAtLength:x=>({x,y:0}),getBoundingClientRect:()=>({top:10,bottom:90}),closest:()=>null};
      document.getElementById('commMap').querySelectorAll=()=>[path];
      document.getElementById('commMap').getBoundingClientRect=()=>({top:0,bottom:100});document.getElementById('view-live').getBoundingClientRect=()=>({top:0,bottom:100});
      document.createElementNS=()=>({dataset:{},setAttribute(){},append(){},remove(){this.removed=true;},animate(frames,options){effects.push(this);return this.animation={finished:new Promise(()=>{}),cancel(){this.cancelled=true;}};}});
      run("const reducedMotion={matches:false};connected=true;state.view='live';$('communication').open=true;");
      for(let i=0;i<40;i++){path.dataset.commEdge='t'+i;run(`flow('t${i}',false,'dispatch')`);}
      assert.equal(run('traffic.particles.size'),8);
      run('clearTraffic();');assert.ok(effects.every(n=>n.removed && n.animation.cancelled));
      path.dataset.commEdge='t1';run("let flashes=0;flashTraffic=()=>flashes++;reducedMotion.matches=true;flow('t1',false,'dispatch');");
      assert.equal(run('traffic.particles.size'),0);assert.equal(run('flashes'),1);
      document.hidden=true;run("flow('t1',true,'done')");assert.equal(run('flashes'),1);
      break;
    }
    case 'c3-symbols': {
      assert.match(html,/comm-envelope/);assert.match(html,/comm-symbol/);
      assert.match(html,/\.comm-particle, \.comm-particle \* \{ pointer-events:none; \}/);
      assert.match(html,/comm-arrival/);
      assert.doesNotMatch(html,/<(?:script|link)[^>]*(?:src|href)="https?:/);
      break;
    }
    case 'c3-suber': {
      let paints=0;context.paint=()=>paints++;
      const profile={classList:{toggle(){}},getClientRects:()=>[{}]};
      document.querySelector=selector=>selector==='.router-profile'?profile:new Element('other');
      run("const reducedMotion={matches:false};connected=true;state.view='chat';renderAvatars=()=>paint();reactReport('t1','done');");
      assert.equal(run('suberReaction'),'done');assert.ok(paints>0);
      const reaction=[...timers.values()].find(t=>t.delay===1100);assert.ok(reaction);reaction.fn();
      assert.equal(run('suberReaction'),null);
      run("reducedMotion.matches=true;reactReport('t1','question');");assert.equal(run('suberReaction'),null);
      run("clearTraffic(true);");assert.equal(run('traffic.effects.size'),0);
      break;
    }
    case 'c4-progress': {
      const say=(name,input)=>run(`toolSentence(${JSON.stringify(name)},${JSON.stringify(input)})`);
      assert.equal(say('Bash','{"command": "rm -rf build"}'),'Running a command');
      assert.equal(say('Read','{"file_path": "/repo/docs/items.md"}'),'Reading items.md');
      assert.equal(say('Edit','{"file_path": "/repo/web/index.html", "old_string": "x"}'),'Editing index.html');
      assert.equal(say('Write','{"file_path": "/repo/notes.txt"}'),'Editing notes.txt');
      assert.equal(say('Read','{"file_path": "/Users/someone/very/long/path/that-was-clipp…'),'Reading a file','A clipped path never shows half a name');
      assert.equal(say('Agent','{"prompt": "x"}'),'Started a sub-agent');assert.equal(say('Task','{}'),'Started a sub-agent');
      assert.equal(say('mcp__browser__click','{}'),'Using click');assert.equal(say('Grep','{"pattern": "x"}'),'Using Grep');
      const human=t=>run(`humanProgress(${JSON.stringify(t)})`);
      assert.equal(human({progress:'Bash {"command": "sleep 900"}'}),'Running a command','Raw tool JSON is never shown');
      assert.equal(human({progress:'Bash {"command": "x"}',activity:{reported:'Checking <layout>',tool:{name:'Bash',input:'{}'}}}),'Checking <layout>','The Worker\'s own words win');
      assert.equal(human({progress:'',activity:{reported:'',tool:{name:'Read',input:'{"file_path": "/a/b.py"}'}}}),'Reading b.py');
      assert.equal(human({progress:'Plain words from the Worker'}),'Plain words from the Worker');
      run(`data.tasks=[{id:'t1',name:'Work',status:'running',progress:'Edit {"file_path": "/a/<x>.js"}'}];renderActivity();`);
      assert.match(run('workerCard(data.tasks[0])'),/Editing &lt;x&gt;\.js/);assert.doesNotMatch(run('workerCard(data.tasks[0])'),/file_path/);
      run(`data.tasks=[{id:'t1',name:'Work',status:'running',progress:'Bash {"command": "x"}',updated:1}];state.tableFilter='active';state.query='';renderTasks();`);
      assert.match(elements.get('taskList').innerHTML,/Running a command/);assert.doesNotMatch(elements.get('taskList').innerHTML,/&quot;command&quot;/);
      break;
    }
    case 'c4-groups': {
      run(`data.items=[{code:'ui-1',name:'UI <one>'},{code:'ops-1',name:'Ops'}];data.counts={running:4};data.tasks=[{id:'t1',status:'running',name:'Loose'},{id:'t2',status:'running',item:'ops-1',name:'Ops work'},{id:'t3',status:'queued',item:'ui-1',name:'UI work'},{id:'t4',status:'running',item:'gone-1',name:'Unknown item'}];state.tableFilter='active';state.query='';renderTasks();`);
      assert.deepEqual(JSON.parse(run('JSON.stringify(workerGroups(filteredTasks()).map(([code,rows])=>[code,rows.map(t=>t.id)]))')),[['ui-1',['t3']],['ops-1',['t2']],['gone-1',['t4']],['',['t1']]],'Item order follows items; unassigned goes last');
      const markup=elements.get('taskList').innerHTML;
      assert.match(markup,/data-item="ui-1"/);assert.match(markup,/UI &lt;one&gt;/);assert.match(markup,/Unassigned/);
      assert.ok(markup.indexOf('data-item="ui-1"')<markup.indexOf('data-item=""'));
      run("state.query='ops';renderTasks();");assert.deepEqual(JSON.parse(run('JSON.stringify(filteredTasks().map(t=>t.id))')),['t2'],'Search matches the item name too');
      await listeners.click({target:{closest:()=>({dataset:{table:'running'}})}});assert.equal(run('state.tableFilter'),'active','Only the three group filters are accepted');
      break;
    }
    case 'c4-routing': {
      const now=Date.now()/1000;
      run(`connected=true;data.router={model:'main-model',avatar:{name:'Router',state:'working'}};data.items=[{code:'ui-1',name:'UI'}];data.tasks=[{id:'t1',code_name:'Nova',name:'Build <it>',status:'running',model:'m',item:'ui-1',progress:'Bash {"command":"x"}',wait_for:[],activity:{run_started:${now}-90,reported:'',tool:{name:'Bash',input:'{}'},exchange:{type:'report.progress',text:'Half way',time:${now}-5}}},{id:'t2',name:'Ask',status:'waiting',model:'m',question:'Ship <it>?',wait_for:[]},{id:'t3',name:'Summary',status:'waiting_deps',model:'m',wait_for:[{task_id:'t1',satisfied:false},{task_id:'t9',satisfied:true}]},{id:'t4',name:'Paused',status:'interrupted',model:'m',wait_for:[]},{id:'t5',name:'Old',status:'completed',model:'m',ended:${now},wait_for:[]},{id:'t6',name:'Broke',status:'failed',model:'m',ended:${now},wait_for:[]},{id:'t7',name:'Yesterday',status:'completed',model:'m',ended:${now}-172800,wait_for:[]}];renderCommunication();`);
      const map=elements.get('commMap').innerHTML, lanes=elements.get('routeLanes').innerHTML, ended=elements.get('routingEnded').innerHTML;
      for(const id of ['t1','t2','t3']) assert.ok(map.includes(`data-comm-edge="${id}"`) && lanes.includes(`data-task="${id}"`));
      for(const id of ['t4','t5','t6','t7']) assert.ok(!lanes.includes(`data-task="${id}"`));
      assert.match(map,/data-comm-edge="user"/);assert.match(map,/data-dependency="t1:t3"/);assert.doesNotMatch(map,/data-dependency="t9:t3"/);
      assert.match(map,/Build &lt;it&gt;/);assert.match(lanes,/Build &lt;it&gt;/);assert.doesNotMatch(lanes+map,/<it>/);
      assert.match(lanes,/Running a command/);assert.match(lanes,/Running a command/);assert.match(lanes,/Needs you: Ship &lt;it&gt;\?/);
      assert.match(lanes,/Waiting on Nova/);assert.match(lanes,/data-task="t2"/);assert.match(lanes,/harness-tag/);
      assert.equal(run("elapsed(data.tasks[0])"),'1m 30s','Duration counts from the latest run start, not creation');
      assert.match(ended,/1 done today · 1 failed or stopped/);assert.match(ended,/data-task="t5"/);assert.doesNotMatch(ended,/data-task="t7"/);
      assert.doesNotMatch(ended,/ open>/,'Finished Workers start folded');
      run("data.tasks=data.tasks.filter(t=>!['t1','t2','t3'].includes(t.id));renderCommunication();");
      assert.match(elements.get('commMap').innerHTML,/No active Workers/);assert.match(elements.get('commMap').innerHTML,/suber-rest/);
      break;
    }
    case 'c4-flights': {
      const svg={children:[],append(...nodes){this.children.push(...nodes);}};
      const lane={dataset:{commEdge:'t2'},ownerSVGElement:svg,getAttribute:()=> 'M0 0 L100 0',getTotalLength:()=>160,getPointAtLength:x=>({x,y:0}),getBoundingClientRect:()=>({top:10,bottom:90}),closest:()=>null};
      document.getElementById('commMap').querySelectorAll=selector=>selector==='[data-comm-edge]'?[lane]:[];
      document.getElementById('commMap').getBoundingClientRect=()=>({top:0,bottom:100});document.getElementById('view-live').getBoundingClientRect=()=>({top:0,bottom:100});
      document.createElementNS=()=>({dataset:{},children:[],setAttribute(){},append(...n){this.children.push(...n);},remove(){},animate(){return {finished:new Promise(()=>{}),cancel(){}};}});
      run("const reducedMotion={matches:false};connected=true;state.view='live';$('communication').open=true;data.time=1;data.tasks=[{id:'t2',status:'waiting_deps',wait_for:[{task_id:'t1',satisfied:false}]}];reconcileTraffic([],new Set(),traffic.epoch);data.tasks=[{id:'t2',status:'running',wait_for:[{task_id:'t1',satisfied:true}]}];reconcileTraffic([],new Set(['state']),traffic.epoch);");
      assert.equal(svg.children.length,2,'A release has one envelope and one trail on the downstream lane');
      assert.equal(svg.children[1].dataset.kind,'release');assert.equal(svg.children[1].dataset.direction,'to-worker');
      assert.equal(run('trafficKinds.release.mail'),true);
      lane.getClientRects=()=>[];run("flow('t2',true,'progress')");assert.equal(svg.children.length,2,'Hidden wires do not animate');
      break;
    }
    case 'c4-drawer': {
      run(`data.profiles=[{name:'work',model:'Model'}];data.items=[{code:'ui-1',name:'UI'}];state.task='t2';state.reportOpen=false;detail={task:{id:'t2',name:'Review',model:'Model',profile:'work',item:'ui-1',status:'completed',progress:'Read {"file_path": "/a/b.md"}',report:'**Done**: shipped it.\\n\\n## Details\\n<script>x</script> more',brief:'Brief',branch:'w/pixel',upstream:[],downstream:[],activity:{run_started:100,reported:'',tool:null,exchange:null},ended:160},timeline:[],total:0,next_offset:null};renderModal(true);`);
      const markup=elements.get('modalBox').innerHTML;
      assert.match(markup,/<div class="message-text markdown report-lead"><p><strong>Done<\/strong>: shipped it\.<\/p><\/div>/,'Only the conclusion paragraph is open');
      assert.match(markup,/<details id="drawerReport" class="drawer-report" ><summary id="drawerReportToggle">Full report/);
      assert.match(markup,/&lt;script&gt;x&lt;\/script&gt;/);assert.doesNotMatch(markup,/<script>/);
      assert.match(markup,/Reading b\.md/);assert.match(markup,/This run/);assert.equal(run('elapsed(detail.task)'),'1m 00s');
      for(const label of ['Interrupt','Resume','Switch model','View diff','Stop']) if(label!=='Interrupt' && label!=='Resume' && label!=='Stop') assert.ok(markup.includes('>'+label+'<'));
      assert.match(html,/confirm\('Stop this Worker\? Its workspace and branch are kept\.'\)/);assert.match(html,/\.btn\.danger \{ color: var\(--failed\); \}/);
      run("detail.task.status='running';renderModal(true);");assert.match(elements.get('modalBox').innerHTML,/class="btn danger" data-action="stop" id="drawerStop">Stop</);
      break;
    }
    case 'c4-english': {
      // Usage and Soul & Memory belong to the parallel system-page work (t9); they are excluded here.
      const ui=html.replace(/<script>[\s\S]*?<\/script>/,m=>m.replace(/\/\/[^\n]*/g,'').replace(/function renderUsage\(\)[\s\S]*?\n\}\n/,'').replace(/const usageNumber[^\n]*/,'').replace(/<p class="page-sub">建设中 · /,''));
      const cjk=ui.match(/[一-鿿]+/g)||[];
      assert.deepEqual(cjk,[],'Interface copy is English; user and Agent text is data, not markup');
      for(const label of ['>Chat<','>Routing<','>Workers<','Needs you','View Workers']) assert.ok(html.includes(label),label);
      assert.match(html,/\.page-inner \{ max-width: 1280px;/);assert.match(html,/\.conversation-content \{ max-width:1000px;/);
      break;
    }
    case 'c4-panel': {
      run(`connected=true;state.view='chat';state.chatRoutingOpen=true;data.router={avatar:{name:'Router',state:'working'}};data.items=[{code:'ui-1',name:'UI'}];data.tasks=[{id:'t9',name:'System <pages>',status:'running',model:'opus',item:'ui-1',progress:'Edit {"file_path": "/w/index.html"}',wait_for:[]},{id:'t3',name:'Ask',status:'waiting',model:'m',question:'Ship?',wait_for:[]},{id:'t5',name:'Done',status:'completed',model:'m',ended:1,wait_for:[]}];$('avatarState').textContent='working';renderCommunication();`);
      const map=elements.get('chatRoutingMap').innerHTML, rows=elements.get('chatRoutingRows').innerHTML;
      assert.match(map,/data-comm-edge="t9"/);assert.match(map,/data-comm-edge="t3"/);assert.doesNotMatch(map,/data-comm-edge="t5"/);
      assert.match(html,/id="chatRoutingExpand"[^>]*title="Expand Routing"/,'Expand opens the full Routing page');
      assert.match(rows,/data-task="t9"/);assert.match(rows,/System &lt;pages&gt;/);assert.doesNotMatch(rows,/<pages>/);
      assert.match(rows,/Editing index\.html/);assert.match(rows,/Needs you: Ship\?/);assert.doesNotMatch(rows,/file_path/);
      assert.equal(elements.get('chatRoutingCount').textContent,'2 active');assert.equal(elements.get('chatRouting').classList.contains('is-idle'),false);
      assert.equal(run("trafficStage().map===$('chatRouting')"),true,'Flights run on the chat panel while Chat is visible');
      run("state.chatRoutingOpen=false;renderCommunication();");
      assert.equal(elements.get('chatRoutingMap').innerHTML,'');assert.equal(elements.get('chatRouting').classList.contains('is-collapsed'),true);
      assert.equal(run('trafficStage()'),null,'A collapsed panel never animates');
      run("state.chatRoutingOpen=true;data.tasks=[];renderCommunication();");
      assert.equal(elements.get('chatRoutingStatus').textContent,'Router idle');assert.equal(elements.get('chatRouting').classList.contains('is-idle'),true);
      assert.equal(elements.get('chatRoutingCount').textContent,'idle');
      assert.match(html,/id="chatRoutingStatus" class="route-status">Router idle/);
      break;
    }
    case 'c4-robots': {
      const look=t=>run(`robotState(${JSON.stringify(t)})`);
      assert.equal(look({status:'running'}),'running');assert.equal(look({status:'waiting'}),'waiting');
      assert.equal(look({status:'queued'}),'pending');assert.equal(look({status:'waiting_deps'}),'pending');
      run('clockOffset=0;');const now=Date.now()/1000;
      assert.equal(look({status:'completed',ended:now-2}),'done');assert.equal(look({status:'completed',ended:now-60}),'rest');
      const robot=run(`robotMarkup({id:'t1',status:'waiting',item:'ui-1" onload="bad'})`);
      assert.match(robot,/<g class="robot-badge ask">/);assert.match(robot,/class="robot tone-\d"/);assert.doesNotMatch(robot,/onload|ui-1/,'Only fixed shapes and a derived colour class');
      assert.match(run(`robotMarkup({id:'t1',status:'running'})`),/class="robot tone-none" data-robot="running"/);
      assert.match(html,/\.robot\[data-robot="pending"\] \{ opacity:\.45; \}/);assert.match(html,/prefers-reduced-motion:reduce\) \{ \.robot \* \{ animation:none !important; \} \}/);
      run(`data.tasks=[{id:'t9',code_name:'Iris',name:'System pages',status:'running',model:'opus',progress:'Read {"file_path": "/w/a.md"}'}];renderActivity();`);
      const card=run('workerCard(data.tasks[0])');
      assert.match(card,/<svg class="robot[^>]*data-robot="running"/);assert.match(card,/<b>Iris<\/b>/);assert.match(card,/<span class="model-tag" title="opus">opus<\/span>/);assert.match(card,/Reading a\.md/);assert.match(card,/<small class="mini-sub">System pages<\/small>/);assert.match(card,/data-task="t9"/);
      run(`messages=new Map([[1,{id:1,role:'router',text:'Hi',created:1,channel:'web'}]]);renderThread(true);`);
      assert.match(elements.get('thread').innerHTML,/<span class="avatar router-mini" aria-hidden="true"><\/span><div class="msg-body"><div class="msg-meta"><b>Router<\/b>/);
      break;
    }
    case 'c2-option-reconcile': {
      const posts=[];context.posts=posts;let seq=0;context.crypto={randomUUID:()=> 'request-'+(++seq)};
      run(`data.router.question={id:21,text:'Question',options:['Yes']};data.tasks=[{id:'t2',status:'waiting',question_id:22,question:'Q2',options:['Two']},{id:'t3',status:'waiting',question_id:33,question:'Q3',options:['Three']}];syncRunning=true;api=async(path,body)=>{if(body) {posts.push({path,body});return {ok:true};} return path==='state'?data:{task:find(path.split('/')[1].split('?')[0])};};`);
      await run("chooseOption('router','21','0')");await run("chooseOption('router','21','0')");
      assert.equal(posts.length,1,'Accepted question stays consumed even if sync is already running');
      run('sync=async()=>{renderActivity();renderThread();};');
      await run("taskAction('t2','send',{text:'Free answer',expected_status:'waiting'})");
      assert.equal(run('actionBusy'),false);
      const choice=elements.get('confirmations').innerHTML.match(/<button[^>]*id="option-t3-33-0"[^>]*>/)[0];
      assert.doesNotMatch(choice,/disabled/,'Finishing another action must re-enable remaining choices');
      break;
    }
    case 'c2-option-preflight': {
      const posts=[];context.posts=posts;
      run("data.tasks=[{id:'t2',status:'waiting',question_id:10,options:['Old']}];api=async(path,body)=>{if(body) posts.push(body);return {task:{id:'t2',status:'waiting',question_id:11,options:['New']}};};taskAction=async(id,action,body)=>posts.push(body);");
      await run("chooseOption('t2','10','0')");assert.equal(posts.length,0);
      assert.match(elements.get('error').textContent,/changed/);
      break;
    }
    case 'c2-placeholder': {
      run("data.items=[{code:'ui-1',name:'UI',tasks:['t9']}];data.tasks=[];renderCommunication();");
      assert.doesNotMatch(elements.get('itemGraph').innerHTML,/data-comm-edge="t9"|data-timer="t9"/);
      run("state.task='t9';detail={task:{id:'t9',item:'ui-1',name:'Loaded',status:'running',wait_for:[]}};renderCommunication();");
      assert.match(elements.get('itemGraph').innerHTML,/data-comm-edge="t9"/);
      assert.match(elements.get('itemGraph').innerHTML,/data-timer="t9"/);
      run("data.items=[];renderCommunication();");assert.match(elements.get('itemGraph').innerHTML,/Item details not loaded/);
      run("data.items=[{code:'ui-1',name:'Loaded item',tasks:['t9']}];renderCommunication();");
      assert.doesNotMatch(elements.get('itemGraph').innerHTML,/Item details not loaded/);
      break;
    }
    case 'c2-drawer-focus': {
      const box=document.getElementById('modalBox');let markup='';
      const focus=document.getElementById('drawerRefresh');box.contains=n=>n?.id==='drawerRefresh';
      Object.defineProperty(box,'innerHTML',{get:()=>markup,set:value=>{markup=value;elements.set('drawerRefresh',new Element('drawerRefresh'));document.activeElement=null;}});
      run("state.task='t2';detail={task:{id:'t2',name:'Task',status:'waiting',question:'Go?',upstream:[{id:'t1',name:'Root',status:'completed',satisfied:true}],downstream:[]},timeline:[],total:0,next_offset:null};renderModal(true);");
      document.getElementById('drawerRefresh').focus();
      assert.match(markup,/id="drawerRefresh"/);
      run("detail.task.progress='New progress';renderModal();");
      assert.equal(document.activeElement.id,'drawerRefresh');
      assert.match(markup,/id="drawer-upstream-t1"/);assert.match(markup,/id="drawer-confirm-t2"/);
      break;
    }
    case 'c2-groups': {
      run(`data.items=[{code:'ui-1',name:'UI',tasks:['t1','t2']},{code:'empty-1',name:'Empty',tasks:[]}];data.tasks=[{id:'t1',name:'Done',status:'completed',item:'ui-1',wait_for:[]},{id:'t2',name:'Review',status:'waiting_deps',item:'ui-1',wait_for:[{task_id:'t1',satisfied:true}]},{id:'t3',name:'Loose',status:'running',item:null,wait_for:[]}];`);
      assert.deepEqual(JSON.parse(run('JSON.stringify(buildRunGroups().map(g=>[g.code,g.tasks.map(t=>t.id)]))')),[['ui-1',['t1','t2']],['empty-1',[]],[null,['t3']]]);
      run("data.tasks.forEach((t,i)=>t.code_name=['Nova','Pixel','Echo'][i]);renderCommunication();");
      const markup=elements.get('itemGraph').innerHTML;
      assert.doesNotMatch(markup.replace(/<[^>]*>/g,''),/\bt\d+\b/);
      assert.match(markup,/Nova → Pixel/);
      assert.match(markup,/data-item="ui-1"/);assert.match(markup,/Unassigned/);assert.match(markup,/data-dependency="t1:t2"/);
      assert.match(markup,/data-task="t1"/,'Completed upstreams remain visible');
      assert.match(markup,/No Workers in this item yet/);
      const node=new Element('graph-worker'), edge=new Element('graph-edge'), fields=new Map();
      node.dataset={task:'t1',role:'worker'};edge.dataset={dependency:'t1:t2'};
      node.querySelector=selector=>{if(!fields.has(selector)) fields.set(selector,{});return fields.get(selector);};
      edge.querySelector=()=>{if(!fields.has('edge-title')) fields.set('edge-title',{});return fields.get('edge-title');};
      elements.get('itemGraph').querySelectorAll=selector=>selector==='.comm-node'?[node]:selector==='[data-dependency]'?[edge]:[];
      run("const previousMap=lastMap;data.tasks[0].code_name='Arch';renderItemGraph();");
      assert.equal(run('previousMap===lastMap'),true,'Rename does not change graph topology');
      assert.equal(fields.get('.comm-code').textContent,'Arch');
      assert.match(fields.get('title').textContent,/Arch/);assert.doesNotMatch(fields.get('title').textContent,/\bt1\b/);
      assert.match(fields.get('edge-title').textContent,/Arch → Pixel/);
      run("data.items=[{code:'up-1',name:'Upstream',tasks:['t1']},{code:'down-1',name:'Downstream',tasks:['t2']}];data.tasks=[{id:'t2',code_name:'Pixel',name:'Review',item:'down-1',status:'waiting_deps',wait_for:[{task_id:'t1',code_name:'Gate',name:'Entry work',satisfied:false}]}];renderItemGraph();");
      assert.equal(fields.get('.comm-code').textContent,'Gate','Dependency metadata enriches an earlier anonymous item placeholder');
      assert.match(fields.get('edge-title').textContent,/Gate → Pixel/);
      break;
    }
    case 'c2-layout': {
      run(`data.items=[{code:'ui-1',name:'UI',tasks:['t3','t2','t1']}];data.tasks=[{id:'t3',status:'waiting_deps',item:'ui-1',wait_for:[{task_id:'t2',satisfied:false}]},{id:'t2',status:'waiting',item:'ui-1',wait_for:[{task_id:'t1',satisfied:true}]},{id:'t1',status:'running',item:'ui-1',wait_for:[]}];`);
      const plan=JSON.parse(run('JSON.stringify(layoutRunGroup(buildRunGroups()[0],2))'));
      const nodes=new Map(plan.nodes.map(n=>[n.id,n]));
      assert.ok(nodes.get('t1').y<nodes.get('t2').y && nodes.get('t2').y<nodes.get('t3').y);
      assert.deepEqual(plan.edges.map(e=>[e.from,e.to,e.satisfied]),[['t2','t3',false],['t1','t2',true]]);
      assert.equal(plan.cycle,false);
      const mobile=JSON.parse(run('JSON.stringify(layoutRunGroup(buildRunGroups()[0],1))'));
      assert.ok(mobile.width<=280);assert.equal(mobile.nodes.length,3);
      assert.ok(run("statuses.includes('waiting_deps')"));assert.equal(run("safeStatus('waiting_deps')"),'waiting_deps');
      const path=run("dependencyPath({x:24,y:184},{x:24,y:448})");
      assert.match(path,/ L12 /,'An edge skipping a phone row goes around the intervening Worker, not through it');
      assert.doesNotMatch(run("dependencyPath({x:24,y:184},{x:24,y:316})"),/ L/);
      break;
    }
    case 'c2-references': {
      run(`data.items=[{code:'ui-1',name:'UI',tasks:['t2']}];data.tasks=[{id:'t1',name:'External',item:'ops-1',status:'completed',wait_for:[]},{id:'t2',name:'Worker',item:'ui-1',status:'waiting_deps',wait_for:[{task_id:'t1',satisfied:true},{task_id:'t99',code_name:'Remote',name:'Outside state',satisfied:false}]}];`);
      const group=JSON.parse(run('JSON.stringify(buildRunGroups()[0])'));
      assert.equal(group.references.find(t=>t.id==='t1').name,'External');
      assert.equal(group.references.find(t=>t.id==='t99').missing,true);
      assert.equal(group.references.find(t=>t.id==='t99').code_name,'Remote');
      run('renderCommunication();');assert.match(elements.get('itemGraph').innerHTML,/External upstream/);assert.match(elements.get('itemGraph').innerHTML,/Not loaded/);
      const cycle=JSON.parse(run("JSON.stringify(layoutRunGroup({tasks:[{id:'a',wait_for:[{task_id:'b'}]},{id:'b',wait_for:[{task_id:'a'}]}],references:[]},2))"));
      assert.equal(cycle.cycle,true);assert.equal(cycle.nodes.length,2);
      break;
    }
    case 'c2-empty': {
      run('data.items=[];data.tasks=[];renderCommunication();');
      assert.match(elements.get('itemGraph').innerHTML,/No items or Workers yet/);assert.doesNotMatch(elements.get('itemGraph').innerHTML,/class="comm-node/);
      assert.match(elements.get('commMap').innerHTML,/No active Workers/);assert.doesNotMatch(elements.get('commMap').innerHTML,/data-task=|data-comm-edge="t/);
      run('data.items_truncated=true;data.tasks_truncated=true;renderCommunication();');
      assert.match(elements.get('commCaption').textContent,/truncated/);
      break;
    }
    case 'c2-escaping': {
      run(`data.items=[{code:'ui-1',name:'<img onerror="bad">',progress:'<svg onload=bad>',tasks:['t1']}];data.tasks=[{id:'t1',name:'<script>bad</script>',status:'waiting',item:'ui-1',progress:'<iframe>',question:'<button onclick=bad>',question_id:4,options:['<img src=x>'],wait_for:[]}];renderCommunication();renderActivity();state.task='t1';detail={task:{...data.tasks[0],model:'<model>',report:'<script>report</script>',brief:'<brief>',upstream:[],downstream:[]},timeline:[],total:0,next_offset:null};renderModal(true);`);
      const markup=elements.get('commMap').innerHTML+elements.get('routeLanes').innerHTML+elements.get('itemGraph').innerHTML+elements.get('confirmations').innerHTML+elements.get('modalBox').innerHTML;
      assert.match(markup,/&lt;img/);assert.match(markup,/&lt;script&gt;/);assert.match(markup,/&lt;iframe&gt;/);
      assert.doesNotMatch(markup,/<script|<iframe|<img\s|<button onclick|<svg onload/i);
      break;
    }
    case 'c2-drawer': {
      run(`data.profiles=[{name:'work',model:'Model'}];state.task='t2';detail={task:{id:'t2',name:'Review',model:'Model',profile:'work',status:'waiting',progress:'Checked entry',question:'Continue?',question_id:4,options:['Yes'],report:'Saved result',brief:'Brief',branch:'w/pixel',upstream:[{id:'t1',name:'Build',status:'completed',satisfied:true}],downstream:[{id:'t3',name:'Report',status:'waiting_deps',satisfied:false}]},timeline:[],total:0,next_offset:null};renderModal(true);`);
      const markup=elements.get('modalBox').innerHTML;
      for(const text of ['Checked entry','Saved result','Continue?','Waits on','Unblocks','Build','Report','More','id="drawerMenu"','Answer in chat']) assert.ok(markup.includes(text));
      assert.match(markup,/data-confirm="t2"/);assert.match(markup,/data-task="t1"/);assert.match(markup,/data-action="stop"/);assert.match(markup,/data-action="model"/);assert.match(markup,/data-diff="1"/);
      assert.match(html,/justify-content:flex-end/);assert.match(html,/aria-label="Inspect Worker"/);
      assert.match(markup,/id="drawer-option-t2-4-0"/,'Drawer choices use their own IDs');assert.doesNotMatch(markup,/id="option-t2-/);
      assert.ok(markup.includes('<details id="drawerMenu" class="more-menu" >'),'Actions stay behind a closed More menu');
      assert.match(markup,/class="session-transcript"/,'Conversation is visible by default');
      break;
    }
    case 'c2-options': {
      context.crypto={randomUUID:()=> 'option-request-id'};const sent=[];context.sent=sent;
      run(`data.tasks=[{id:'t2',name:'Confirm',status:'waiting',question:'Choose',question_id:9,options:['Yes <exact>','No'],wait_for:[]}];data.router.question={id:20,text:'Router ask',options:['Router <choice>']};messages.set(20,{id:20,role:'router',kind:'question',text:'Router ask',options:['Router <choice>']});renderActivity();renderThread();api=async path=>path==='state'?data:{task:find(path.split('/')[1].split('?')[0])};taskAction=async(id,action,body)=>{sent.push({id,action,body});return true;};send=async key=>{sent.push({text:pending.get(key).text});pending.delete(key);return true;};`);
      const markup=elements.get('confirmations').innerHTML;
      assert.match(markup,/Yes &lt;exact&gt;/);assert.match(markup,/data-reply-input="t2"/);
      assert.match(elements.get('thread').innerHTML,/Router &lt;choice&gt;/);
      await run("chooseOption('t2','9','0')");assert.deepEqual(JSON.parse(JSON.stringify(sent[0])),{id:'t2',action:'send',body:{text:'Yes <exact>',expected_status:'waiting'}});
      await run("chooseOption('router','20','0')");assert.equal(sent[1].text,'Router <choice>');
      break;
    }
    case 'c2-stale-options': {
      const sent=[];context.sent=sent;let release;context.waiting=()=>new Promise(resolve=>{release=resolve;});
      run("data.tasks=[{id:'t2',status:'waiting',question_id:10,options:['New']}];api=async()=>({task:data.tasks[0]});taskAction=async()=>{sent.push('sent');await waiting();};");
      await run("chooseOption('t2','9','0')");assert.equal(sent.length,0);
      await run("chooseOption('t2','10','99')");assert.equal(sent.length,0);
      const first=run("chooseOption('t2','10','0')");await run("chooseOption('t2','10','0')");assert.equal(sent.length,1);release();await first;
      break;
    }
    case 'c2-answered': {
      run(`data.router.avatar={state:'done'};data.router.question={id:2,text:'Repeated?',options:['Continue']};messages=new Map([[1,{id:1,role:'router',kind:'question',text:'Repeated?',options:['Old']}],[2,{id:2,role:'router',kind:'question',text:'Repeated?',options:['Continue']}] ]);renderThread();`);
      let markup=elements.get('thread').innerHTML;
      assert.equal((markup.match(/confirmation router-question/g)||[]).length,1);
      assert.ok(markup.includes('id="router-question-1"'));assert.ok(markup.includes('id="router-question-2"'));
      assert.doesNotMatch(markup,/data-router-reply="1"|>Old<\/button>/);
      run('data.router.question=null;renderThread();');
      markup=elements.get('thread').innerHTML;assert.doesNotMatch(markup,/confirmation router-question|data-router-reply=/);
      assert.equal((markup.match(/Repeated\?/g)||[]).length,2);
      break;
    }
    case 'c2-progress-focus': {
      const container=document.getElementById('progressSummary');let markup='',paint=0;
      container.contains=element=>element?.id==='progressView';
      Object.defineProperty(container,'innerHTML',{get:()=>markup,set:value=>{markup=value;paint++;elements.set('progressView',new Element('progressView'));document.activeElement=null;}});
      run("data.tasks=[{id:'t2',name:'Working',status:'running',progress:'First'}];renderActivity();");
      const first=document.getElementById('progressView');first.focus();
      run("data.tasks[0].progress='Second';renderActivity();");
      assert.equal(document.activeElement,first,'Progress no longer repaints the compact summary');
      run("data.tasks.push({id:'t3',status:'running'});renderActivity();");
      assert.notEqual(document.activeElement,first);assert.equal(document.activeElement.id,'progressView');
      const changed=paint;run('renderActivity();');assert.equal(paint,changed,'Unchanged count does not repaint');
      break;
    }
    case 'c2-snapshot': {
      const node={dataset:{role:'worker',task:'t9'},classList:{toggle(){}},setAttribute(){},querySelector(){return {textContent:''};}};
      const edge={dataset:{dependency:'t1:t9'},querySelector(){return {textContent:''};}};
      document.getElementById('commMap').querySelectorAll=selector=>selector==='.comm-node'?[node]:selector==='[data-dependency]'?[edge]:[];
      run("data.items=[];data.tasks=[{id:'t9',name:'Fresh',status:'completed',wait_for:[]}];state.task=null;detail={task:{id:'t9',name:'Old',status:'running',wait_for:[]}};renderCommunication();");
      assert.equal(node.dataset.status,'completed','Closed drawer metadata must not override a newer live snapshot');
      run("data.tasks=[];state.task='t9';detail.task.item='ui-1';detail.task.wait_for=[{task_id:'t1',satisfied:true}];renderCommunication();");
      assert.equal(edge.dataset.satisfied,'true','Selected metadata outside the bounded snapshot still has real dependency satisfaction');
      run('state.task=null;renderCommunication();');assert.equal(run('buildRunGroups().length'),0);
      break;
    }
    case 'c2-links': {
      const node={dataset:{task:'t2',reference:'false'},scrollIntoView(){assert.equal(run('state.task'),'t2','Scroll only after the drawer establishes final graph geometry');},focus(){}};
      document.getElementById('commMap').querySelectorAll=selector=>selector==='[data-task]'?[node]:[];
      run("data.items=[{code:'ui-1',name:'UI',tasks:['t2']}];data.tasks=[{id:'t2',name:'Confirm',item:'ui-1',status:'waiting',question:'Go?',wait_for:[]}];loadDetail=()=>{};showWorker('t2');");
      assert.equal(run('state.view'),'live');assert.equal(run('state.task'),'t2');assert.equal(run('state.selectedTask'),'t2');
      assert.equal(elements.get('communication').open,true);
      run("showConfirmation('t2');");assert.equal(run('state.view'),'chat');assert.equal(run('state.task'),null);assert.equal(document.activeElement.id,'confirm-t2');
      break;
    }
    case 'c1-skeleton': {
      assert.match(html,/<aside id="chatRouting"/);assert.doesNotMatch(html,/id="profileName"|id="profileConnection"/);
      assert.match(html,/<div class="brand">Super Super Harness<\/div>/);
      assert.equal((html.match(/data-router-avatar>/g)||[]).length,1,'Only the conversation corner contains a character');
      assert.match(html,/id="view-live"/); assert.match(html,/data-view="live"[^>]*id="chatRoutingExpand"/);
      assert.match(html,/id="headerWaiting"/); assert.match(html,/id="headerRunning"/);
      assert.match(html,/id="taskSearch"/); assert.match(html,/id="reminders"/);
      assert.doesNotMatch(run("messages=new Map([[1,{id:1,role:'router',text:'reply'}]]);renderThread();$('thread').innerHTML"),/data-router-avatar/);
      break;
    }
    case 'c1-confirmations': {
      run(`data.tasks=[{id:'t1',name:'Review <one>',status:'waiting',question:'Continue <script>?',progress:'Checked & ready'}, {id:'t2',name:'Two',status:'waiting',question:'Same question'}, {id:'t3',name:'Running',status:'running',progress:'busy'}]; renderActivity();`);
      const markup=elements.get('confirmations').innerHTML;
      for(const id of ['t1','t2']) {
        assert.ok(markup.includes(`id="confirm-${id}"`));
        assert.ok(markup.includes(`data-reply-input="${id}"`));
        assert.ok(markup.includes(`data-reply="${id}"`));
      }
      assert.match(markup,/Review &lt;one&gt;/); assert.match(markup,/Continue &lt;script&gt;\?/);
      assert.match(markup,/Checked &amp; ready/); assert.doesNotMatch(markup,/confirm-t3|<script/);
      run("data.tasks=data.tasks.filter(t=>t.id!=='t1');renderActivity();");
      assert.doesNotMatch(elements.get('confirmations').innerHTML,/confirm-t1/);
      assert.match(elements.get('confirmations').innerHTML,/confirm-t2/);
      break;
    }
    case 'c1-router-question': {
      run(`data.router.avatar={state:'waiting'}; data.router.question={id:2,text:'Choose <b>one</b>',options:[]}; data.tasks=[{id:'t7',name:'Linked task',status:'running'}]; messages=new Map([[1,{id:1,role:'router',kind:'question',text:'Choose <b>one</b>',tasks:['t7']}],[2,{id:2,role:'router',kind:'question',text:'Choose <b>one</b>',tasks:[]}]]);renderThread();`);
      const markup=elements.get('thread').innerHTML;
      for(const id of [1,2]) assert.ok(markup.includes(`id="router-question-${id}"`),'Repeated text is not a duplicate question ID');
      assert.equal((markup.match(/confirmation router-question/g)||[]).length,1,'Only the current question remains actionable');
      assert.doesNotMatch(markup,/data-router-reply="1"/);
      assert.match(markup,/data-router-reply="2"/); assert.match(markup,/Linked task/);
      assert.match(markup,/this conversation/); assert.match(markup,/&lt;b&gt;one&lt;\/b&gt;/);
      assert.doesNotMatch(markup,/data-reply-input|data-reply="|<b>one<\/b>/);
      break;
    }
    case 'c1-progress': {
      run(`data.tasks=[{id:'t4',name:'Browser',status:'running',progress:'Checking <button>'},{id:'t5',name:'Wait',status:'waiting',question:'confirm'},{id:'t6',name:'Finished',status:'completed',progress:'done'}];renderActivity();`);
      const markup=elements.get('progressSummary').innerHTML;
      assert.match(markup,/1 Worker running/); assert.match(markup,/data-view="live">View/);
      assert.match(run('workerCard(data.tasks[0])'),/Checking &lt;button&gt;/); assert.doesNotMatch(markup,/data-task="t[456]"/);
      assert.match(elements.get('confirmations').innerHTML,/id="confirm-t5"/);
      run('data.tasks=[];renderActivity();'); assert.equal(elements.get('progressSummary').innerHTML,'');
      break;
    }
    case 'c1-tasks': {
      run(`data.tasks=statuses.map((status,i)=>({id:'t'+i,name:'Task '+i,status,progress:'progress '+i,question:status==='waiting'?'Question & confirm':'',updated:1700000000+i}));data.counts=Object.fromEntries(statuses.map(s=>[s,1]));data.tasks_truncated=true;state.query='';state.tableFilter='active';renderTasks();`);
      assert.equal(run('statuses.length'),9);
      const groups={active:['running','queued','waiting_deps','interrupted'],waiting:['waiting'],ended:['completed','dormant','failed','stopped']};
      for(const [filter,members] of Object.entries(groups)) {
        run(`state.tableFilter='${filter}';renderTasks();`);
        assert.deepEqual(JSON.parse(run('JSON.stringify(filteredTasks().map(t=>t.status).sort())')),[...members].sort());
      }
      assert.equal(Object.values(groups).flat().length,9,'Every real status belongs to exactly one filter');
      for(const query of ['TASK 4','t4','progress 4']) {
        run(`state.tableFilter='active';state.query='${query}';renderTasks();`);
        assert.equal(run('filteredTasks()[0].id'),'t4'); assert.equal(run('filteredTasks().length'),1);
      }
      run("state.tableFilter='waiting';state.query='confirm';renderTasks();"); assert.equal(run('filteredTasks()[0].status'),'waiting');
      const markup=elements.get('taskList').innerHTML;
      assert.match(markup,/data-confirm="t0"/); assert.match(markup,/Question &amp; confirm/);
      assert.match(elements.get('taskLimit').textContent,/1,000/);
      run("state.query='missing';renderTasks();"); assert.match(elements.get('taskList').innerHTML,/No matching Workers/);
      run("state.query='';state.tableFilter='ended';renderTasks();");
      assert.match(elements.get('taskList').innerHTML,/Inspect →/);
      for(const filter of Object.keys(groups)) assert.match(elements.get('taskFilters').innerHTML,new RegExp(`data-table="${filter}"`));
      assert.doesNotMatch(elements.get('taskFilters').innerHTML,/data-table="(queued|waiting_deps|completed)"/);
      assert.match(elements.get('taskFilters').innerHTML,/Running<span>4<\/span>/,'Running counts running, queued, waiting on deps and interrupted');
      break;
    }
    case 'c1-navigation': {
      const scroll=document.getElementById('scroll'); scroll.scrollTop=321;scroll.scrollHeight=1000;scroll.clientHeight=500;
      run("state.task='t8';state.selectedTask='t8';state.step=4;state.tableFilter='waiting';state.query='review';navigate('live');");
      scroll.scrollTop=0;scroll.scrollHeight=0;scroll.clientHeight=0;
      run("messages=new Map([[3,{id:3,role:'router',text:'arrived while hidden'}]]);renderThread();navigate('tasks');navigate('chat');");
      assert.equal(scroll.scrollTop,321); assert.equal(run('state.task'),'t8');assert.equal(run('state.selectedTask'),'t8');
      assert.equal(run('state.step'),4);assert.equal(run('state.tableFilter'),'waiting');assert.equal(run('state.query'),'review');
      break;
    }
    case 'c1-links': {
      const card=document.getElementById('confirm-t2');let scrolled=0,focused=0;
      card.scrollIntoView=()=>scrolled++;card.focus=()=>focused++;
      run("data.tasks=[{id:'t2',name:'Confirm',status:'waiting',question:'Go?'},{id:'t3',name:'Running',status:'running'}];navigate('tasks');showConfirmation('t2');");
      assert.equal(run('state.view'),'chat');assert.equal(scrolled,1);assert.equal(focused,1);assert.equal(run('state.selectedTask'),'t2');
      run("$('communication').open=false;showWorker('t3');");assert.equal(run('state.view'),'live');assert.equal(run('state.selectedTask'),'t3');
      assert.equal(elements.get('communication').open,true,'A direct Worker link must expand a collapsed map');
      const input=document.getElementById('input');input.focus=()=>focused++;
      await listeners.click({target:{closest:()=>({dataset:{routerReply:'27'}})}});
      assert.equal(run('state.view'),'chat');assert.equal(focused,2);
      break;
    }
    case 'c1-reply': {
      const input={value:'Keep this answer',dataset:{replyInput:'t2'}};
      document.querySelectorAll=selector=>selector==='[data-reply-input]'?[input]:[];
      const calls=[];context.fetch=async(path,opts)=>{calls.push([path,JSON.parse(opts.body)]);return {ok:false,status:409,json:async()=>({error:'Task changed'})};};
      run("data.tasks=[{id:'t2',name:'Confirm',status:'waiting'}];");
      await listeners.click({target:{closest:()=>({dataset:{reply:'t2'}})}});
      assert.equal(calls[0][0],'/api/workers/t2/send?by=id');
      assert.deepEqual(calls[0][1],{text:'Keep this answer',expected_status:'waiting'});
      assert.equal(input.value,'Keep this answer');assert.match(elements.get('error').textContent,/Task changed/);
      break;
    }
    case 'c1-live': {
      run(`data.tasks=[{id:'t1',name:'Working <x>',status:'running'},{id:'t2',name:'Waiting',status:'waiting'},{id:'t3',name:'Completed',status:'completed'},{id:'t4',name:'Queued',status:'queued'}];state.selectedTask='t1';renderCommunication();`);
      let lanes=elements.get('routeLanes').innerHTML;
      for(const id of ['t1','t2','t4']) assert.ok(lanes.includes(`data-task="${id}"`) && elements.get('commMap').innerHTML.includes(`data-comm-edge="${id}"`));
      assert.doesNotMatch(lanes,/data-task="t3"/,'Finished Workers leave Routing');
      assert.match(elements.get('itemGraph').innerHTML,/data-task="t3"/);assert.match(elements.get('itemGraph').innerHTML,/Done/);
      assert.match(lanes,/Working &lt;x&gt;/);
      run("data.tasks[0].status='completed';renderCommunication();");
      assert.doesNotMatch(elements.get('routeLanes').innerHTML,/data-task="t1"/);
      assert.match(elements.get('itemGraph').innerHTML,/data-task="t1"/);
      assert.equal(run("buildRunGroups()[0].tasks.find(t=>t.id==='t1').status"),'completed');
      assert.equal(run('state.selectedTask'),'t1','Selection survives a Worker leaving Routing');
      run('data.tasks=[];renderCommunication();');assert.doesNotMatch(elements.get('commMap').innerHTML,/data-comm-edge="t/);
      assert.match(elements.get('commMap').innerHTML,/No active Workers/);
      break;
    }
    case 'c1-draft': {
      const container=document.getElementById('confirmations'),scroll=document.getElementById('scroll');
      const input={id:'',dataset:{replyInput:'t2'},value:'draft & keep',selectionStart:2,selectionEnd:7,focus(){document.activeElement=this;},setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}};
      const getElement=document.getElementById; document.getElementById=id=>id==='t2'?null:getElement(id);
      document.activeElement=input;container.contains=el=>el===input;container.querySelectorAll=selector=>selector==='input,select'?[input]:[];
      document.querySelectorAll=selector=>selector==='[data-reply-input]'?[input]:[];
      scroll.scrollTop=450;
      run("data.tasks=[{id:'t2',status:'waiting',name:'New name',question:'Updated question'}];renderActivity();");
      assert.equal(document.activeElement,input);assert.equal(input.value,'draft & keep');assert.equal(input.selectionStart,2);assert.equal(input.selectionEnd,7);assert.equal(scroll.scrollTop,450);
      break;
    }
    case 'c1-message-scroll': {
      const scroll=document.getElementById('scroll'),thread=document.getElementById('thread');
      scroll.scrollHeight=2400;scroll.clientHeight=500;scroll.scrollTop=1900;
      scroll.getBoundingClientRect=()=>({top:0,bottom:500});
      thread.getBoundingClientRect=()=>({top:-500,bottom:500});
      let aligned=0;thread.scrollIntoView=()=>{aligned++;scroll.scrollTop=400;};
      run("messages.set(1,{id:1,role:'router',text:'New answer above long cards'});renderThread();");
      assert.equal(aligned,1);assert.equal(scroll.scrollTop,400,'Follow the message end, not the end of the appended task cards');
      run("messages.set(2,{id:2,role:'router',text:'Another answer'});renderThread();");
      assert.equal(aligned,2,'Keep following the message end even while task cards remain below the viewport');
      document.activeElement={dataset:{replyInput:'t2'}};
      run("messages.set(3,{id:3,role:'router',text:'Do not move an active draft'});renderThread();");
      assert.equal(aligned,2);
      break;
    }
    case 'c1-latest-question': {
      const requests=[];
      context.fetch=async path=>{requests.push(path);return {ok:true,json:async()=>({items:[{id:12,role:'router',kind:'question',text:'Current question'}],next_after:12,has_more:false})};};
      run("state.historical=true;data.router.avatar={state:'waiting'};data.router.question={id:12,text:'Current question',options:[]};messages.set(1,{id:1,role:'router',kind:'question',text:'Old answered question'});");
      await run('showWaiting()');
      assert.equal(run('state.historical'),false);assert.equal(document.activeElement.id,'router-question-12');
      assert.ok(requests.includes('/api/conversation?limit=50'));
      // An active question can be older than one page of subsequent Worker notices.
      context.fetch=async path=>({ok:true,json:async()=>path.includes('before=200')?
        {items:[{id:150,role:'router',kind:'question',text:'Pending behind notices'}],next_after:150,has_more:false}:
        {items:[{id:200,role:'router',kind:'notice',text:'Worker notice'}],next_after:200,has_more:true}});
      run("data.router.question={id:150,text:'Pending behind notices',options:[]};");
      await run('showWaiting()');assert.equal(document.activeElement.id,'router-question-150');
      let release; context.fetch=()=>new Promise(resolve=>{release=resolve;});
      const loading=run('showWaiting()');run("navigate('tasks');");
      const focused=document.activeElement;
      release({ok:true,json:async()=>({items:[{id:300,role:'router',kind:'question',text:'Late response'}],next_after:300,has_more:false})});
      await loading;assert.equal(run('state.view'),'tasks');assert.equal(document.activeElement,focused,'A late lookup must not steal focus after navigation');
      run("navigate('chat');");
      const loadingAgain=run('showWaiting()'), beforeMessages=run('JSON.stringify([...messages])');
      const scroll=document.getElementById('scroll');scroll.scrollTop=274;
      document.getElementById('thread').scrollIntoView=()=>{scroll.scrollTop=999;};
      run("navigate('tasks');navigate('chat');");
      release({ok:true,json:async()=>({items:[{id:400,role:'router',kind:'question',text:'Stale lookup'}],next_after:400,has_more:false})});
      await loadingAgain;
      assert.equal(scroll.scrollTop,274,'Returning to Chat does not authorize an older lookup to scroll it');
      assert.equal(run('JSON.stringify([...messages])'),beforeMessages,'Guard before mutating conversation state');
      assert.equal(document.activeElement,focused);
      break;
    }
    case 'c1-live-focus': {
      const container=document.getElementById('liveWaiting'),button=document.getElementById('live-confirm-t2');
      let markup='',paints=0;
      Object.defineProperty(container,'innerHTML',{get:()=>markup,set:value=>{paints++;markup=value;document.activeElement=null;}});
      container.contains=el=>el===button;
      run("data.tasks=[{id:'t2',name:'Confirm',status:'waiting'}];renderCommunication();");
      document.activeElement=button;const initial=paints;
      run('renderCommunication();');
      assert.equal(paints,initial);assert.equal(document.activeElement,button);
      run("data.tasks.push({id:'t3',name:'Another question',status:'waiting'});renderCommunication();");
      assert.equal(document.activeElement,button,'Changed waiting lists restore focus by task identity');
      break;
    }
    case 'c1-detail': {
      run("data.profiles=[{name:'work',model:'Model'}];state.task='t1';detail={task:{id:'t1',name:'Task',status:'running',profile:'work',branch:'w/nova',brief:'Brief'},timeline:[],total:0,next_offset:null};renderModal(true);");
      let markup=elements.get('modalBox').innerHTML;
      for(const action of ['interrupt','stop','model']) assert.ok(markup.includes(`data-action="${action}"`));
      assert.match(markup,/data-diff="1"/);assert.match(markup,/id="taskInput"/);
      run("detail.task.status='interrupted';renderModal(true);");assert.match(elements.get('modalBox').innerHTML,/data-action="resume"/);
      assert.match(html,/\.modal \.bar, \.modal \.bar \.tabs \{ flex-wrap:wrap; \}/,'Narrow timeline actions must wrap rather than be clipped by the modal');
      assert.match(html,/\.comm-map svg \{ display:block; max-width:100%; height:auto;/,'The grouped live graph fits its content column');
      assert.ok(run("layoutRunGroup({tasks:[{id:'t1',wait_for:[]}],references:[]},1).width")<=280,'Phone graph layout remains bounded');
      break;
    }
    case 'suber-poses': {
      for(const [state,pose] of Object.entries({idle:'idle',rest:'idle',error:'idle',thinking:'typing',working:'typing',waiting:'waiting',done:'done',unknown:'idle'})) {
        const markup=run(`suberMarkup('${state}',true)`);
        assert.ok(markup.includes(`aria-label="Suber · ${pose}"`));
        assert.ok(markup.includes(`class="suber-static" src="/assets/suber/${pose}.png"`));
        const blinks=[...markup.matchAll(/(?:src|href)="([^"]*-blink.png)"/g)].map(m=>m[1]);
        assert.deepEqual(blinks,pose==='done'?[]:[`/assets/suber/${pose}-blink.png`]);
        assert.doesNotMatch(markup,/data:|https?:|onload|onerror/);
      }
      assert.equal(run("suberMarkup('working')"),'<img src="/assets/suber/mark.png" alt="Suber">');
      break;
    }
    case 'suber-layers': {
      const first=run("suberMarkup('working',true)"), second=run("suberMarkup('thinking',true)");
      const ids=markup=>[...markup.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
      assert.equal(ids(first).length,3); assert.equal(new Set([...ids(first),...ids(second)]).size,6);
      assert.equal((first.match(/typing-clean.png/g)||[]).length,1);
      assert.equal((first.match(/href="\/assets\/suber\/typing.png"/g)||[]).length,2);
      assert.match(first,/<\/defs>\s*<image href="\/assets\/suber\/typing-clean.png"/);
      for(const id of ids(first)) assert.ok(first.includes(`clip-path="url(#${id})"`));
      assert.match(first,/cx="394" cy="503" rx="47" ry="58"/);
      assert.match(first,/cx="575" cy="558" rx="53" ry="52"/);
      assert.doesNotMatch(html,/\.suber-typing\s*\{[^}]*animation:/);
      break;
    }
    case 'suber-private':
    case 'suber-recovery':
    case 'suber-transitions': {
      const nodes=[false,true].map(large=>({dataset:{},innerHTML:'',classList:{toggle(){},remove(){}},
        closest:()=>large?{}:null,replaceChildren(image){ this.image=image; this.innerHTML='private'; }}));
      document.querySelectorAll=selector=>selector==='[data-router-avatar]'?nodes:[];
      run('const reducedMotion={matches:false}; connected=true;');
      if(process.argv[3]==='suber-recovery') {
        run("data.router.avatar={state:'working',asset:null}; renderAvatars();");
        for(const tagName of ['IMG','image']) {
          const markup=nodes[1].innerHTML;
          listeners.error({target:{tagName,getAttribute:()=>'/assets/suber/typing.png',closest:()=>nodes[1]}});
          assert.equal(nodes[1].innerHTML,markup,'Never retry inside an image error callback');
          assert.equal(nodes[1].dataset.avatarKey,undefined);
          run('renderAvatars();');
          assert.ok(nodes[1].dataset.avatarKey,'Retry the same state on the next reconciliation');
          assert.notEqual(nodes[1].innerHTML,markup,'Fresh SVG IDs prove failed layers were reloaded');
        }
        const mark={tagName:'IMG',src:'',getAttribute:()=>'/assets/suber/mark.png',closest:()=>null,hasAttribute:()=>false,
          setAttribute(name,value){this[name]=value;},removeAttribute(name){delete this[name];}};
        listeners.error({target:mark}); assert.equal(mark['data-artwork-retry'],'true');
        assert.equal(mark.src,'');
        document.querySelectorAll=selector=>selector==='img[data-artwork-retry]'?[mark]:[];
        run('renderAvatars();');
        assert.equal(mark.src,'/assets/suber/mark.png'); assert.equal(mark['data-artwork-retry'],undefined);
      } else if(process.argv[3]==='suber-private') {
        run("data.router.avatar={state:'working',name:'Private Router',asset:{url:'/api/router-avatar/working.png?v=1',animated:false}}; renderAvatars();");
        assert.equal(nodes[1].image.src,'/api/router-avatar/working.png?v=1');
        run('avatarPaused=true; renderAvatars();');
        assert.equal(nodes[1].innerHTML,'private','Private stills remain available while paused');
        run("data.router.avatar.asset={url:'/api/router-avatar/working.gif?v=1',animated:true}; renderAvatars();");
        assert.match(nodes[1].innerHTML,/suber-static/); assert.equal(nodes[1].dataset.paused,'true');
        run('avatarPaused=false; reducedMotion.matches=true; renderAvatars();');
        assert.match(nodes[1].innerHTML,/suber-static/);
        run('reducedMotion.matches=false; renderAvatars();');
        assert.equal(nodes[1].image.src,'/api/router-avatar/working.gif?v=1');
        listeners.error({target:{tagName:'IMG',getAttribute:()=>'/api/router-avatar/working.gif?v=1',closest:()=>nodes[1]}});
        assert.match(nodes[1].innerHTML,/suber-typing/); assert.equal(nodes[1].dataset.avatarKey,undefined);
        run('renderAvatars();'); assert.equal(nodes[1].innerHTML,'private');
        listeners.error({target:{tagName:'IMG',getAttribute:()=>'/assets/suber/typing.png',closest:()=>nodes[1]}});
        assert.equal(nodes[1].innerHTML,'private','Built-in load failure must not trigger an infinite replacement loop');
      } else {
        for(const state of ['idle','thinking','working','waiting','done','rest','error','idle']) {
          run(`data.router.avatar={state:'${state}',name:'Router',token:'42'}; renderAvatars();`);
          assert.equal(nodes[1].dataset.avatarState,state);
          assert.equal(elements.get('avatarState').textContent,state);
          const markup=nodes[1].innerHTML;
          run('renderAvatars();'); assert.equal(nodes[1].innerHTML,markup,'Reconciliation must not restart the animation');
          run('avatarPaused=true; renderAvatars();');
          assert.equal(nodes[1].innerHTML,markup); assert.equal(nodes[1].dataset.paused,'true');
          assert.equal(elements.get('avatarMotion').textContent,'Resume motion');
          run('avatarPaused=false; renderAvatars();');
        }
        run("data.router.avatar.state='injected'; renderAvatars();");
        assert.equal(nodes[1].dataset.avatarState,'idle');
      }
      break;
    }
    case 'suber-style': {
      assert.match(html,/\.comm-edge\s*\{[^}]*stroke-width:1;[^}]*stroke-dasharray:3 5;/);
      assert.match(html,/data-theme="dark"/); assert.match(html,/--mascot-stage: #b9bdb9/);
      assert.match(html,/\[data-paused="true"\] \.suber-live \{ display:none; \}/);
      assert.match(html,/@media \(prefers-reduced-motion:reduce\)\s*\{\s*\.router-profile \.suber-live \{ display:none; \}/);
      assert.match(html,/\.router-profile \.avatar \{ width:72px; height:72px;/);
      assert.match(html,/data-avatar-state="error"[^}]*background:var\(--failed\)/);
      for(const label of ['Chat','Workers','Soul & Memory','Models']) assert.ok(html.includes(`aria-label="${label}"`));
      assert.match(html,/@media \(max-width:760px\)\s*\{\s*\.page \.card > table \{ min-width:720px;/);
      assert.match(html,/@media \(max-width:760px\)[^\n]*\.router-profile \{ display:none; \}/);
      assert.equal((html.match(/data-router-avatar>/g)||[]).length,1,'No hidden duplicate GIF can keep playing outside the corner character');
      assert.match(html,/\.chat-side \{ display:flex;/);
      assert.match(html,/id="avatarMotion"[^>]*aria-label="Pause motion"/);
      assert.ok(html.includes("$('avatarMotion').setAttribute('aria-label',$('avatarMotion').textContent)"),'Generated icon content must not change the accessible button name');
      assert.match(html,/nav-label">Workspace/); assert.doesNotMatch(html,/linearGradient id="logo|linear-gradient\(145deg/);
      assert.doesNotMatch(html,/<(?:script|link)[^>]*(?:src|href)="https?:/);
      break;
    }
    case 'fallback': {
      const rows=[{id:1,role:'router',text:'saved',created:1,channel:'web'}];
      let reads=0;
      context.fetch=async path=>({ok:true,json:async()=>path==='/api/state'?
        {tasks:[],counts:{},profiles:[],user:{name:'You'},router:{busy:true},time:Date.now()/1000}:
        (reads++,{items:rows,next_after:1,has_more:false})});
      await run('sync()');
      assert.ok([...timers.values()].some(t=>t.delay===2000),'busy Router must schedule a 2-second fallback even without SSE');
      let paints=0; const thread=elements.get('thread');
      const markup=thread.innerHTML;
      Object.defineProperty(thread,'innerHTML',{get:()=>markup,set:()=>{paints++;}});
      const focus={selectionStart:2,selectionEnd:4}; document.activeElement=focus;
      elements.get('scroll').scrollTop=123;
      await run('sync()');
      assert.equal(paints,0,'unchanged SSE/poll results must not repaint the conversation');
      assert.equal(document.activeElement,focus); assert.equal(focus.selectionStart,2); assert.equal(focus.selectionEnd,4);
      assert.equal(elements.get('scroll').scrollTop,123); assert.ok(reads>=4);
      const retained=run("threadNodes.get('message:1').node");
      rows.push({id:2,role:'router',text:'new durable reply'});
      await run('sync()');
      assert.equal(run("threadNodes.get('message:1').node"),retained,'new replies must retain existing selected/focused message nodes');
      assert.equal(elements.get('scroll').scrollTop,123);
      context.fetch=async path=>({ok:true,json:async()=>path==='/api/state'?
        {tasks:[],counts:{},profiles:[],user:{name:'You'},router:{busy:false,queued:0},time:Date.now()/1000}:
        {items:rows,next_after:1,has_more:false}});
      await run('sync()');
      assert.ok([...timers.values()].some(t=>t.delay===15000),'idle fallback is 15 seconds');
      run("pending.set('waiting',{text:'not acknowledged',sending:true});"); await run('sync()');
      assert.ok([...timers.values()].some(t=>t.delay===2000),'pending sends use the fast fallback');
      run("pending.get('waiting').sending=false; pending.get('waiting').failed='offline';"); await run('sync()');
      assert.ok([...timers.values()].some(t=>t.delay===15000),'failed drafts waiting for manual Retry are idle');
      run("pending.clear(); messages.set(1,{...messages.get(1),role:'user',status:'queued'});");
      await run('sync()');
      assert.ok([...timers.values()].some(t=>t.delay===15000),'stale cached display statuses must not keep the idle timer fast');
      run('data.router.pending=1;');
      rows[0].role='user'; rows[0].status='queued';
      // A server-side queued message also requires fast reconciliation.
      context.fetch=async path=>({ok:true,json:async()=>path==='/api/state'?run('data'):{items:rows,next_after:1,has_more:false}});
      await run('sync()');
      assert.ok([...timers.values()].some(t=>t.delay===2000));
      break;
    }
    case 'fallback-timeout': {
      context.fetch=async(path,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Error('test timeout'))));
      const syncing=run('sync()');
      const deadline=[...timers.values()].find(t=>t.delay===8000);
      assert.ok(deadline,'reconciliation fetch must have a bounded abort deadline');
      deadline.fn(); await syncing;
      assert.equal(run('syncRunning'),false,'a hung fetch must release the single-flight gate');
      assert.ok([...timers.values()].some(t=>t.delay===15000));
      break;
    }
    case 'fallback-metadata': {
      run("data.tasks=[{id:'t1',name:'fake',status:'running'}]; messages.set(1,{id:1,role:'router',text:'selectable reply',tasks:['t1']}); renderThread();");
      const node=run("threadNodes.get('message:1').node");
      assert.match(node.innerHTML,/data-robot="running"/);
      run("data.tasks[0].status='completed'; renderThread();");
      assert.equal(run("threadNodes.get('message:1').node"),node,'metadata updates must patch rather than replace the message');
      assert.match(node.innerHTML,/data-robot="rest"/);
      assert.doesNotMatch(node.innerHTML,/data-robot="running"/);
      assert.match(node.innerHTML,/selectable reply/);
      break;
    }
    case 'fallback-empty': {
      context.fetch=async path=>({ok:true,json:async()=>path==='/api/state'?
        {tasks:[],counts:{},profiles:[],user:{name:'You'},router:{},time:Date.now()/1000}:
        {items:[],next_after:0,has_more:false}});
      await run('sync()');
      let paints=0; const thread=elements.get('thread'), markup=thread.innerHTML;
      Object.defineProperty(thread,'innerHTML',{get:()=>markup,set:()=>{paints++;}});
      await run('sync()');
      assert.equal(paints,0,'an empty conversation is already loaded, not a reason to force repaint');
      break;
    }
    case 'markdown': {
      const text='# Heading\n\n**bold** and *italic* and `code`\n\n```js\nconst x = "<safe>";\n```\n\n- one\n- two\n\n1. first\n2. second\n\n> quote\n\n| Name | Value |\n| --- | --- |\n| a | b |\n\n[site](https://example.invalid/a?x=1&y=2)';
      context.fixtureText=text;
      run("messages.set(1,{id:1,role:'router',text:fixtureText}); renderThread(true);");
      const rendered=elements.get('thread').innerHTML;
      for(const tag of ['h1','strong','em','code','pre','ul','ol','blockquote','table','a'])
        assert.match(rendered,new RegExp('<'+tag+'(?:>| )'),'Markdown needs '+tag);
      assert.match(rendered,/href="https:\/\/example.invalid\/a\?x=1&amp;y=2"/);
      run("messages.clear(); messages.set(2,{id:2,role:'user',text:fixtureText}); renderThread(true);");
      assert.doesNotMatch(elements.get('thread').innerHTML,/<h1>|<strong>|<pre>|<a href=/,'user messages remain plain text');
      assert.match(elements.get('thread').innerHTML,/\*\*bold\*\*/);
      break;
    }
    case 'chip-identity': {
      const old=new Element('chips'), next=new Element('chips'), focused=new Element('button'); focused.dataset.task='t2'; old.appendChild(focused);
      for(const id of ['t10','t2','t3']) { const chip=new Element('button'); chip.dataset.task=id; next.appendChild(chip); }
      const appended=new Element('new-detail'); appended._html='new tool details'; next.appendChild(appended);
      document.activeElement=focused; context.oldChips=old; context.nextChips=next;
      run('patchThreadNode(oldChips,nextChips)');
      assert.equal(old.children[1],focused,'inserting an earlier sorted chip must keep the focused task identity');
      assert.equal(focused.dataset.task,'t2'); assert.equal(document.activeElement,focused);
      assert.equal(old.children[0].dataset.task,'t10');
      assert.equal(old.children.length,4,'new keyed and unkeyed trailing children must enter the live DOM');
      assert.equal(old.children[2].dataset.task,'t3');
      assert.equal(old.children[3],appended);
      break;
    }
    case 'markdown-bounds': {
      context.unclosed='['.repeat(65536)+'\n\n'+('[x](https://example.invalid/'.repeat(3000));
      const start=performance.now();
      const rendered=run('markdown(unclosed)');
      assert.match(rendered,/<p>/);
      assert.ok(performance.now()-start<1500,'unclosed link markers must not cause quadratic inline scanning');
      assert.ok(rendered.includes('['.repeat(65536)));
      break;
    }
    case 'markdown-word-boundaries': {
      const literals=['super_super_harness/web_regressions.js','SSHARNESS_NO_CAFFEINATE',
        'https://example.invalid/super_super_harness/file_name','a_b_c','a__b__c',
        '用户_登录_状态','用户__登录__状态','naïve_é_value'];
      for(const value of literals) {
        context.markdownInput=value;
        assert.equal(run('markdown(markdownInput)'),'<p>'+value+'</p>','word-internal underscores must stay copyable');
      }
      assert.equal(run("markdown('_italic_ and __bold__, (*star* **strong**)')"),
        '<p><em>italic</em> and <strong>bold</strong>, (<em>star</em> <strong>strong</strong>)</p>');
      assert.equal(run("markdown('a_word_ _word_b a__word__ __word__b')"),'<p>a_word_ _word_b a__word__ __word__b</p>');
      break;
    }
    case 'markdown-ordered-continuations': {
      assert.equal(run("markdown('1. A\\n   explanation\\n2. B\\n\\n3. C')"),
        '<ol start="1"><li>A<br>explanation</li><li>B</li><li>C</li></ol>');
      assert.equal(run("markdown('5. Five\\n\\n6. Six')"),'<ol start="5"><li>Five</li><li>Six</li></ol>');
      assert.equal(run("markdown('5. Five\\n   continuation\\n\\n   more detail\\n6. Six\\n\\nOutside')"),
        '<ol start="5"><li>Five<br>continuation<br><br>more detail</li><li>Six</li></ol><p>Outside</p>');
      assert.equal(run("markdown('1. One\\n 2. Two\\n3. Three')"),
        '<ol start="1"><li>One</li><li>Two</li><li>Three</li></ol>');
      assert.equal(run("markdown('1. A\\n   ```bash\\n  **literal**\\n   ```\\nAfter')"),
        '<ol start="1"><li>A</li></ol><pre><code>  **literal**</code></pre><p>After</p>');
      assert.equal(run("markdown('1. Item\\n\\n  Outside')"),
        '<ol start="1"><li>Item</li></ol><p>  Outside</p>');
      const injected=run(`markdown('5. safe\\n\\n5" onmouseover="bad. unsafe')`);
      assert.match(injected,/<ol start="5">/);
      assert.doesNotMatch(injected,/<ol[^>]*onmouseover|start="[^"]*[^0-9"]/);
      break;
    }
    case 'markdown-space-heading-65536':
    case 'markdown-space-heading-262144':
    case 'markdown-space-delimiter-65536':
    case 'markdown-space-delimiter-262144': {
      const parts=process.argv[3].split('-'), kind=parts[2], size=Number(parts[3]);
      const spaces=' '.repeat(size);
      context.markdownInput=kind==='heading'?'# before'+spaces+'after':'| heading |\n'+spaces+'not-a-delimiter\nvisible tail';
      run("markdown('# warm up')");
      const start=performance.now(), rendered=run('markdown(markdownInput)'), elapsed=performance.now()-start;
      assert.ok(elapsed<1500,'long whitespace must render within a generous 1.5-second budget; elapsed='+elapsed);
      assert.ok(rendered.includes(spaces),'whitespace performance must not come from truncating input');
      assert.ok(rendered.includes(kind==='heading'?'after':'visible tail'),'content after whitespace must survive');
      if(kind==='heading') assert.match(rendered,/<h1>before/);
      console.log(JSON.stringify({case:process.argv[3],bytes:size,elapsedMs:elapsed}));
      break;
    }
    case 'markdown-unicode-bounds': {
      const samples=[];
      for(const size of [65536,262144]) {
        const spaces=' '.repeat(size);
        const separator=String.fromCharCode(0x2028), paragraph=String.fromCharCode(0x2029);
        samples.push('-'+spaces+'x'+separator+'!', '1.'+spaces+'x'+paragraph+'!',
          '- first\n-'+spaces+'x'+separator+'!', '1. first\n2.'+spaces+'x'+paragraph+'!',
          '#'+spaces+'x'+separator+'!', '| h |\n'+spaces+'---'+spaces+'!');
      }
      for(const input of samples) {
        context.markdownInput=input;
        const start=performance.now(), rendered=run('markdown(markdownInput)'), elapsed=performance.now()-start;
        assert.ok(elapsed<1500,'line-separator and delimiter failure paths must stay bounded: '+elapsed);
        assert.ok(rendered.includes(' '.repeat(65536)),'failure handling must preserve the full input');
        assert.ok(rendered.includes('!'));
      }
      break;
    }
    case 'markdown-table-cells': {
      const rendered=run("markdown('| A | B |\\n| --- | --- |\\n| `x|y` | z |\\n| escaped\\\\|pipe | right |\\n| extra | cells | never lost |')");
      assert.match(rendered,/<td><code>x\|y<\/code><\/td><td>z<\/td>/);
      assert.match(rendered,/<td>escaped\|pipe<\/td><td>right<\/td>/);
      assert.match(rendered,/<td>extra<\/td><td>cells<\/td><td>never lost<\/td>/);
      assert.match(rendered,/<thead><tr><th>A<\/th><th>B<\/th><th><\/th><\/tr>/);
      break;
    }
    case 'markdown-wide-row':
    case 'markdown-wide-header': {
      for(const size of [4096,32768]) {
        const width=size>>1, rows=Math.floor((size-width-6)/2), headerWide=process.argv[3]==='markdown-wide-header';
        const wide='|'.repeat(width-1)+(headerWide?'header-tail':'wide-tail')+'|';
        context.markdownInput=(headerWide?wide:'| h |')+'\n---\n'+(headerWide?'':wide+'\n')+'| short |\n'.repeat(rows);
        const start=performance.now(), rendered=run('markdown(markdownInput)'), elapsed=performance.now()-start;
        assert.ok(elapsed<1500,'wide tables must stay within a generous 1.5-second budget: '+elapsed);
        const td=(rendered.match(/<td>/g)||[]).length, th=(rendered.match(/<th>/g)||[]).length;
        if(size===32768) {
          const grid=(width-1)*(rows+(headerWide?1:2));
          assert.ok(grid>4194304,'this tier must exceed the layout-grid limit');
          assert.equal(td,0,'oversized tables must not create body cells');
          assert.equal(th,0,'oversized tables must not create header cells');
          assert.equal(rendered,'<pre><code>'+run('esc(markdownInput)')+'</code></pre>','fallback must preserve the complete escaped original');
          assert.ok(rendered.includes(headerWide?'header-tail':'wide-tail'),'the wide row tail must survive');
          assert.ok(rendered.length<context.markdownInput.length*64,'table output must not grow as width times row count');
          console.log(JSON.stringify({case:process.argv[3],size,inputLength:context.markdownInput.length,grid,td,th,outputLength:rendered.length,elapsedMs:elapsed}));
          continue;
        }
        assert.equal(td,rows+(headerWide?0:width-1),'body rows must not be padded to maximum width');
        assert.equal(th,width-1,'header must still span the maximum width');
        assert.equal((rendered.match(/<td>short<\/td>/g)||[]).length,rows,'all short rows must retain their content');
        assert.ok(rendered.includes(headerWide?'header-tail':'wide-tail'),'the wide row tail must survive');
        assert.ok(rendered.length<context.markdownInput.length*64,'table output must not grow as width times row count');
        console.log(JSON.stringify({case:process.argv[3],size,inputLength:context.markdownInput.length,td,th,outputLength:rendered.length,elapsedMs:elapsed}));
      }
      break;
    }
    case 'markdown-table-grid-limit': {
      for(const height of [2047,2048,2049]) {
        const width=2048, shortRows=height-2;
        const table='| head<&> |\n|---|\n'+'|a'.repeat(width-1)+'|tail<&>|\n'+'| short<&> |\n'.repeat(shortRows);
        context.markdownTable=table; context.markdownInput=table+'\nAfter table';
        const rendered=run('markdown(markdownInput)'), grid=width*height;
        if(grid>4194304) {
          assert.ok(rendered.startsWith('<pre><code>'),'above-limit grid must use the code fallback');
          assert.equal(rendered,'<pre><code>'+run('esc(markdownTable)')+'\n</code></pre><p>After table</p>');
          assert.doesNotMatch(rendered,/<table>|<td>|<th>|&amp;lt;/);
        } else {
          assert.ok(rendered.startsWith('<table>'),'below and exactly at the limit must retain table behavior');
          assert.equal((rendered.match(/<th>/g)||[]).length,width);
          assert.equal((rendered.match(/<td>/g)||[]).length,width+shortRows);
          assert.ok(rendered.includes('tail&lt;&amp;&gt;'));
        }
      }
      break;
    }
    case 'markdown-ordered-two-space-restart':
    case 'markdown-ordered-three-space-restart':
    case 'markdown-ordered-tab-restart':
    case 'markdown-ordered-two-digit-restart': {
      const variants={
        'markdown-ordered-two-space-restart':[1,'  '],
        'markdown-ordered-three-space-restart':[10,'   '],
        'markdown-ordered-tab-restart':[1,'\t'],
        'markdown-ordered-two-digit-restart':[99,'  '],
      };
      const [start,indent]=variants[process.argv[3]];
      context.markdownInput=[start+'. A',indent+'1. sub-one',indent+'2. sub-two',(start+1)+'. B'].join('\n');
      const rendered=run('markdown(markdownInput)'), shown=[];
      for(const list of rendered.matchAll(/<ol start="(\d+)">([\s\S]*?)<\/ol>/g)) {
        let ordinal=Number(list[1]);
        for(const item of list[2].matchAll(/<li>([\s\S]*?)<\/li>/g)) shown.push([item[1],ordinal++]);
      }
      assert.deepEqual(shown,[['A',start],['sub-one',1],['sub-two',2],['B',start+1]],'displayed ordinals must match the source');
      assert.equal(rendered,'<ol start="'+start+'"><li>A</li></ol><ol start="1"><li>sub-one</li><li>sub-two</li></ol><ol start="'+(start+1)+'"><li>B</li></ol>');
      break;
    }
    case 'markdown-fence-inline-prefix': {
      for(const input of ['```inline``` text\nnext line','```a``` and ```b```\nnext','```code```','``` ```\nnext']) {
        context.markdownInput=input;
        const rendered=run('markdown(markdownInput)');
        assert.doesNotMatch(rendered,/<pre>/,'backticks in a backtick-fence info string must reject the opener');
        assert.match(rendered,/<p>/);
        assert.ok(rendered.includes(input.includes('inline')?'inline':input.includes('code')?'code':input.includes('and')?'and':'`'));
        if(input.includes('\n')) assert.ok(rendered.includes(input.slice(input.indexOf('\n')+1)),'the following line must remain visible');
      }
      assert.equal(run("markdown('before\\n```inline``` text\\nnext line')"),
        '<p>before<br>``<code>inline</code>`` text<br>next line</p>');
      assert.equal(run("markdown('1. A\\n   ```code``` after\\n   next\\n2. B')"),
        '<ol start="1"><li>A<br>``<code>code</code>`` after<br>next</li><li>B</li></ol>');
      assert.equal(run("markdown('~~~info`unchanged\\nbody\\n~~~')"),'<pre><code>body</code></pre>');
      break;
    }
    case 'markdown-ordered-sublist-boundary': {
      assert.equal(run("markdown('1. A\\n   1. sub\\n   2. sub\\n2. B')"),
        '<ol start="1"><li>A</li></ol><ol start="1"><li>sub</li><li>sub</li></ol><ol start="2"><li>B</li></ol>');
      assert.equal(run("markdown('1. A\\n\\n   1. sub\\n   2. sub\\n\\n2. B')"),
        '<ol start="1"><li>A</li></ol><ol start="1"><li>sub</li><li>sub</li></ol><ol start="2"><li>B</li></ol>');
      assert.equal(run("markdown('1. A\\n   - sub\\n   - sub2\\n2. B')"),
        '<ol start="1"><li>A</li></ol><ul><li>sub</li><li>sub2</li></ul><ol start="2"><li>B</li></ol>');
      break;
    }
    case 'markdown-fence-info': {
      for(const info of ['bash title="x"','foo.py','arbitrary <fake> "attribute"']) {
        context.markdownInput='```'+info+'\ninside <fake>\n```\nafter fence';
        const rendered=run('markdown(markdownInput)');
        assert.equal(rendered,'<pre><code>inside &lt;fake&gt;</code></pre><p>after fence</p>');
      }
      assert.equal(run("markdown('~~~anything here\\ninside\\n~~~ not closing\\nmore\\n~~~\\nafter')"),
        '<pre><code>inside\n~~~ not closing\nmore</code></pre><p>after</p>');
      break;
    }
    case 'markdown-xss': {
      context.fixtureText='<script>globalThis.pwned=1</script>\n\n[bad](javascript:alert(1)) [data](data:text/html,bad) [relative](/api/state)\n\n[attribute](https://example.invalid/" onmouseover="bad)\n\n<img src=x onerror="bad">\n\n`<svg onload=bad>`\n\n[ok](http://example.invalid/)';
      run("messages.set(1,{id:1,role:'router',text:fixtureText}); renderThread(true);");
      const rendered=elements.get('thread').innerHTML;
      assert.match(rendered,/<a href="http:\/\/example.invalid\/"/,'safe links must be rendered');
      assert.doesNotMatch(rendered,/<script|<img|<svg onload|href="(?:javascript:|data:|\/)/i);
      assert.doesNotMatch(rendered,/href="[^"]*"\s+onmouseover=/i);
      assert.match(rendered,/&lt;script&gt;/);
      break;
    }
    case 'tool-calls': {
      run("for(const [id,role] of [[1,'user'],[2,'router']]) messages.set(id,{id,role,text:'plain',steps:[{name:'Bash',text:'<fake>'},{name:'Read',text:'file'}]}); renderThread(true);");
      const rendered=elements.get('thread').innerHTML;
      assert.equal((rendered.match(/data-steps=/g)||[]).length,1,'tools belong only to Router replies');
      assert.match(rendered,/data-steps="2">2 actions/);
      assert.doesNotMatch(rendered,/class="steps"/,'tool calls are collapsed by default');
      await listeners.click({target:{closest:()=>({dataset:{steps:'2'}})}});
      assert.match(elements.get('thread').innerHTML,/class="steps"/);
      assert.match(elements.get('thread').innerHTML,/Running a command/);assert.doesNotMatch(elements.get('thread').innerHTML,/<fake>/);
      break;
    }
    case 'auth-recovery': {
      run('source=new EventSource("/api/stream"); connected=true;');
      context.fetch=async()=>({ok:false,status:401,json:async()=>({error:'secret-ticket-do-not-render'})});
      await run('sync()');
      assert.equal(streams[0].readyState,2);
      const message=elements.get('error').textContent;
      assert.match(message,/supersuperharness web --open/,'401 must explain the login command');
      assert.doesNotMatch(message,/secret-ticket|token=|https?:/);
      assert.ok([...timers.values()].some(t=>t.delay<=15000),'401 recovery must continue probing');
      context.fetch=async path=>({ok:true,json:async()=>path==='/api/state'?
        {tasks:[],counts:{},profiles:[],user:{name:'You'},router:{},time:Date.now()/1000}:
        {items:[{id:1,role:'router',text:'returned'}],next_after:1,has_more:false}});
      await run('sync()');
      assert.equal(streams.length,2,'successful cookie probe must recreate the closed stream');
      assert.equal(streams[1].url,'/api/stream');
      streams[1].onopen(); await run('sync()');
      assert.equal(run('connected'),true);
      assert.equal(elements.get('error').hidden,true);
      assert.match(elements.get('thread').innerHTML,/returned/);
      break;
    }
    case 'files-draft': {
      context.fetch = async()=>({ok:true,json:async()=>({content:'</textarea><script>fake</script>',version:'v1',exists:true})});
      await run("loadRouterFile('soul')");
      assert.match(elements.get('view-files').innerHTML,/&lt;\/textarea&gt;/);
      assert.doesNotMatch(elements.get('view-files').innerHTML,/<script>fake/);
      run("routerFiles.soul.draft='unsaved'; renderRouterFile()");
      await run("loadRouterFile('memory')");
      await run("loadRouterFile('soul')");
      assert.match(elements.get('view-files').innerHTML,/unsaved/);
      context.confirm=()=>false;
      await run("loadRouterFile('soul',true)");
      assert.equal(run('routerFiles.soul.draft'),'unsaved');
      break;
    }
    case 'files-conflict': {
      context.fetch = async()=>({ok:true,json:async()=>({content:'initial',version:'v1',exists:true})});
      await run("loadRouterFile('memory')");
      run("routerFiles.memory.draft='local edit'");
      context.fetch = async()=>({ok:false,status:409,json:async()=>({error:'conflict'})});
      await run('saveRouterFile()');
      assert.equal(run('routerFiles.memory.draft'),'local edit');
      assert.equal(run('routerFiles.memory.version'),'v1');
      assert.match(elements.get('fileStatus').textContent,/conflict.*Reload/i);
      let posted;
      context.fetch = async(path,opts)=>{posted=JSON.parse(opts.body);return {ok:true,json:async()=>({content:posted.content,version:'v2',exists:true})};};
      await run('saveRouterFile()');
      assert.equal(posted.version,'v1');
      assert.equal(run('routerFiles.memory.version'),'v2');
      assert.equal(run('routerFiles.memory.content'),'local edit');
      break;
    }
    case 'scroll':
      run('renderReminders()'); tasksPage.scrollTop=850;
      run('data.reminders.items[0].next_time="Later"; renderReminders()');
      assert.equal(tasksPage.scrollTop,850,'Tasks scroll must survive a repaint containing reminders');
      break;
    case 'cancel': {
      let confirmation=''; context.confirm = text => { confirmation=text; return false; };
      await listeners.click({target:{closest:()=>({dataset:{cancelReminder:'r7'}})}});
      assert.match(confirmation,/Review 6/,'confirmation must name the selected reminder');
      break;
    }
    case 'run':
      run(`messages.set(1,{id:1,role:'user',kind:'reminder.run',text:'scheduled',created:1,channel:'terminal'}); renderThread(true);`);
      assert.match(elements.get('thread').innerHTML,/Scheduled run/);
      assert.match(elements.get('thread').innerHTML,/to Router/);
      assert.doesNotMatch(elements.get('thread').innerHTML,/to You/);
      break;
    case 'usage':
      run(`usage={workers:{running:2,queued:3,limit:4},start:'2026-01-01',end:'2026-01-03',timezone:'UTC',truncated:true,
        summary:{tokens:null,known_tokens:20,unknown_sessions:1},
        days:[{day:'2026-01-01',tokens:20,known_tokens:20,unknown_sessions:0}],
        models:[{model:'<fake-model>',tokens:20,known_tokens:20,unknown_sessions:0}],
        tasks:[{task_id:'t1',code_name:'Nova <tag>',tokens:null,known_tokens:null,unknown_sessions:1}],
        quota:[{provider:'<fixture>',status:'unconfigured'}]}; renderUsage();`);
      assert.match(elements.get('view-usage').innerHTML,/未知/);
      assert.match(elements.get('view-usage').innerHTML,/未配置/);
      assert.match(elements.get('view-usage').innerHTML,/&lt;fake-model&gt;/);
      assert.match(elements.get('view-usage').innerHTML,/&lt;fixture&gt;/);
      assert.match(elements.get('view-usage').innerHTML,/UTC/);
      assert.match(elements.get('view-usage').innerHTML,/已知小计/);
      assert.match(elements.get('view-usage').innerHTML,/上限/);
      assert.match(elements.get('view-usage').innerHTML,/截断/);
      assert.match(elements.get('view-usage').innerHTML,/按天/);
      assert.match(elements.get('view-usage').innerHTML,/按模型/);
      assert.match(elements.get('view-usage').innerHTML,/按 Worker/);
      assert.match(elements.get('view-usage').innerHTML,/Nova &lt;tag&gt;/);
      assert.doesNotMatch(elements.get('view-usage').innerHTML,/>t\d+</);
      run(`usage.summary={tokens:0,known_tokens:0,unknown_sessions:0}; usage.tasks=[]; usage.models=[]; renderUsage();`);
      assert.match(elements.get('view-usage').innerHTML,/>0</);
      assert.match(elements.get('view-usage').innerHTML,/暂无记录/);
      break;
    case 'c5-files': {
      context.fetch = async()=>({ok:true,json:async()=>({content:'# Soul',version:'v1',exists:true})});
      await run("loadRouterFile('soul')");
      const page=elements.get('view-files').innerHTML;
      assert.match(page,/Soul: who the Router is and how it works\. Memory: what it remembers about you\./);
      for(const control of ['data-router-file="soul"','data-router-file="memory"','data-file-reload="1"','data-file-save="1"','id="fileContent"']) assert.ok(page.includes(control),control);
      assert.doesNotMatch(page,/建设中|memory_read|memory_write|bypass conflict|router\/soul\.md|16 KiB|Loaded current file|conflict/i);
      assert.match(page,/id="fileStatus" role="status" aria-live="polite"><\/p>/,'A loaded file shows no status text');
      context.fetch = async()=>({ok:true,json:async()=>({content:'',version:'v0',exists:false})});
      await run("loadRouterFile('memory')");
      assert.match(elements.get('view-files').innerHTML,/placeholder="No file yet\. Write here and Save to create it\."/);
      break;
    }
    case 'c5-models': {
      run(`models={router:{primary:{profile:'main',model:'<b>main</b>'},backup:{profile:'spare',model:'spare-model'},current:{profile:'spare',model:'spare-model'},active:'backup',mode:'running',switching:false},
        profiles:[{name:'<img src=x onerror=bad>',harness:'claude-code',model:'work-model',provider:'two',default:true,healthy:false},{name:'main',harness:'codex',model:'main-model',provider:'one',default:false,healthy:true}],
        version:'abc1234',home:'/Users/private/home',base_url:'http://127.0.0.1:9',key:'sk-fixture',swap:'total = 1M',outbox:[{channel:'web',count:1,status:'sent'}]}; renderModels();`);
      const page=elements.get('view-models').innerHTML;
      assert.match(page,/<h1 class="page-title">Models<\/h1>/);
      for(const text of ['Primary','Backup','In use','Status','Running','Worker Profiles','Default Worker','Provider status','Reachable','Unreachable','codex','Version abc1234']) assert.ok(page.includes(text),text);
      assert.match(page,/&lt;b&gt;main&lt;\/b&gt;/); assert.match(page,/&lt;img src=x onerror=bad&gt;/);
      assert.doesNotMatch(page,/<img|<b>main/);
      assert.doesNotMatch(page,/\/Users\/private|127\.0\.0\.1|sk-fixture|total = 1M|Outbox|swap|Inbox|<input|<select|<textarea|data-action/i);
      run(`models.router.switching=true; renderModels();`);
      assert.match(elements.get('view-models').innerHTML,/Switching/);
      break;
    }
    case 'c5-navigation': {
      const models={router:{primary:{profile:'main',model:'m'},backup:{profile:'main',model:'m'},current:{profile:'main',model:'m'},active:'primary',mode:'running',switching:false},profiles:[],version:'v'};
      context.fetch=async path=>({ok:true,json:async()=>path==='/api/models'?models:{content:'',version:'v1',exists:true}});
      assert.doesNotMatch(html,/data-view="usage"/,'Usage is not reachable from the page');
      assert.match(html,/id="view-models"/);
      assert.doesNotMatch(html,/id="view-developer"/);
      run("navigate('models')");
      assert.equal(run('state.view'),'models');
      assert.equal(elements.get('view-models').classList.contains('on'),true);
      run("navigate('developer')");
      assert.equal(run('state.view'),'models','The old navigation still reaches Models during integration');
      run("navigate('usage')");
      assert.equal(run('state.view'),'chat','Usage routes back to Chat');
      assert.equal(elements.get('view-usage').classList.contains('on'),false);
      assert.equal(elements.get('view-chat').classList.contains('on'),true);
      break;
    }
    case 'worker':
      run(`messages.set(1,{id:1,role:'router',kind:'worker.done',title:'✓ 完成 · t7 Neutral <task>',text:'Done',created:1,channel:'terminal'}); renderThread(true);`);
      assert.match(elements.get('thread').innerHTML,/✓ 完成 · t7 Neutral &lt;task&gt;/);
      break;
    default: throw Error('unknown test');
  }
  console.log('PASS '+process.argv[3]);
})().catch(error=>{ console.error(error); process.exitCode=1; });
