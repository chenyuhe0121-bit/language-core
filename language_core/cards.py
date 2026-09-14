"""JSON character-card import. Private author overrides never become product policy."""
import json
import re
import uuid
from . import config, persona


def normalize_card(value):
    if not isinstance(value, dict):
        raise ValueError('角色卡必须是 JSON 对象')
    data = value.get('data', value)
    if not isinstance(data, dict):
        raise ValueError('角色卡 data 必须是对象')
    name = str(data.get('name') or data.get('identity', {}).get('name') or '').strip()
    description = str(data.get('description') or '').strip()
    if not name or not description:
        raise ValueError('请填写角色名称与描述')
    if len(name) > 60 or len(description) > 18000:
        raise ValueError('名称最多 60 字，角色描述最多 18000 字')
    examples = data.get('examples') or data.get('mes_example') or []
    if not isinstance(examples, (str, list)):
        raise ValueError('示例必须是文本或对话列表')
    raw = {'id': 'custom_' + uuid.uuid4().hex[:12], 'version': '2.0',
           'identity': {'name': name}, 'description': description,
           'personality': str(data.get('personality') or ''),
           'greeting': str(data.get('greeting') or data.get('first_mes') or ''),
           'examples': examples, 'scenario': str(data.get('scenario') or ''),
           'color': '#547867', 'tagline': str(data.get('tagline') or '自定义角色')[:100]}
    if len(json.dumps(raw, ensure_ascii=False)) > 26000:
        raise ValueError('角色卡内容过长，请精简到 26000 字以内')
    return raw


def save_card(value):
    card = normalize_card(value)
    directory = config.DATA_DIR / 'characters'
    directory.mkdir(exist_ok=True)
    (directory / (card['id'] + '.json')).write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding='utf-8')
    persona.all_character_ids.cache_clear()
    persona.load_character.cache_clear()
    return card


def public_card(cid):
    ch = persona.load_character(cid)
    return {'id': ch.id, 'name': ch.name, 'tagline': ch.raw.get('tagline', ch.inner_summary),
            'color': ch.raw.get('color', '#b98a6d'), 'greeting': ch.raw.get('greeting', ''),
            'description': ch.raw.get('description', ch.inner_summary),
            'personality': ch.raw.get('personality', ''), 'examples': ch.raw.get('examples', []),
            'scenario': ch.raw.get('scenario', '')}
