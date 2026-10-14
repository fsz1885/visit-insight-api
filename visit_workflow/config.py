from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
MODEL = "k3"
MODELS = {'k3': 'K3', 'k3-256k': 'K3 · 256K', 'kimi-for-coding': 'K2.8 Preview'}
PROVIDERS = {
    'kimi': {'name': 'Kimi Code', 'models': MODELS, 'urls': ['https://api.kimi.com/coding/v1', 'https://api.kimi.ai/coding/v1'], 'default_model': 'k3', 'efforts': ['low', 'high', 'max'], 'max_tokens': 131072},
    'deepseek': {'name': 'DeepSeek', 'models': {'deepseek-flash': 'DeepSeek V4.1 Flash', 'deepseek-v4-pro': 'DeepSeek V4 Pro'}, 'urls': ['https://api.deepseek.com', 'https://api.deepseek.com/v1'], 'default_model': 'deepseek-flash', 'efforts': ['low', 'high', 'max', 'none'], 'max_tokens': 393216},
}
EDITABLE = ('provider', 'model', 'effort', 'max_tokens', 'timeout', 'max_concurrency', 'base_url')


def local_env() -> dict[str, str]:
    data = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip().strip('"\'')
    return data


@dataclass
class Settings:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.kimi.com/coding/v1"
    effort: str = "high"
    timeout: float = 600
    max_tokens: int = 22000
    max_concurrency: int = 3
    model: str = MODEL
    provider: str = 'kimi'
    credentials: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError('请选择支持的 API 厂商')
        spec = PROVIDERS[self.provider]
        if self.model not in spec['models']:
            raise ValueError('请选择该厂商支持的模型')
        if not 1024 <= self.max_tokens <= spec['max_tokens']:
            raise ValueError(f"最大输出 token 必须在 1024–{spec['max_tokens']} 之间")
        if not 30 <= self.timeout <= 1800:
            raise ValueError('超时必须在 30–1800 秒之间')
        if not 1 <= self.max_concurrency <= 8:
            raise ValueError('并发数必须为 1 到 8')
        self.base_url = self.base_url.rstrip('/')
        if self.base_url not in spec['urls']:
            raise ValueError('接口地址必须是所选厂商的官方地址')
        if self.effort not in spec['efforts']:
            raise ValueError('所选厂商不支持该思考强度')
        self.credentials = dict(self.credentials)
        if self.api_key:
            self.credentials[self.provider] = self.api_key

    @property
    def provider_name(self):
        return PROVIDERS[self.provider]['name']

    def select(self, options: dict, api_key: str | None = None):
        """Credentials are scoped to the provider; never inherit a different provider's key."""
        from dataclasses import replace
        provider = options.get('provider', self.provider)
        key = self.api_key if provider == self.provider else self.credentials.get(provider, '')
        if api_key is not None and api_key.strip():
            key = api_key.strip()
        return replace(self, **options, api_key=key, credentials=dict(self.credentials))

    @classmethod
    def load(cls) -> "Settings":
        data = {**local_env(), **os.environ}
        model = {}
        path = ROOT / "model.json"
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                candidates = value if isinstance(value, list) else value.get("models", [value])
                model = next((m for m in candidates if isinstance(m, dict) and (m.get("id") in {MODEL, "kimi-k3"} or m.get("model") in {MODEL, "kimi-k3"})), {})
                if not model:
                    model = next((m for m in candidates if isinstance(m, dict) and (m.get('model') in MODELS or m.get('id') in MODELS)), {})
            except (json.JSONDecodeError, AttributeError) as exc:
                raise ValueError("model.json 格式无效，需模型对象或模型数组") from exc
        # Read provider-specific local credentials, without publishing them to the browser.
        candidates = candidates if path.exists() else []
        deepseek = next((m for m in candidates if isinstance(m, dict) and
                         (m.get('provider') == 'deepseek' or m.get('model', m.get('id', '')) in PROVIDERS['deepseek']['models'])), {})
        keys = {
            'kimi': data.get('KIMI_CODE_API_KEY') or data.get('KIMI_API_KEY') or model.get('apiKey') or model.get('api_key', ''),
            'deepseek': data.get('DEEPSEEK_API_KEY') or deepseek.get('apiKey') or deepseek.get('api_key', ''),
        }
        defaults = dict(api_key=keys['kimi'], credentials=keys,
                   base_url=data.get('KIMI_CODE_BASE_URL') or model.get('url') or model.get('base_url') or cls.base_url,
                   effort=data.get('KIMI_CODE_REASONING_EFFORT') or model.get('reasoning', {}).get('defaultEffort', 'high'),
                   timeout=float(data.get('KIMI_CODE_TIMEOUT', '600')),
                   max_tokens=min(int(model.get('maxOutputTokens', 22000)), 32000),
                   max_concurrency=int(data.get('KIMI_CODE_CONCURRENCY', '3')),
                   model=data.get('KIMI_CODE_MODEL') or (model.get('model') if model.get('model') in MODELS else model.get('id') if model.get('id') in MODELS else MODEL))
        selected = cls(**defaults)
        if data.get('LLM_PROVIDER') == 'deepseek' or (deepseek and not model):
            selected = selected.select({'provider': 'deepseek', 'model': data.get('DEEPSEEK_MODEL') or deepseek.get('model') or deepseek.get('id') or 'deepseek-flash',
                                        'base_url': data.get('DEEPSEEK_BASE_URL') or deepseek.get('url') or deepseek.get('base_url') or 'https://api.deepseek.com',
                                        'effort': data.get('DEEPSEEK_REASONING_EFFORT', 'high')})
        return selected.with_saved(RUNS / '.model-settings.json')

    def with_saved(self, path: Path):
        if not path.exists():
            return self
        value = json.loads(path.read_text(encoding='utf-8'))
        return self.select({k:value[k] for k in EDITABLE if k in value})

    def options(self):
        return {key:getattr(self,key) for key in EDITABLE}

    def public(self) -> dict:
        return {"configured": bool(self.api_key), **self.options()}
