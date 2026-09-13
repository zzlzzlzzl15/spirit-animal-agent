# 🦊 SpiritFoxAnimator — 小灵狐 AI 动画组件

> 用纯 SVG + CSS 动画实现的"小灵狐"，完美匹配 Spirit Agent 桌宠窗口规格（192×208）

---

## 📦 交付文件

| 文件 | 用途 |
|------|------|
| `desktop-app/src/components/SpiritFoxAnimator.vue` | **可嵌入的 Vue 组件**（生产用） |
| `desktop-app/src/components/fox-animation/preview.html` | **独立预览文件**（双击即看） |

---

## 🎨 动画清单（共 10 种）

| # | 动画 | 周期 | 效果描述 |
|---|------|------|---------|
| 1 | 整体呼吸 | 3.2s | 身体上下浮动 ±3px + 轻微缩放 |
| 2 | 尾巴摇摆 | 2.4s | 大尾巴左右摇摆 -8°~+12° |
| 3 | 尾巴尖 | 2.4s | 尾巴尖独立小摆 +6° |
| 4 | 自动眨眼 | 4.5s | 每 4.5s 瞬时眨眼一次 |
| 5 | 腮红脉冲 | 2.6s | 脸颊红晕透明度+大小脉动 |
| 6 | 耳朵抖动 | 5s | 偶尔耳朵微抖 |
| 7 | 灵纹闪烁 | 2.8s | 额头菱形灵纹透明度+缩放（Spirit 主题） |
| 8 | 地面阴影 | 3.2s | 与呼吸同步的阴影变化 |
| 9 | 嘴角微笑 | 3.5s | 嘴角微微上扬 |
| 10 | 魔法粒子 | 4-5.5s | 6 个暖色粒子围绕小狐狸向上飘 |

---

## 🚀 三种集成方式

### 方式 1️⃣：直接预览（最快）

直接双击打开：

```
desktop-app/src/components/fox-animation/preview.html
```

会看到三种尺寸的预览：原始 192×208、默认窗口 144×156、放大版 256×277。
还可以用按钮单独测试每种效果（眨眼/摇尾/呼吸/灵纹）。

### 方式 2️⃣：替换 PetSprite（推荐）

修改 `desktop-app/src/App.vue`：

```vue
<template>
  <!-- 替换这一行：<PetSprite ... /> -->
  <SpiritFoxAnimator
    :scale="petStore.scale"
    :paused="isDragging"
    :show-particles="true"
    :enable-spirit-mark="true"
    @click="onSpriteClick"
    @contextmenu.prevent="onSpriteRightClick"
  />
</template>

<script setup lang="ts">
import SpiritFoxAnimator from './components/SpiritFoxAnimator.vue'
// ... 其他代码保持不变
</script>
```

### 方式 3️⃣：叠加为特效层（保留精灵图）

如果你想**保留**现有 spritesheet 动画，再叠加 Spirit 灵纹+粒子：

```vue
<template>
  <div class="pet-stack">
    <!-- 底层：现有精灵图 -->
    <PetSprite ... />

    <!-- 顶层：Spirit 灵纹 + 粒子 -->
    <SpiritFoxAnimator
      :scale="petStore.scale"
      :paused="isDragging"
      :show-particles="true"
      :enable-spirit-mark="true"
      :enable-blush="false"
      class="spirit-overlay"
    />
  </div>
</template>

<style>
.pet-stack {
  position: relative;
}
.spirit-overlay {
  position: absolute;
  top: 0;
  left: 0;
  pointer-events: none;
  /* 让下方精灵图处理点击事件 */
}
</style>
```

---

## ⚙️ Props API

| Prop | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `scale` | number | `0.75` | 缩放比例（与 PetSprite 一致） |
| `paused` | boolean | `false` | 是否暂停动画（拖拽时设为 true） |
| `showParticles` | boolean | `true` | 是否显示魔法粒子 |
| `enableBlush` | boolean | `true` | 是否显示腮红 |
| `enableSpiritMark` | boolean | `true` | 是否显示额头灵纹 |

---

## 🎯 与现有系统的兼容性

✅ **尺寸完全匹配**：`FRAME_W=192`、`FRAME_H=208`（与 `usePetAnimation.ts` 一致）
✅ **缩放比例一致**：默认 `scale=0.75`，实际 148×160（含 4px padding）
✅ **拖拽暂停**：传 `paused` 即可，组件内部用 `animation-play-state: paused`
✅ **事件透传**：支持 `@click`、`@contextmenu` 等鼠标事件
✅ **静态拖拽态**：paused 时自动渲染无动画版本（避免视觉跳变）
✅ **类型安全**：完整 TypeScript 类型定义

---

## 🎨 配色变量

如果你想调整风格，可以修改组件顶部的 `<defs>` 渐变：

| 元素 | 颜色 |
|------|------|
| 身体主色 | `#ff9b5e` → `#e8723c`（橙红渐变）|
| 白肚皮 | `#fff8ee` → `#ffe4c4`（暖白）|
| 尾巴 | 同身体 + 白色尾尖 |
| 耳朵内 | `#ffc8b5` → `#ff9b8e`（粉色）|
| 腮红 | `#ff8c8c`（粉红）|
| 灵纹 | `#fff5b8` / `#ffd97a`（金黄）|
| 粒子 | `#fff5b8` → `#ffb37a` → `#ff8c5a`（暖色光晕）|

---

## 💡 性能说明

- **0 依赖**：纯 SVG + CSS，无 JS 动画循环
- **GPU 加速**：使用 `transform` + `opacity`，由浏览器合成层处理
- **内存占用**：极小（~2KB 文本 vs 585KB spritesheet）
- **CPU 占用**：可忽略不计（CSS 动画由浏览器原生优化）

适合 7×24 小时常驻桌面的场景。

---

## 🔄 未来可扩展

- [ ] 添加更多状态（思考/说话/睡觉/吃饭）
- [ ] 响应 WebSocket 事件（任务完成时欢呼、失败时沮丧）
- [ ] 支持主题切换（圣诞/新年/暗黑模式）
- [ ] 与宠物状态联动（健康度低 → 灵纹变暗）

---

**作者**：Spirit Agent
**创建日期**：2026-09-12
**版本**：1.0.0