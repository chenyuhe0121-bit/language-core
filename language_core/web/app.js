'use strict';
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let importedScenario='',requestId=null;
let characters=[],scenes=[],sessions=[],state=null,current=null,busy=false,controller=null,voice=false,feedbackTurn=null,lastDebug=null;
const moods={neutral:'平静',happy:'开心',concern:'关切',sad:'难过',playful:'俏皮',shy:'害羞',annoyed:'不悦',surprised:'惊讶'};
const paces={slow:'慢',normal:'自然',fast:'快',very_slow:'很慢',very_fast:'很快'};
const intents={comfort:'安慰',tease:'打趣',probe:'追问',share:'分享',invite:'邀请',deflect:'岔开',refuse:'拒绝',agree:'应下',reminisce:'忆旧',idle:'随口'};
const expressions={smile:'微笑',grin:'笑开',wry_smile:'苦笑',surprised:'惊讶',slight_frown:'微皱眉',frown:'皱眉',look_down:'低头',look_away:'移开视线',glance_up:'抬眼',tilt_head:'偏头',lean_forward:'前倾',lean_back:'后靠',blink_slow:'慢眨眼',eyes_wide:'睁大眼',nod:'点头',shake_head:'摇头',hand_to_face:'手扶脸',fidget:'小动作',wipe_cup:'擦杯子',wipe_counter:'擦吧台',steam_wand:'打奶泡',pour:'倒咖啡',hold_cup_both_hands:'双手捧杯',pull_blanket:'拉毯子',put_needle_down:'放唱针',point_far:'指远处',hug_arm:'拉手臂',tuck_hair:'拢头发',jump:'跳一下',cover_eyes:'捂眼',hold_balloon:'拿气球',lie_side:'侧躺',close_eyes:'闭眼',reach_hand:'伸手',turn_off_light:'关灯'};
async function api(path,body){const res=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await res.json();if(!res.ok)throw Error(data.error||'请求失败');return data;}
function qs(){return '?session_id='+encodeURIComponent(current);}
function notice(text,error=false){$('notice').textContent=text;$('notice').classList.toggle('error',error);}
function scroll(){const box=$('messages');box.scrollTop=box.scrollHeight;}
function setBusy(value){busy=value;for(const id of ['send','scene','relationship','new-chat','add-character','memory-enabled'])$(id).disabled=value;$('stop').classList.toggle('hidden',!value);$('generation-status').textContent=value?'正在组织回应…':'台词与情绪同步生成';renderCharacters();renderSessions();}
function avatar(node,card){node.textContent=card.name.slice(0,1);node.style.setProperty('--avatar',/^#[0-9a-f]{6}$/i.test(card.color)?card.color:'#547867');}
function renderCharacters(){const box=$('characters');box.replaceChildren();characters.forEach(card=>{const b=document.createElement('button');b.className='character-card'+(state?.character.id===card.id?' active':'');b.disabled=busy;b.innerHTML=`<span class="avatar"></span><span><b>${esc(card.name)}</b><small>${esc(card.tagline)}</small></span>`;avatar(b.firstElementChild,card);b.onclick=()=>guard(async()=>{const prior=sessions.find(s=>s.character_id===card.id);if(prior)await openSession(prior.id);else await newChat(card.id);});box.append(b);});}
function renderSessions(){const box=$('sessions');box.replaceChildren();sessions.forEach(s=>{const card=characters.find(c=>c.id===s.character_id);const b=document.createElement('button');b.className='session'+(s.id===current?' active':'');b.disabled=busy;b.innerHTML=`${esc(s.title)}<small>${esc(card?.name||s.character_id)} · ${esc(s.created_at.slice(5,10))}</small>`;b.onclick=()=>guard(()=>openSession(s.id));box.append(b);});}
async function refreshSessions(){sessions=(await api('/api/sessions')).sessions;renderSessions();}
function renderState(){const c=state.character;$('character-name').textContent=c.name;$('character-tagline').textContent=c.tagline;avatar($('header-avatar'),c);$('scene').value=state.scene.id;$('relationship').value=String([0,10,20,30,40].filter(v=>v<=state.intimacy).pop()||0);$('memory-enabled').checked=!!state.session?.use_memory;$('model-status').textContent=state.mode==='live'?state.model:'离线演示 · 非真实对话';renderCharacters();renderSessions();}
async function openSession(id){if(busy)return;current=id;localStorage.setItem('muse-session',id);state=await api('/api/state'+qs());renderState();$('messages').replaceChildren();lastDebug=null;$('debug').innerHTML='<p class="muted">选择一条回复的“详情”，或发一条新消息。</p>';
 if(state.records.length){state.records.forEach(record=>{userTurn(record.user_text);const wrap=assistantTurn();record.payload.reply.segments.forEach(s=>appendSegment(wrap,s));finishTurn(wrap,record.payload,record.feedback);});}
 else{const welcome=document.createElement('div');welcome.className='welcome';welcome.innerHTML=`<div class="avatar"></div><h2>和 ${esc(state.character.name)} 聊一会儿</h2><p>${esc(state.character.tagline)}。从此刻想到的一件小事开始。</p>`;avatar(welcome.firstElementChild,state.character);$('messages').append(welcome);if(state.character.greeting){const w=assistantTurn();appendSegment(w,{spoken:true,text:state.character.greeting});}}
 notice(state.mode==='mock'?'当前是离线管道演示，不能用来评判角色质量。':'');$('sidebar').classList.remove('open');await loadMemory();scroll();$('input').focus();}
async function newChat(characterId=state?.character.id||'elise'){if(busy)return;const {session}=await api('/api/sessions',{character_id:characterId,scene_id:$('scene').value||'cafe'});await refreshSessions();await openSession(session.id);}
function userTurn(text){const w=document.createElement('div');w.className='turn user';w.innerHTML=`<div class="speaker">你</div><div class="bubble">${esc(text)}</div>`;$('messages').append(w);scroll();return w;}
function assistantTurn(){const w=document.createElement('div');w.className='turn assistant';w.innerHTML=`<div class="speaker">${esc(state.character.name)}</div>`;$('messages').append(w);return w;}
function appendSegment(w,s){
  const box=document.createElement('div');box.className='segment';
  const d=document.createElement('div');d.className=s.spoken?'bubble':'narration';d.textContent=s.text||'';
  box.append(d);
  const n=s.narration;
  if(n){
    const tags=[];
    for(const e of (n.emotion||[])){
      const pct=Math.round((Number(e.intensity)||0)*100);
      tags.push({k:'mood',label:`情绪 ${moods[e.name]||e.name}`,meter:pct});
    }
    if(n.pace&&paces[n.pace])tags.push({k:'pace',label:`语速 ${paces[n.pace]}`});
    if(n.intent)tags.push({k:'intent',label:`意图 ${intents[n.intent]||n.intent}`});
    for(const x of (n.expression||[]))tags.push({k:'expr',label:`表情 ${expressions[x]||x}`});
    if(tags.length){
      const bar=document.createElement('div');bar.className='segment-tags';
      for(const t of tags){
        const el=document.createElement('span');el.className='tag '+t.k;el.textContent=t.label;
        if(t.meter!==undefined){
          const m=document.createElement('i');m.className='meter';m.style.width=t.meter+'%';
          m.title=`强度 ${(t.meter/100).toFixed(1)}`;el.append(m);
        }
        bar.append(el);
      }
      box.append(bar);
      if(!s.spoken)box.classList.add('quiet');
    }
  }
  w.append(box);scroll();
}
function speak(s){if(!voice||!s.spoken||!s.text||!('speechSynthesis'in window))return;const u=new SpeechSynthesisUtterance(s.text);u.lang='zh-CN';u.rate={slow:.85,fast:1.15}[s.narration?.pace]||1;u.onstart=()=>{$('generation-status').textContent='试听 · '+(s.narration?.emotion||[]).map(e=>moods[e.name]||e.name).join(' / ');};u.onend=()=>{if(!speechSynthesis.pending&&!busy)$('generation-status').textContent='台词与情绪同步生成';};speechSynthesis.speak(u);}
function finishTurn(w,payload,feedback={}){const d=payload.debug||{};const actions=document.createElement('div');actions.className='turn-actions';actions.innerHTML=`<button data-like class="${feedback?.rating===1?'selected':''}">喜欢</button><button data-dislike class="${feedback?.rating===-1?'selected':''}">不合适</button><button data-detail>详情</button><button data-play>朗读</button><span class="timing">${d.first_segment_ms!=null?'首段 '+(d.first_segment_ms/1000).toFixed(1)+'s':(d.latency_ms/1000).toFixed(1)+'s'}</span>`;actions.querySelector('[data-like]').onclick=()=>guard(async()=>{await api('/api/feedback',{session_id:current,turn_id:payload.reply.turn_id,rating:1});actions.querySelector('[data-like]').classList.add('selected');actions.querySelector('[data-dislike]').classList.remove('selected');notice('已保存这条回复的反馈。');});actions.querySelector('[data-dislike]').onclick=()=>{feedbackTurn={id:payload.reply.turn_id,actions};$('feedback-note').value=feedback?.note||'';$('feedback-dialog').showModal();};actions.querySelector('[data-detail]').onclick=()=>{debug(payload);$('inspector').classList.remove('hidden');};actions.querySelector('[data-play]').onclick=()=>{if(!('speechSynthesis'in window)){notice('当前浏览器不支持试听。',true);return;}speechSynthesis.cancel();const previous=voice;voice=true;payload.reply.segments.forEach(speak);voice=previous;notice('浏览器声音试听，尚未接入产品 TTS 与视频形象。');};w.append(actions);}
function debug(payload){lastDebug=payload;const d=payload.debug||{};const checks=d.checks||{};$('debug').innerHTML=`<h3>本轮表达</h3>${payload.reply.segments.map(s=>`<p>${esc(s.text)}</p><div>${(s.narration?.emotion||[]).map(e=>`<span class="badge">${esc(moods[e.name]||e.name)} ${e.intensity}</span>`).join('')}<span class="badge">${esc(paces[s.narration?.pace]||'自然')}</span>${(s.narration?.expression||[]).map(e=>`<span class="badge">${esc(e)}</span>`).join('')}</div>`).join('')}<h3>生成信息</h3><pre>${esc(JSON.stringify({model:payload.reply.meta?.model,prompt:d.context?.prompt_version,first_segment_ms:d.first_segment_ms,total_ms:d.latency_ms,history_messages:d.context?.recent_count,mode:d.mode,regenerated:d.regenerated},null,2))}</pre><h3>协议检查</h3><p>${checks.hard_fail?'未通过':'可解析'}${checks.warnings?.length?' · 有风格提示':''}</p><p class="muted">协议通过不等于对话质量通过；自然度与人设以你的体验为准。</p><details><summary>实际发送给模型的消息</summary><pre>${esc(JSON.stringify(d.messages||[],null,2))}</pre></details><details><summary>上下文使用量</summary><pre>${esc(JSON.stringify(d.budget,null,2))}</pre></details><details><summary>完整输出协议</summary><pre>${esc(JSON.stringify(payload.reply,null,2))}</pre></details>`;}
async function loadMemory(){const data=await api('/api/memory'+qs());$('memory').innerHTML=data.memories.map(m=>`<p class="muted">${esc(m.content)}</p>`).join('')||'<p class="muted">当前会话暂无长期记忆。</p>';}
async function settings(){if(busy)return;const out=await api('/api/session/settings',{session_id:current,scene_id:$('scene').value,intimacy:Number($('relationship').value),use_memory:$('memory-enabled').checked});state=out.state;renderState();notice('已更新当前对话的场景与关系。');}
async function send(event){event?.preventDefault();if(busy||!current)return;const text=$('input').value.trim();if(!text)return;const submittedSession=current;setBusy(true);notice('');$('input').value='';$('input').style.height='auto';userTurn(text);const w=assistantTurn();const typing=document.createElement('div');typing.className='typing';typing.textContent='正在想怎么接你的话…';w.append(typing);scroll();controller=new AbortController();requestId=crypto.randomUUID();let complete=false,segments=0;
 try{const response=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:submittedSession,request_id:requestId,message:text}),signal:controller.signal});if(!response.ok){const e=await response.json();throw Error(e.error||'生成失败');}const reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';
 const processEvent=block=>{let name='message',data='';for(const line of block.split('\n')){if(line.startsWith('event:'))name=line.slice(6).trim();if(line.startsWith('data:'))data+=line.slice(5).trim();}if(!data)return;const p=JSON.parse(data);if(name==='segment'){typing.remove();appendSegment(w,p);speak(p);segments++;$('generation-status').textContent='正在回应…';}if(name==='error')throw Error(p.error);if(name==='done'){typing.remove();if(!segments)p.reply.segments.forEach(s=>appendSegment(w,s));finishTurn(w,p);debug(p);complete=true;}};
 while(true){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let pos;while((pos=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,pos);buffer=buffer.slice(pos+2);processEvent(block);}}
 if(!complete)throw Error('连接中断，本轮未保存。');await refreshSessions();await loadMemory();
 }catch(e){typing.remove();if('speechSynthesis'in window)speechSynthesis.cancel();w.remove();notice(e.name==='AbortError'?'已停止，本轮不计入对话历史。':'未完成：'+e.message+' 输入已保留，可重试。',e.name!=='AbortError');if(!$('input').value)$('input').value=text;}
 finally{controller=null;setBusy(false);$('input').focus();scroll();}}
async function guard(fn){try{await fn();}catch(e){notice(e.message,true);}}
$('composer').onsubmit=send;
$('input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send();}};
$('input').oninput=()=>{$('input').style.height='auto';$('input').style.height=Math.min($('input').scrollHeight,170)+'px';};
$('stop').onclick=()=>{if(!controller)return;api('/api/chat/cancel',{session_id:current,request_id:requestId}).finally(()=>controller?.abort());};
$('new-chat').onclick=()=>guard(()=>newChat());
$('scene').onchange=()=>guard(settings);$('relationship').onchange=()=>guard(settings);$('memory-enabled').onchange=()=>guard(settings);
$('inspect').onclick=()=>{const open=$('inspector').classList.contains('hidden');$('inspector').classList.toggle('hidden');$('inspect').setAttribute('aria-expanded',String(open));};
$('close-inspector').onclick=()=>{$('inspector').classList.add('hidden');$('inspect').setAttribute('aria-expanded','false');};
$('menu').onclick=()=>$('sidebar').classList.toggle('open');
$('voice').onclick=()=>{if(!('speechSynthesis'in window)){notice('当前浏览器不支持试听。',true);return;}voice=!voice;$('voice').textContent='试听：'+(voice?'开':'关');$('voice').setAttribute('aria-pressed',String(voice));if(!voice)speechSynthesis.cancel();notice(voice?'使用浏览器语音试听；音色与情绪不代表最终 TTS 效果。':'已关闭自动试听。');};
$('add-character').onclick=()=>{$('card-form').reset();importedScenario='';$('card-error').textContent='';$('card-dialog').showModal();};
$('close-card').onclick=()=>$('card-dialog').close();
$('card-file').onchange=async()=>{try{const file=$('card-file').files[0];if(!file)return;if(file.size>200000)throw Error('文件过大，请使用小于 200KB 的 JSON 角色卡');const raw=JSON.parse(await file.text());const c=raw.data||raw;importedScenario=c.scenario||'';$('card-name').value=c.name||c.identity?.name||'';$('card-description').value=c.description||'';$('card-personality').value=c.personality||'';$('card-greeting').value=c.first_mes||c.greeting||'';$('card-examples').value=typeof(c.mes_example||c.examples)==='string'?(c.mes_example||c.examples):JSON.stringify(c.examples||[],null,2);$('card-error').textContent='已读取基础角色字段。卡片中的全局指令覆盖、世界书与 PNG 图片暂不导入。';}catch(e){$('card-error').textContent=e.message;}};
$('card-form').onsubmit=async e=>{e.preventDefault();$('save-card').disabled=true;try{const raw=$('card-examples').value;let examples=raw;try{examples=JSON.parse(raw);}catch{}const out=await api('/api/characters',{card:{name:$('card-name').value,description:$('card-description').value,personality:$('card-personality').value,greeting:$('card-greeting').value,scenario:importedScenario,examples}});characters=(await api('/api/characters')).characters;$('card-dialog').close();await newChat(out.character.id);}catch(e){$('card-error').textContent=e.message;}finally{$('save-card').disabled=false;}};
$('close-feedback').onclick=()=>$('feedback-dialog').close();
$('feedback-form').onsubmit=e=>{e.preventDefault();guard(async()=>{await api('/api/feedback',{session_id:current,turn_id:feedbackTurn.id,rating:-1,note:$('feedback-note').value});feedbackTurn.actions.querySelector('[data-dislike]').classList.add('selected');feedbackTurn.actions.querySelector('[data-like]').classList.remove('selected');$('feedback-dialog').close();notice('已保存反馈，包含这轮的角色、提示词与回复。');});};
$('export').onclick=()=>guard(async()=>{const data=await api('/api/export'+qs());const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='muse-conversation.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
(async()=>{try{const health=await api('/api/health');if(health.version!=='0.2')throw Error('请重启服务以加载新版本');characters=(await api('/api/characters')).characters;scenes=(await api('/api/scenes')).scenes;$('scene').innerHTML=scenes.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');$('scene').value='cafe';await refreshSessions();const saved=localStorage.getItem('muse-session');if(sessions.some(s=>s.id===saved))await openSession(saved);else if(sessions.length)await openSession(sessions[0].id);else await newChat('elise');}catch(e){notice('连接失败：'+e.message,true);}})();
