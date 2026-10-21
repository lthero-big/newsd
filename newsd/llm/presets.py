"""Model provider presets (the model settings page of the accepted v2 demo, design/demo-v2/assets/providers.js).

Every preset names a wire format and a default base URL; the user may override the base URL
(self-hosted gateway / proxy), which is then checked by :mod:`newsd.llm.netguard` before use.

Wire formats:
  openai     -> ``{base}/chat/completions``                       (base usually ends with /v1)
  anthropic  -> ``{base}/messages``                                (base ends with /v1)
  gemini     -> ``{base}/models/{model}:generateContent``          (base ends with /v1beta)
  ollama     -> ``{root}/api/chat``                                (a trailing /v1 is ignored)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    id: str
    region: str  # intl | cn | local
    name: str
    wire: str
    base: str
    models: tuple[str, ...] = ()
    key_hint: str = ""
    short: str = ""
    color: str = "#334155"
    json_mode: bool = False  # accepts response_format=json_object
    keyless: bool = False
    hidden: bool = False  # legacy id kept for stored configs / old API clients
    alias_of: str | None = None

    def public(self) -> dict:
        return {"id": self.id, "region": self.region, "name": self.name, "wire": self.wire, "base_url": self.base,
                "models": list(self.models), "key_hint": self.key_hint, "short": self.short, "color": self.color, "needs_key": not self.keyless,
                "needs_base_url": not self.base}


REGIONS = (("intl", "国外"), ("cn", "国内"), ("local", "本地 / 自定义"))

_P = [
    Preset('openai', 'intl', 'OpenAI', 'openai', 'https://api.openai.com/v1', ('gpt-4.1', 'gpt-4.1-mini', 'o4-mini'), 'sk-…', 'Oa', '#10a37f', json_mode=True),
    Preset('anthropic', 'intl', 'Anthropic Claude', 'anthropic', 'https://api.anthropic.com/v1', ('claude-sonnet-4-5', 'claude-haiku-4-5', 'claude-opus-4-1'), 'sk-ant-…', 'Cl', '#d97757'),
    Preset('gemini', 'intl', 'Google Gemini', 'gemini', 'https://generativelanguage.googleapis.com/v1beta', ('gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.5-flash-lite'), 'AIza…', 'Gm', '#4285f4'),
    Preset('xai', 'intl', 'xAI Grok', 'openai', 'https://api.x.ai/v1', ('grok-4', 'grok-4-fast', 'grok-3-mini'), 'xai-…', 'Gk', '#111111'),
    Preset('mistral', 'intl', 'Mistral', 'openai', 'https://api.mistral.ai/v1', ('mistral-large-latest', 'mistral-medium-latest', 'mistral-small-latest'), '', 'Mi', '#fa520f', json_mode=True),
    Preset('groq', 'intl', 'Groq', 'openai', 'https://api.groq.com/openai/v1', ('llama-3.3-70b-versatile', 'openai/gpt-oss-120b', 'qwen/qwen3-32b'), 'gsk_…', 'Gq', '#f55036', json_mode=True),
    Preset('openrouter', 'intl', 'OpenRouter', 'openai', 'https://openrouter.ai/api/v1', ('anthropic/claude-sonnet-4.5', 'openai/gpt-5', 'deepseek/deepseek-chat'), 'sk-or-…', 'Or', '#6467f2'),
    Preset('deepseek', 'cn', 'DeepSeek', 'openai', 'https://api.deepseek.com/v1', ('deepseek-chat', 'deepseek-reasoner'), 'sk-…', 'Ds', '#4d6bfe', json_mode=True),
    Preset('qwen', 'cn', '通义千问 · 阿里百炼', 'openai', 'https://dashscope.aliyuncs.com/compatible-mode/v1', ('qwen-max', 'qwen-plus', 'qwen-turbo', 'qwen3-235b-a22b'), 'sk-…', 'Qw', '#615ced', json_mode=True),
    Preset('glm', 'cn', '智谱 GLM', 'openai', 'https://open.bigmodel.cn/api/paas/v4', ('glm-4.6', 'glm-4.5-air', 'glm-4-flash'), '', 'Zp', '#2563eb'),
    Preset('kimi', 'cn', 'Moonshot Kimi', 'openai', 'https://api.moonshot.cn/v1', ('kimi-k2-0905-preview', 'moonshot-v1-32k'), 'sk-…', 'Km', '#16a34a', json_mode=True),
    Preset('doubao', 'cn', '豆包 · 火山方舟', 'openai', 'https://ark.cn-beijing.volces.com/api/v3', ('doubao-seed-1-6', 'doubao-1-5-pro-32k'), '方舟 API Key（模型填接入点 ep-…）', 'Db', '#0ea5e9'),
    Preset('qianfan', 'cn', '百度千帆 ERNIE', 'openai', 'https://qianfan.baidubce.com/v2', ('ernie-4.5-turbo-128k', 'ernie-x1-turbo-32k'), 'bce-v3/…', 'Er', '#2932e1'),
    Preset('hunyuan', 'cn', '腾讯混元', 'openai', 'https://api.hunyuan.cloud.tencent.com/v1', ('hunyuan-turbos-latest', 'hunyuan-t1-latest'), 'sk-…', 'Hy', '#0052d9'),
    Preset('minimax', 'cn', 'MiniMax', 'openai', 'https://api.minimaxi.com/v1', ('MiniMax-M2', 'abab6.5s-chat'), '', 'Mm', '#e11d48'),
    Preset('stepfun', 'cn', '阶跃星辰', 'openai', 'https://api.stepfun.com/v1', ('step-2-16k', 'step-1-8k'), '', 'St', '#7c3aed'),
    Preset('yi', 'cn', '零一万物', 'openai', 'https://api.lingyiwanwu.com/v1', ('yi-lightning', 'yi-large'), '', 'Yi', '#0f766e'),
    Preset('siliconflow', 'cn', '硅基流动 SiliconFlow', 'openai', 'https://api.siliconflow.cn/v1', ('deepseek-ai/DeepSeek-V3', 'Qwen/Qwen3-235B-A22B', 'THUDM/GLM-4-32B-0414'), 'sk-…', 'Sf', '#8b5cf6'),
    Preset('ollama', 'local', 'Ollama · 本地', 'ollama', 'http://localhost:11434', ('llama3.3', 'qwen3:8b', 'gemma3:12b'), '（本地无需 Key，可留空）', 'Ol', '#525252', keyless=True),
    Preset('custom', 'local', '自定义 OpenAI 兼容', 'openai', '', (), 'sk-…', '＋', '#334155'),
    # legacy ids (stored configs and the v1 /api/me/llm API keep working)
    Preset('tongyi', 'cn', '通义千问 (DashScope)', 'openai', 'https://dashscope.aliyuncs.com/compatible-mode/v1', ('qwen-plus',), 'sk-…', 'Qw', '#615ced', json_mode=True, hidden=True, alias_of='qwen'),
    Preset('openai_compatible', 'local', 'OpenAI 兼容（管理员配置的地址）', 'openai', '', (), '', '＋', '#334155', hidden=True, alias_of='custom'),
]
PRESETS: dict[str, Preset] = {p.id: p for p in _P}
VISIBLE = tuple(p for p in _P if not p.hidden)


def get(preset_id: str) -> Preset | None:
    return PRESETS.get(preset_id)
