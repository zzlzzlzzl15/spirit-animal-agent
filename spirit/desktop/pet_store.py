"""Pet 精灵图存储 — 安装/切换/列出/热加载宠物主题。

借鉴 Hermes agent/pet/store.py 设计：
- 宠物存储在 ~/.spirit/pets/<slug>/ 目录
- 每个宠物包含 pet.json + spritesheet.{png,webp}
- 支持安装/卸载/切换/重命名
- 支持从本地文件注册新宠物
- 主题热替换：前端通过 WebSocket 获取当前宠物信息

目录结构：
    ~/.spirit/pets/
    ├── spirit-fox/          # 默认灵狐
    │   ├── pet.json         # {id, displayName, description, spritesheetPath}
    │   └── spritesheet.png  # 8col × 9row 精灵图
    ├── cyber-cat/           # 赛博猫（可 AI 生成）
    │   ├── pet.json
    │   └── spritesheet.webp
    └── ...
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstalledPet:
    """磁盘上已安装的宠物。"""

    slug: str
    display_name: str
    description: str
    directory: Path
    spritesheet: Path

    @property
    def exists(self) -> bool:
        """精灵图文件是否存在。"""
        return self.spritesheet.is_file()


class PetStoreError(RuntimeError):
    """宠物存储操作错误。"""


# ---------------------------------------------------------------------------
# 路径管理
# ---------------------------------------------------------------------------

def _spirit_home() -> Path:
    """返回 Spirit 主目录（~/.spirit）。"""
    import os
    home = os.environ.get("SPIRIT_HOME", "")
    if home:
        return Path(home)
    return Path.home() / ".spirit"


def pets_dir() -> Path:
    """返回宠物存储目录（按需创建）。"""
    path = _spirit_home() / "pets"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _safe_slug(slug: str) -> str:
    """规范化 slug，防止路径穿越。

    去除路径分隔符，拒绝 . / .. 等特殊值。
    """
    segment = Path(str(slug).strip()).name
    if segment in ("", ".", ".."):
        return ""
    return segment


def _read_pet_json(directory: Path) -> dict:
    """读取宠物目录下的 pet.json。"""
    pet_json = directory / "pet.json"
    if not pet_json.is_file():
        return {}
    try:
        return json.loads(pet_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("无法读取 pet.json (%s): %s", directory, exc)
        return {}


def _resolve_spritesheet(directory: Path, meta: dict) -> Path:
    """解析宠物的精灵图路径。

    优先使用 pet.json 中声明的 spritesheetPath，
    否则按约定探测 spritesheet.{webp,png}。
    """
    declared = str(meta.get("spritesheetPath", "") or "").strip()
    if declared:
        candidate = directory / declared
        if candidate.is_file():
            return candidate

    for name in ("spritesheet.webp", "spritesheet.png", "sprite.webp", "sprite.png"):
        candidate = directory / name
        if candidate.is_file():
            return candidate

    # 默认路径（即使不存在也返回，便于后续判断）
    return directory / "spritesheet.png"


def slugify(name: str) -> str:
    """将显示名称转换为文件系统安全的 slug。"""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "pet"


def unique_slug(name: str) -> str:
    """生成不冲突的 slug。"""
    base = slugify(name)
    slug = base
    counter = 2
    while (pets_dir() / slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def load_pet(slug: str) -> Optional[InstalledPet]:
    """加载指定 slug 的宠物信息。不存在返回 None。"""
    slug = _safe_slug(slug)
    if not slug:
        return None
    directory = pets_dir() / slug
    if not directory.is_dir():
        return None
    meta = _read_pet_json(directory)
    return InstalledPet(
        slug=slug,
        display_name=str(meta.get("displayName", "") or slug),
        description=str(meta.get("description", "") or ""),
        directory=directory,
        spritesheet=_resolve_spritesheet(directory, meta),
    )


def installed_pets() -> List[InstalledPet]:
    """列出所有已安装的宠物（按 slug 排序）。"""
    out: List[InstalledPet] = []
    pd = pets_dir()
    if not pd.is_dir():
        return out
    for child in sorted(pd.iterdir()):
        if not child.is_dir():
            continue
        pet = load_pet(child.name)
        if pet and pet.exists:
            out.append(pet)
    return out


def resolve_active_pet(configured_slug: Optional[str] = None) -> Optional[InstalledPet]:
    """解析当前应展示的宠物。

    优先级：
    1. 配置的 slug（如果已安装）
    2. 第一个已安装的宠物（字母序）
    3. None
    """
    if configured_slug:
        pet = load_pet(configured_slug.strip())
        if pet and pet.exists:
            return pet
    pets = installed_pets()
    return pets[0] if pets else None


def register_pet(
    spritesheet_source,
    *,
    slug: str = "",
    display_name: str = "",
    description: str = "",
) -> InstalledPet:
    """注册一个新宠物到存储中。

    spritesheet_source 可以是：
    - Path: 精灵图文件路径
    - bytes: 精灵图原始数据
    - PIL.Image: PIL 图像对象

    返回注册后的 InstalledPet。
    """
    if not slug:
        slug = unique_slug(display_name or "new-pet")
    else:
        slug = _safe_slug(slug)
        if not slug:
            raise PetStoreError("无效的宠物 slug")

    directory = pets_dir() / slug
    directory.mkdir(parents=True, exist_ok=True)
    sprite_path = directory / "spritesheet.png"

    # 写入精灵图
    try:
        _write_spritesheet(spritesheet_source, sprite_path)
    except Exception as exc:
        raise PetStoreError(f"无法写入精灵图: {exc}") from exc

    # 写入 pet.json
    meta = {
        "id": slug,
        "displayName": display_name or slug,
        "description": description or "",
        "spritesheetPath": sprite_path.name,
    }
    (directory / "pet.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    pet = load_pet(slug)
    if pet is None or not pet.exists:
        raise PetStoreError(f"注册宠物 '{slug}' 后精灵图不可用")
    return pet


def remove_pet(slug: str) -> bool:
    """删除已安装的宠物。返回是否实际删除了内容。"""
    slug = _safe_slug(slug)
    if not slug:
        return False
    directory = pets_dir() / slug
    if not directory.is_dir():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()


def rename_pet(slug: str, new_display_name: str) -> Optional[str]:
    """重命名宠物的显示名称。

    如果新名称产生不同的 slug，也会重命名目录。
    返回最终的 slug，失败返回 None。
    """
    slug = _safe_slug(slug)
    new_display_name = (new_display_name or "").strip()
    if not slug or not new_display_name:
        return None

    directory = pets_dir() / slug
    pet_json = directory / "pet.json"
    if not pet_json.is_file():
        return None

    try:
        meta = json.loads(pet_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}

    meta["displayName"] = new_display_name
    new_slug = slug

    # 尝试重命名目录
    desired = slugify(new_display_name)
    if desired and desired != slug and not (pets_dir() / desired).exists():
        try:
            directory.rename(pets_dir() / desired)
            directory = pets_dir() / desired
            pet_json = directory / "pet.json"
            new_slug = desired
            meta["id"] = new_slug
        except OSError:
            new_slug = slug  # 重命名失败，保留原 slug

    try:
        pet_json.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        return None

    return new_slug


def get_pet_info(slug: str) -> dict:
    """获取宠物的完整信息字典（用于 WebSocket 传输）。"""
    pet = load_pet(slug)
    if pet is None:
        return {}
    return {
        "slug": pet.slug,
        "displayName": pet.display_name,
        "description": pet.description,
        "spritesheetExists": pet.exists,
        "directory": str(pet.directory),
        "spritesheet": str(pet.spritesheet),
    }


def export_pet(slug: str) -> tuple:
    """将宠物打包为 zip 文件（用于分享/备份）。

    返回 (filename, bytes) 元组。
    """
    import io
    import zipfile

    slug = _safe_slug(slug)
    if not slug:
        raise PetStoreError("无效的宠物 slug")

    root = pets_dir()
    directory = root / slug
    if not directory.is_dir():
        raise PetStoreError(f"宠物 '{slug}' 未安装")

    name = directory.name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                archive.write(path, f"{name}/{path.name}")
    return f"{name}.zip", buf.getvalue()


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _write_spritesheet(source, dest: Path) -> None:
    """将精灵图源写入目标路径。

    source 可以是 Path、bytes 或 PIL.Image。
    """
    if isinstance(source, (bytes, bytearray)):
        dest.write_bytes(bytes(source))
        return

    if isinstance(source, (str, Path)):
        src_path = Path(source)
        if not src_path.is_file():
            raise PetStoreError(f"精灵图源文件不存在: {src_path}")
        shutil.copy2(src_path, dest)
        return

    # 假设是 PIL.Image
    try:
        from PIL import Image
        if isinstance(source, Image.Image):
            image = source.convert("RGBA")
            image.save(dest, format="PNG")
            return
    except ImportError:
        pass

    raise PetStoreError(f"不支持的精灵图源类型: {type(source)}")


__all__ = [
    "InstalledPet",
    "PetStoreError",
    "pets_dir",
    "load_pet",
    "installed_pets",
    "resolve_active_pet",
    "register_pet",
    "remove_pet",
    "rename_pet",
    "get_pet_info",
    "export_pet",
    "slugify",
    "unique_slug",
]
