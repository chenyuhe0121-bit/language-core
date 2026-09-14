'use strict';

const USER_ID = 'local';

const el = (id) => document.getElementById(id);

let STATE = null;
let SCENES = [];

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { data });
  return data;
}

/* ---------------- 状态与侧栏 ---------------- */

async function loadState() {
  STATE = await api(`/api/state?user_id=${USER_ID}`);
  el('s-scene').textContent = STATE.scene.name;
  el('s-stage').textContent = STATE.stage.name;
  el('s-intimacy').textContent = STATE.intimacy;
  el('s-mode').textContent = STATE.mode === 'live' ? STATE.model : 'mock（离线）';
  el('h-scene').textContent = `${STATE.character.name} · ${STATE.scene.name}`;
  el('h-goal').textContent = STATE.scene.goal || '';
  renderScenes();
  await loadMemory();
}

function renderScenes() {
  const box = el('scene-list');
  box.innerHTML = '';
  SCENES.forEach((s) => {
    const unlocked = STATE.unlocked_scenes.includes(s.id);
    const active = STATE.scene.id === s.id;
    const div = document.createElement('div');
    div.className = 'scene-item' + (active ? ' active' : '') + (unlocked ? '' : ' locked');
    div.innerHTML = `<span>${s.name}</span><span class="meta">${unlocked ? (active ? '当前' : '可进入') : '未解锁'}</span>`;
    if (unlocked && !active) div.onclick = () => switchScene(s.id);
    box.appendChild(div);
  });
}

async function switchScene(sceneId) {
  try {
    const res = await api('/api/scene', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: USER_ID, scene_id: sceneId }),
    });
    STATE = res.state;
    el('s-scene').textContent = STATE.scene.name;
    el('h-scene').textContent = `${STATE.character.name} · ${STATE.scene.name}`;
    el('h-goal').textContent = STATE.scene.goal || '';
    renderScenes();
    addSystemNote(`转到「${STATE.scene.name}」——关系状态不变，她不会退回生疏。`);
  } catch (e) {
    addSystemNote(e.message);
  }
}

async function loadMemory() {
  const data = await api(`/api/memory?user_id=${USER_ID}`);

  const box = el('memory-list');
  box.innerHTML = '';
  if (!data.memories.length) {
    box.innerHTML = '<div class="memory-empty">还没有记住任何事。聊几句试试。</div>';
  }
  data.memories.forEach((m) => {
    const div = document.createElement('div');
    div.className = 'memory-item';
    div.innerHTML = `
      <div>${escapeHtml(m.content)}</div>
      <div class="meta">
        <span class="mtype">${m.type} · 置信 ${m.confidence}</span>
        <span>
          <button class="mini link" data-edit="${m.id}">改</button>
          <button class="mini link" data-del="${m.id}">删</button>
        </span>
      </div>`;
    box.appendChild(div);
  });

  box.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = async () => {
      await api('/api/memory/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ memory_id: b.dataset.del }),
      });
      addSystemNote('已删除这条记忆。她后续不会再提起它。');
      loadMemory();
    };
  });

  box.querySelectorAll('[data-edit]').forEach((b) => {
    b.onclick = async () => {
      const current = b.closest('.memory-item').firstElementChild.textContent;
      const next = prompt('修改这条记忆（她会按新内容记）：', current);
      if (next === null) return;
      await api('/api/memory/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ memory_id: b.dataset.edit, content: next }),
      });
      loadMemory();
    };
  });

  const loops = el('loop-list');
  loops.innerHTML = '';
  if (!data.open_loops.length) {
    loops.innerHTML = '<div class="memory-empty">没有未结的事。</div>';
  }
  data.open_loops.forEach((l) => {
    const div = document.createElement('div');
    div.className = 'memory-item';
    div.innerHTML = `<div>${escapeHtml(l.topic)}</div>
      <div class="meta"><span class="mtype">${l.status}</span><span>${l.created_scene || ''}</span></div>`;
    loops.appendChild(div);
  });
}

/* ---------------- 对话渲染 ---------------- */

function addSystemNote(text) {
  const div = document.createElement('div');
  div.className = 'system-note';
  div.textContent = text;
  el('messages').appendChild(div);
  scrollBottom();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function renderTurn(reply) {
  const wrap = document.createElement('div');
  wrap.className = 'turn assistant';

  reply.segments.forEach((seg) => {
    const d = document.createElement('div');

    if (seg.type === 'inner_thought') {
      d.className = 'seg seg-thought';
      d.textContent = `（${seg.text || ''}）`;
      wrap.appendChild(d);
      return;
    }

    if (!seg.spoken) {
      d.className = 'seg seg-narration';
      d.textContent = `（${seg.text || ''}）`;
      wrap.appendChild(d);
      return;
    }

    d.className = 'seg';
    const bubble = document.createElement('div');
    bubble.className = 'seg-dialogue';
    bubble.textContent = seg.text || '';
    d.appendChild(bubble);

    const n = seg.narration;
    if (n) {
      const tags = document.createElement('div');
      tags.className = 'seg-tags';
      (n.emotion || []).forEach((e) => {
        tags.innerHTML += `<span class="tag mood">情绪 ${e.name} ${e.intensity}</span>`;
      });
      if (n.pace) tags.innerHTML += `<span class="tag">语速 ${n.pace}</span>`;
      if (n.intent) tags.innerHTML += `<span class="tag">意图 ${n.intent}</span>`;
      (n.expression || []).forEach((x) => {
        tags.innerHTML += `<span class="tag expr">表情 ${x}</span>`;
      });
      if (tags.children.length) d.appendChild(tags);
    }
    wrap.appendChild(d);
  });

  el('messages').appendChild(wrap);
  scrollBottom();
}

function renderUser(text) {
  const wrap = document.createElement('div');
  wrap.className = 'turn user';
  wrap.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  el('messages').appendChild(wrap);
  scrollBottom();
}

function scrollBottom() {
  const m = el('messages');
  m.scrollTop = m.scrollHeight;
}

/* ---------------- 调试面板 ---------------- */

function renderDebug(payload) {
  const d = payload.debug || {};
  const c = d.context || {};
  const b = d.budget || {};
  const checks = d.checks || {};

  const recalled = (c.recalled_memory_preview || []);
  const loops = c.open_loops || [];
  const failed = checks.failed || [];

  el('debug-body').innerHTML = `
    <h4>参数</h4>
    <pre>${escapeHtml(JSON.stringify(d.params || {}, null, 1))}</pre>

    <h4>上下文预算（字符）</h4>
    <pre>${escapeHtml(Object.entries(b.used || {}).map(([k, v]) => `${k}: ${v} / ${(b.budget || {})[k] ?? '-'}`).join('\n'))}</pre>
    ${(b.trimmed || []).length ? `<div class="bad">裁剪: ${b.trimmed.join(', ')}</div>` : ''}

    <h4>召回的记忆</h4>
    ${recalled.length ? `<ul>${recalled.map((x) => `<li>${escapeHtml(x)}</li>`).join('')}</ul>` : '<div>（无）</div>'}

    <h4>未闭合话题</h4>
    ${loops.length ? `<ul>${loops.map((x) => `<li>${escapeHtml(x)}</li>`).join('')}</ul>` : '<div>（无）</div>'}

    <h4>本场景可用表情</h4>
    <div>${(c.allowed_expressions || []).map(escapeHtml).join('、')}</div>

    <h4>校验</h4>
    <div class="${checks.passed ? 'ok' : 'bad'}">${checks.passed ? '全部通过' : '有未通过项'}</div>
    ${failed.length ? `<ul>${failed.map((x) => `<li>${escapeHtml(x)}</li>`).join('')}</ul>` : ''}
    <pre>${escapeHtml(JSON.stringify(checks.metrics || {}, null, 1))}</pre>

    <h4>解析</h4>
    <div>状态 ${c.parse_status || (checks.metrics || {}).parse_status || '-'}，重生成 ${d.regenerated || 0} 次</div>
    ${d.error ? `<div class="bad">模型错误：${escapeHtml(d.error)}（已降级）</div>` : ''}
  `;
}

/* ---------------- 发送 ---------------- */

async function send() {
  const input = el('input');
  const text = input.value.trim();
  if (!text) return;

  input.value = '';
  input.style.height = 'auto';
  el('btn-send').disabled = true;

  renderUser(text);

  try {
    const payload = await api('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: USER_ID, message: text }),
    });
    renderTurn(payload.reply);
    renderDebug(payload);
    el('s-scene').textContent = STATE.scene.name;
    await loadState();
  } catch (e) {
    addSystemNote('出错：' + e.message);
  } finally {
    el('btn-send').disabled = false;
    input.focus();
  }
}

/* ---------------- 事件 ---------------- */

el('btn-send').onclick = send;
el('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
el('input').addEventListener('input', () => {
  const t = el('input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 140) + 'px';
});
el('btn-debug').onclick = () => el('debug').classList.toggle('hidden');
el('btn-memory-refresh').onclick = loadMemory;
el('btn-export').onclick = async () => {
  const data = await api(`/api/export?user_id=${USER_ID}`);
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'language-core-export.json';
  a.click();
};
el('btn-wipe').onclick = async () => {
  if (!confirm('删除该角色下全部记忆与对话，不可恢复。继续？')) return;
  await api('/api/delete_all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: USER_ID }),
  });
  el('messages').innerHTML = '';
  addSystemNote('已清空。她从零开始认识你。');
  await loadState();
};

/* ---------------- 启动 ---------------- */

(async function boot() {
  const health = await api('/api/health');
  if (!health.assets.ok) {
    addSystemNote('资产校验未通过：' + (health.assets.errors || []).join('; '));
  }
  SCENES = (await api('/api/scenes')).scenes;
  await loadState();

  const recent = STATE.recent || [];
  if (!recent.length) {
    addSystemNote('新会话。尝试告诉她你的名字，或说说今天过得怎么样。');
  } else {
    recent.forEach((t) => {
      if (t.role === 'user') renderUser(t.content);
      else {
        const wrap = document.createElement('div');
        wrap.className = 'turn assistant';
        const d = document.createElement('div');
        d.className = 'seg';
        d.innerHTML = `<div class="seg-dialogue">${escapeHtml(t.content)}</div>`;
        wrap.appendChild(d);
        el('messages').appendChild(wrap);
      }
    });
    scrollBottom();
    addSystemNote('以上是上次的对话记录。她记得这些。');
  }
  el('input').focus();
})();
