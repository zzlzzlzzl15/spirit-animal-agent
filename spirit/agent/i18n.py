"""轻量国际化（i18n）—— 点分 key 的 catalog 翻译。

移植自 Hermes ``agent/i18n.py``（精简可测试子集）。

解析优先级：显式 ``lang`` 参数 > 环境变量 ``SPIRIT_LANGUAGE`` > config 的
``display.language`` > 默认 ``en``。catalog 从 ``locales/<lang>.yaml`` 加载并展平为
点分 key 空间；缺失 catalog 或缺失 key 时逐级降级（目标语言 → en → 裸 key），
**绝不抛异常**。

零强依赖：PyYAML 缺失或 catalog 文件不存在时静默降级为返回 key 本身。
"""

import logging
import os
import sysconfig
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = (
    "en", "zh", "zh-hant", "ja", "de", "es", "fr", "ko", "ru", "pt",
)
DEFAULT_LANGUAGE = "en"

# 接受若干自然别名，让输入 "chinese" / "zh-CN" / "jp" 的用户拿到正确 catalog，
# 而非静默回退英文。
_LANGUAGE_ALIASES: Dict[str, str] = {
    "english": "en", "en-us": "en", "en-gb": "en",
    # 简体中文 —— 显式码路由到此；裸 "chinese" / "mandarin" 也默认简体。
    "chinese": "zh", "mandarin": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh",
    # 繁体中文 —— 独立 catalog。
    "traditional-chinese": "zh-hant", "traditional_chinese": "zh-hant",
    "zh-tw": "zh-hant", "zh-hk": "zh-hant", "zh-mo": "zh-hant",
    "japanese": "ja", "jp": "ja", "ja-jp": "ja",
    "german": "de", "deutsch": "de", "de-de": "de", "de-at": "de", "de-ch": "de",
    "spanish": "es", "español": "es", "espanol": "es", "es-es": "es", "es-mx": "es",
    "french": "fr", "français": "fr", "france": "fr", "fr-fr": "fr", "fr-be": "fr",
    "korean": "ko", "한국어": "ko", "ko-kr": "ko",
    "russian": "ru", "русский": "ru", "ru-ru": "ru",
    "portuguese": "pt", "português": "pt", "portugues": "pt", "pt-pt": "pt", "pt-br": "pt",
}

_catalog_cache: Dict[str, Dict[str, str]] = {}
_catalog_lock = threading.Lock()


def _locales_dir() -> Path:
    """返回存放 locale YAML 文件的目录。

    解析顺序，首个存在者胜出：
    1. ``SPIRIT_BUNDLED_LOCALES`` 环境变量 —— 密封打包系统指向已安装 catalog 目录。
    2. ``<repo-root>/locales`` —— 源码检出与 ``pip install -e .``。
    3. ``<sysconfig data|purelib|platlib>/locales`` —— pip wheel 安装。

    即使都不存在也回退到源码式路径，让 ``_load_catalog`` 的日志信息保持有用。
    """
    override = os.getenv("SPIRIT_BUNDLED_LOCALES", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            return candidate
        logger.warning(
            "SPIRIT_BUNDLED_LOCALES 指向非目录路径 (%s)；回退到内置/源码 locale 解析",
            override,
        )

    # spirit/agent/i18n.py -> spirit/agent/ -> spirit/ -> repo root
    source_dir = Path(__file__).resolve().parent.parent.parent / "locales"
    if source_dir.is_dir():
        return source_dir

    for scheme in ("data", "purelib", "platlib"):
        raw = sysconfig.get_path(scheme)
        if not raw:
            continue
        candidate = Path(raw) / "locales"
        if candidate.is_dir():
            return candidate

    return source_dir


def _normalize_lang(value: Any) -> str:
    """把用户提供的语言值规范化为受支持的码。

    接受受支持码本身、常见别名（``chinese`` → ``zh``）、大小写不敏感的地区标签
    （``zh-CN`` → ``zh``）。未知值返回默认语言。
    """
    if not isinstance(value, str):
        return DEFAULT_LANGUAGE
    key = value.strip().lower()
    if not key:
        return DEFAULT_LANGUAGE
    if key in SUPPORTED_LANGUAGES:
        return key
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    base = key.split("-", 1)[0]
    if base in SUPPORTED_LANGUAGES:
        return base
    return DEFAULT_LANGUAGE


def _flatten_into(node: Any, prefix: str, out: Dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            _flatten_into(value, child_key, out)
    elif isinstance(node, str):
        out[prefix] = node
    # 非字符串、非字典的叶子被忽略 —— catalog 仅含文本。


def _load_catalog(lang: str) -> Dict[str, str]:
    """加载并展平一个 locale YAML 文件为点分 key 字典。按语言进程内缓存。"""
    with _catalog_lock:
        cached = _catalog_cache.get(lang)
        if cached is not None:
            return cached

    path = _locales_dir() / f"{lang}.yaml"
    if not path.is_file():
        logger.debug("i18n catalog 缺失：%s（路径 %s）", lang, path)
        with _catalog_lock:
            _catalog_cache[lang] = {}
        return {}

    try:
        import yaml
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("加载 i18n catalog 失败 %s: %s", path, exc)
        with _catalog_lock:
            _catalog_cache[lang] = {}
        return {}

    flat: Dict[str, str] = {}
    _flatten_into(raw, "", flat)
    with _catalog_lock:
        _catalog_cache[lang] = flat
    return flat


@lru_cache(maxsize=1)
def _config_language_cached() -> Optional[str]:
    """从 config.yaml 读一次 ``display.language``（进程内缓存）。

    缓存因为 ``t()`` 在热路径调用（每次审批提示、每次网关回复），逐次重读 YAML
    浪费。``reset_language_cache()`` 在运行期 config 变化时清除它。
    """
    try:
        from spirit.config import get_config_value
        lang = get_config_value("display.language")
        if lang:
            return _normalize_lang(lang)
    except Exception as exc:
        logger.debug("无法从 config 读取 display.language: %s", exc)
    return None


def reset_language_cache() -> None:
    """失效已缓存的语言解析与 catalog。运行期改了 ``display.language`` 后调用。"""
    _config_language_cached.cache_clear()
    with _catalog_lock:
        _catalog_cache.clear()


def get_language() -> str:
    """按 env > config > default 顺序解析活跃语言。"""
    env_lang = os.environ.get("SPIRIT_LANGUAGE")
    if env_lang:
        return _normalize_lang(env_lang)
    cfg_lang = _config_language_cached()
    if cfg_lang:
        return cfg_lang
    return DEFAULT_LANGUAGE


def t(key: str, lang: Optional[str] = None, **format_kwargs: Any) -> str:
    """把一个点分 key 翻译为活跃语言。

    Parameters
    ----------
    key
        catalog 的点分路径，如 ``"approval.choose_long"``。
    lang
        显式语言覆盖，优先于 env + config。
    **format_kwargs
        ``str.format`` 替换参数（``t("gateway.drain", count=3)`` 期望 catalog 条目
        含 ``{count}`` 占位符）。

    Returns
    -------
    翻译后的字符串；目标语言缺 key 时回退英文；英文也缺时返回裸 key。
    """
    target = _normalize_lang(lang) if lang else get_language()
    catalog = _load_catalog(target)
    value = catalog.get(key)

    if value is None and target != DEFAULT_LANGUAGE:
        # 回退英文，而非把 key 路径展示给用户。
        value = _load_catalog(DEFAULT_LANGUAGE).get(key)

    if value is None:
        # 最后手段：返回 key 本身。损坏的 catalog 不应崩溃，只是难看。
        logger.debug("i18n 未命中：key=%r lang=%r", key, target)
        value = key

    if format_kwargs:
        try:
            return value.format(**format_kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "i18n 格式化失败 key=%r lang=%r kwargs=%r: %s",
                key, target, format_kwargs, exc,
            )
            return value
    return value


__all__ = [
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "t",
    "get_language",
    "reset_language_cache",
]
