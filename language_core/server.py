"""Local conversation studio HTTP API. SSE contains complete, validated speech segments."""
from __future__ import annotations
import json
import mimetypes
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import config, persona, proactive
from .cards import public_card, save_card
from .engine import Engine
from .memory import ConversationStore

WEB_DIR = config.ROOT / 'language_core' / 'web'

_asset_stamps = {}


def _asset_stamp(name):
    """静态资源的内容指纹。文件一变，URL 就变，浏览器缓存自动失效。"""
    path = WEB_DIR / name
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return '0'
    cached = _asset_stamps.get(name)
    if cached and cached[0] == key:
        return cached[1]
    import hashlib
    stamp = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
    _asset_stamps[name] = (key, stamp)
    return stamp
_engine_lock = threading.Lock()
_engine = None
_chat_lock = threading.Lock()
_active = {}
_active_lock = threading.Lock()

def engine():
    global _engine
    with _engine_lock:
        if _engine is None: _engine = Engine(store=ConversationStore())
        return _engine

class Handler(BaseHTTPRequestHandler):
    server_version = 'LanguageCore/0.2'
    def log_message(self, fmt, *args):
        print(f'[{self.log_date_time_string()}] {fmt % args}', flush=True)

    def _send_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length > 200000: raise ValueError('请求过大')
        value = json.loads(self.rfile.read(length) or b'{}')
        if not isinstance(value, dict): raise ValueError('请求必须为 JSON 对象')
        return value

    def _identity(self, values):
        owner = str(values.get('user_id') or 'local')
        sid = str(values.get('session_id') or '')
        if sid:
            convo = engine().store.conversation(owner, sid)
            return sid, convo['character_id'], convo
        character = str(values.get('character_id') or config.DEFAULT_CHARACTER)
        persona.load_character(character)
        return owner, character, None

    def _state(self, values):
        uid, cid, convo = self._identity(values)
        eng = engine()
        profile = eng.store.get_profile(uid, cid)
        scene = persona.load_scene(profile.get('current_scene', config.DEFAULT_SCENE))
        rel = eng.relationship(uid, cid)
        return {'character': public_card(cid),
                'scene': {'id': scene.id, 'name': scene.name, 'goal': scene.scene_goal.get('primary', '')},
                'stage': {'id': rel['stage'].id, 'name': rel['stage'].name}, 'intimacy': rel['intimacy'],
                'unlocked_scenes': list(persona.all_scene_ids()), 'mode': eng.llm.mode,
                'model': config.LLM_MODEL if eng.llm.mode == 'live' else 'mock',
                'recent': [x.to_dict() for x in eng.store.recent_turns(uid, cid, 60)],
                'session': convo,
                'records': eng.store.replies(str(values.get('user_id') or 'local'), uid) if convo else []}

    def _idle(self, values):
        """对话是不是卡住了，该不该由她主动开口。

        只做判断，不改状态。真正的「我开口了」在生成那一轮才落库，
        否则用户一刷新页面就会把额度用掉。
        """
        uid, cid, convo = self._identity(values)
        if not config.PROACTIVE_ENABLED:
            return {'due': False, 'reason': '主动开口已关闭', 'enabled': False}
        eng = engine()
        store = eng.store
        with _active_lock:
            generating = uid in _active
        decision = proactive.decide(
            last_activity_at=store.last_activity_at(uid, cid),
            last_proactive_at=store.last_proactive_at(uid, cid),
            user_turns_since_proactive=store.user_messages_since_proactive(uid, cid),
            generating=generating,
        )
        # 挑一种开口方式。用消息数当种子，同一轮的结果稳定；
        # 传入上一次用过的方式，避免连着两次用同一种。
        if decision.due:
            recent = store.last_two_user_messages(uid, cid)
            stall = proactive.StallSignal(
                idle_seconds=decision.idle_seconds,
                last_user_text=recent[-1] if recent else '',
                prev_user_text=recent[-2] if len(recent) > 1 else '',
            )
            decision.kind = proactive.pick_kind(
                scene=persona.load_scene(store.get_profile(uid, cid).get('current_scene', config.DEFAULT_SCENE)),
                char=persona.load_character(cid),
                stall=stall,
                last_kind=proactive.kind_from_history(store.last_proactive_context(uid, cid)),
                seed=store.message_count(uid, cid),
            )
        out = decision.to_dict()
        out['enabled'] = True
        out['poll_seconds'] = config.PROACTIVE_POLL_SECONDS
        return out

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path
            values = {k: v[0] for k,v in urllib.parse.parse_qs(parsed.query).items()}
            owner = str(values.get('user_id') or 'local')
            if route in ('/', '/app.js', '/app.css'):
                path = WEB_DIR / ('index.html' if route == '/' else route[1:])
                raw = path.read_bytes()
                if route == '/':
                    # 给静态资源的引用带上内容指纹。
                    # 不加这个，浏览器会一直用缓存里的旧 app.js——
                    # 表现是「代码改了但页面行为没变」，排查起来很费时间。
                    raw = raw.replace(b'/app.js',
                                      ('/app.js?v=' + _asset_stamp('app.js')).encode()).replace(
                                  b'/app.css',
                                  ('/app.css?v=' + _asset_stamp('app.css')).encode())
                self.send_response(200)
                self.send_header('Content-Type', (mimetypes.guess_type(str(path))[0] or 'text/plain') + '; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers(); self.wfile.write(raw)
            elif route == '/api/health':
                self._send_json({'ok': True, 'version': '0.2', 'mode': engine().llm.mode, 'model': config.LLM_MODEL, 'assets': persona.validate_assets().to_dict()})
            elif route == '/api/characters':
                self._send_json({'characters': [public_card(c) for c in persona.all_character_ids()]})
            elif route == '/api/scenes':
                self._send_json({'scenes': [{'id': s, 'name': persona.load_scene(s).name} for s in persona.all_scene_ids()]})
            elif route == '/api/sessions':
                self._send_json({'sessions': engine().store.conversations(owner)})
            elif route == '/api/idle': self._send_json(self._idle(values))
            elif route == '/api/state': self._send_json(self._state(values))
            elif route in ('/api/memory', '/api/export'):
                uid,cid,convo = self._identity(values)
                if route == '/api/export':
                    payload = engine().store.export_user(uid,cid)
                    if convo: payload['records'] = engine().store.replies(owner,uid)
                    self._send_json(payload)
                else:
                    store=engine().store
                    self._send_json({'memories':[m.to_dict() for m in store.all_memories(uid,cid)], 'summary':store.get_summary(uid,cid),
                                     'open_loops':[l.to_dict() for l in store.open_loops(uid,cid)]})
            else: self._send_json({'error':'接口不存在'},404)
        except (ValueError, FileNotFoundError, KeyError) as e: self._send_json({'error':str(e)},400)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception: self._send_json({'error':'服务内部错误，请查看本地日志'},500); raise

    def do_POST(self):
        streaming = False
        try:
            # Local-only browser API, no cross-origin writes.
            origin = self.headers.get('Origin')
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get('Host'):
                self._send_json({'error':'不接受跨站请求'},403); return
            body = self._body()
            route = urllib.parse.urlparse(self.path).path
            owner = str(body.get('user_id') or 'local')
            if route == '/api/characters':
                card=save_card(body.get('card')); self._send_json({'character':public_card(card['id'])}); return
            if route == '/api/sessions':
                cid = str(body.get('character_id') or config.DEFAULT_CHARACTER)
                scene = str(body.get('scene_id') or config.DEFAULT_SCENE)
                persona.load_character(cid); persona.load_scene(scene)
                convo=engine().store.create_conversation(owner,cid,scene)
                greeting=persona.load_character(cid).raw.get('greeting','')
                if greeting: engine().store.add_message(convo['id'],cid,'assistant',greeting,scene)
                self._send_json({'session':convo}); return
            uid,cid,convo = self._identity(body)
            if route == '/api/chat/cancel':
                with _active_lock:
                    active = _active.get(uid)
                    if active and active[0] == str(body.get('request_id') or ''):
                        active[1].set()
                self._send_json({'ok':True}); return
            if route == '/api/feedback':
                engine().store.save_feedback(owner,uid,str(body.get('turn_id')),int(body.get('rating',0)),str(body.get('note') or ''))
                self._send_json({'ok':True}); return
            if route in ('/api/scene','/api/session/settings'):
                scene_id=str(body.get('scene_id') or (convo or {}).get('scene_id') or config.DEFAULT_SCENE)
                persona.load_scene(scene_id)
                intimacy=int(body.get('intimacy',(convo or {}).get('intimacy',0)))
                if not 0 <= intimacy <= 100: raise ValueError('关系值超出范围')
                if convo:
                    engine().store.configure_conversation(owner,uid,scene_id,intimacy,bool(body.get('use_memory',convo['use_memory'])))
                else: engine().store.set_profile(uid,cid,'current_scene',scene_id)
                self._send_json({'ok':True,'state':self._state(body)}); return
            if route in ('/api/chat','/api/chat/stream'):
                message=str(body.get('message') or '').strip()
                # 主动开口那一轮没有用户输入，所以只有普通回合才要求非空
                if not body.get('proactive') and (not message or len(message)>6000):
                    raise ValueError('消息须为 1–6000 字')
                scene_id=str(body.get('scene_id') or (convo or {}).get('scene_id') or engine().store.get_profile(uid,cid).get('current_scene',config.DEFAULT_SCENE))
                persona.load_scene(scene_id)
                if not _chat_lock.acquire(blocking=False):
                    self._send_json({'error':'正在生成另一条回复，请稍后重试'},409); return
                cancel_event = threading.Event()
                with _active_lock:
                    _active[uid] = (str(body.get('request_id') or ''), cancel_event)
                try:
                    started=time.perf_counter(); first=None
                    def emit(name,payload):
                        raw=f'event: {name}\ndata: {json.dumps(payload,ensure_ascii=False)}\n\n'.encode('utf-8')
                        self.wfile.write(raw); self.wfile.flush()
                    if route.endswith('/stream'):
                        self.send_response(200); self.send_header('Content-Type','text/event-stream; charset=utf-8')
                        self.send_header('Cache-Control','no-cache'); self.send_header('Connection','close')
                        self.end_headers(); self.close_connection=True; streaming=True
                        emit('start',{'mode':engine().llm.mode})
                    def segment(value):
                        nonlocal first
                        if first is None: first=time.perf_counter()-started
                        emit('segment',value)
                    if body.get('proactive'):
                        # 主动开口：没有用户输入，由她起头
                        result=engine().proactive(user_id=uid,character_id=cid,scene_id=scene_id,
                            kind=str(body.get('kind') or 'bored'),seed=int(body.get('seed') or 0),
                            use_memory=bool((convo or {}).get('use_memory',False)),
                            on_segment=segment if streaming else None,cancelled=cancel_event.is_set)
                    else:
                        result=engine().respond(user_id=uid,character_id=cid,scene_id=scene_id,message=message,
                            use_memory=bool((convo or {}).get('use_memory',False)),on_segment=segment if streaming else None,cancelled=cancel_event.is_set)
                    payload=result.to_dict()
                    payload['debug']['mode']=engine().llm.mode
                    payload['debug']['latency_ms']=round((time.perf_counter()-started)*1000)
                    payload['debug']['first_segment_ms']=round(first*1000) if first is not None else None
                    payload['debug']['messages']=result.context.messages()
                    # 前端靠这个标记渲染「她主动开口」，落库也靠它参与计数
                    payload['debug'].setdefault('context',{})['proactive']=result.context.proactive
                    if convo: engine().store.save_reply(uid,message if message else '（她主动开口）',payload)
                    if streaming: emit('done',payload)
                    else: self._send_json(payload)
                except (BrokenPipeError,ConnectionResetError): pass
                except Exception as e:
                    if streaming: emit('error',{'error':str(e)})
                    else: self._send_json({'error':str(e)},502)
                finally:
                    with _active_lock: _active.pop(uid,None)
                    _chat_lock.release()
                return
            if route in ('/api/memory/delete','/api/memory/update'):
                mid=str(body.get('memory_id') or '')
                if mid not in {m.id for m in engine().store.all_memories(uid,cid)}: raise ValueError('记忆不存在')
                if route.endswith('delete'): ok=engine().store.delete_memory(mid)
                else:
                    content=str(body.get('content') or '').strip()
                    if not content: raise ValueError('记忆不能为空')
                    ok=engine().store.update_memory(mid,content)
                self._send_json({'ok':ok}); return
            self._send_json({'error':'接口不存在'},404)
        except (ValueError,FileNotFoundError,KeyError) as e:
            if not streaming: self._send_json({'error':str(e)},400)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception:
            if not streaming: self._send_json({'error':'服务内部错误，请查看本地日志'},500)
            raise

def serve():
    report=persona.validate_assets()
    print(f'Language Core 0.2 | {config.MODE} | http://{config.HOST}:{config.PORT}',flush=True)
    if not report.ok: print(report.to_dict(),flush=True)
    httpd=ThreadingHTTPServer((config.HOST,config.PORT),Handler)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        httpd.server_close()
        if _engine: _engine.shutdown()

if __name__=='__main__': serve()
