"""Live quality probe, synthetic conversations only; output is ignored by Git."""
import sys,json,time,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from run import apply_env
apply_env(False)
from language_core.engine import Engine
from language_core.memory import MemoryStore
from language_core import config
if config.MODE!='live':raise SystemExit('Live model configuration required')
questions=['我今天发现一家特别好吃的店。','是家面馆，不过老板一直跟我聊他养的猫，差点忘了给我下单。','你怎么什么都能接，你有没有不喜欢的东西？','其实我今天面试没过，刚才那些只是想岔开一下。','先别帮我分析原因，我现在只想随便聊聊。','那讲一件你最近搞砸的小事吧。']
rows=[]
for cid in ['elise','tangguo','shenyan']:
 eng=Engine(store=MemoryStore(':memory:'))
 try:
  for i,q in enumerate(questions):
   started=time.perf_counter();stamps=[]
   try:
    r=eng.respond(user_id='synthetic-eval',character_id=cid,scene_id='park',message=q,on_segment=lambda s:stamps.append(time.perf_counter()))
    row={'character':cid,'input':q,'dialogue':r.turn.dialogue,'segments':[s.to_dict() for s in r.turn.segments], 'checks':r.checks,'first_ms':round((stamps[0]-started)*1000) if stamps else None,'total_ms':round((time.perf_counter()-started)*1000)}
   except Exception as e:row={'character':cid,'input':q,'error':str(e)}
   rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
 finally:eng.shutdown()
path=ROOT/'data'/'_quality_probe.json';path.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
