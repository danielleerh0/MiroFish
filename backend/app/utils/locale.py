"""English-only string lookup for ScenarioIQ.

The locale API (set_locale / get_locale) is kept so existing callers work,
but the only supported locale is 'en'.
"""
import json
import os

LOCALE = 'en'

_locales_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'locales')

with open(os.path.join(_locales_dir, 'languages.json'), 'r', encoding='utf-8') as f:
    _languages = json.load(f)

with open(os.path.join(_locales_dir, 'en.json'), 'r', encoding='utf-8') as f:
    _messages = json.load(f)


def set_locale(locale: str):
    """No-op. Kept for compatibility with background-thread callers."""


def get_locale() -> str:
    return LOCALE


def t(key: str, **kwargs) -> str:
    value = _messages
    for part in key.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break
    if value is None:
        return key
    for k, v in kwargs.items():
        value = value.replace(f'{{{k}}}', str(v))
    return value


def get_language_instruction() -> str:
    return _languages[LOCALE]['llmInstruction']
