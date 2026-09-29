"""Provider 模块注册表 —— 移植自 Hermes ``providers/__init__.py``。

Provider profile 可存在于两处：

1. 内置插件：``<repo>/plugins/model-providers/<name>/``（随 Spirit 发布）
2. 用户插件：``~/.spirit/plugins/model-providers/<name>/``（每用户覆盖）

每个插件目录含：
  - ``__init__.py`` —— import 时调用 ``register_provider(profile)``
  - ``plugin.yaml`` —— 清单（name, kind: model-provider, version, description）

发现是**懒**的：首次调用 ``get_provider_profile()`` 或 ``list_providers()`` 才扫描
两处并 import 每个插件。用户插件在名字冲突时覆盖内置插件（last-writer-wins），
故第三方可替换任意内置 profile 而无需改仓库代码。

为向后兼容，``spirit/providers/<name>.py``（除 ``base.py`` / ``__init__.py``）仍会
经 ``pkgutil.iter_modules`` 被发现 —— 让树外用户在可编辑安装里丢一个单文件 profile
即可生效，无需插件目录结构。新 profile 应优先用插件布局。

用法::

    from spirit.providers import get_provider_profile
    profile = get_provider_profile("minimax")   # ProviderProfile 或 None
    profile = get_provider_profile("qwen")       # 查 name + aliases
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

from spirit.providers.base import OMIT_TEMPERATURE, ProviderProfile  # noqa: F401

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ProviderProfile] = {}
_ALIASES: dict[str, str] = {}
_discovered = False

# repo 根 ``plugins/model-providers/`` —— 发现时填充。
# __file__ = spirit/providers/__init__.py → parent^3 = repo 根（spirit-agent-main）
_BUNDLED_PLUGINS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "plugins" / "model-providers"
)


def register_provider(profile: ProviderProfile) -> None:
    """按 name 与 aliases 注册一个 provider profile。

    同名的后注册者替换先注册者 —— 故 ``~/.spirit/plugins/model-providers/`` 下的
    用户插件无需改仓库代码即可覆盖内置 profile。
    """
    _REGISTRY[profile.name] = profile
    for alias in profile.aliases:
        _ALIASES[alias] = profile.name


def get_provider_profile(name: str) -> ProviderProfile | None:
    """按 name 或 alias 查 provider profile。

    provider 无 profile 时返回 None（调用方回退到通用/字典逻辑）。
    """
    if not name:
        return None
    if not _discovered:
        _discover_providers()
    canonical = _ALIASES.get(name, name)
    return _REGISTRY.get(canonical)


def list_providers() -> list[ProviderProfile]:
    """返回所有已注册 profile（每个 canonical name 一个）。"""
    if not _discovered:
        _discover_providers()
    # 去重：_REGISTRY 是 canonical name；_ALIASES 指向同一批对象
    seen: set[int] = set()
    result: list[ProviderProfile] = []
    for profile in _REGISTRY.values():
        pid = id(profile)
        if pid not in seen:
            seen.add(pid)
            result.append(profile)
    return result


def _user_plugins_dir() -> Path | None:
    """返回 ``~/.spirit/plugins/model-providers/``（存在时）。"""
    try:
        from spirit.config import get_spirit_home

        d = get_spirit_home() / "plugins" / "model-providers"
        return d if d.is_dir() else None
    except Exception:
        return None


def _import_plugin_dir(plugin_dir: Path, source: str) -> None:
    """import 单个插件目录使其自注册。``source`` 仅用于日志（"bundled"/"user"）。"""
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return

    # 用唯一模块名，避免多个 HERMES_HOME/SPIRIT_HOME profile 互相 alias。
    # submodule_search_locations 让插件内的相对 import 生效。
    safe_name = plugin_dir.name.replace("-", "_")
    module_name = f"_spirit_provider_{source}_{safe_name}"

    if module_name in sys.modules:
        return  # 已导入

    try:
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(plugin_dir)]
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 单个插件失败绝不阻断其余
        logger.warning(
            "加载 %s provider 插件 %s 失败: %s", source, plugin_dir.name, exc
        )
        sys.modules.pop(module_name, None)


def _discover_providers() -> None:
    """import 每个 provider 插件以填充注册表。

    顺序：
      1. 内置插件 ``<repo>/plugins/model-providers/<name>/``
      2. 用户插件 ``~/.spirit/plugins/model-providers/<name>/``（可覆盖内置）
      3. 兼容单文件 ``spirit/providers/<name>.py``

    每步 import 其插件，插件在模块级调用 ``register_provider()``。后者胜。
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    # 1. 内置插件 —— 随 Spirit 发布。
    if _BUNDLED_PLUGINS_DIR.is_dir():
        for child in sorted(_BUNDLED_PLUGINS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            _import_plugin_dir(child, "bundled")

    # 2. 用户插件 —— ~/.spirit/plugins/model-providers/<name>/。
    #    可覆盖任意同名内置 profile（register_provider 里 last-writer-wins）。
    user_dir = _user_plugins_dir()
    if user_dir is not None:
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            _import_plugin_dir(child, "user")

    # 3. 兼容单文件 profile：spirit/providers/<name>.py。
    try:
        import pkgutil

        import spirit.providers as _pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(_pkg.__path__):
            if modname.startswith("_") or modname == "base":
                continue
            try:
                importlib.import_module(f"spirit.providers.{modname}")
            except ImportError as exc:
                logger.warning("导入兼容 provider 模块 %s 失败: %s", modname, exc)
    except Exception:
        pass


__all__ = [
    "ProviderProfile",
    "OMIT_TEMPERATURE",
    "register_provider",
    "get_provider_profile",
    "list_providers",
]
