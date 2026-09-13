<!--
  SpiritFoxAnimator.vue
  ───────────────────────────────────────────────────────────────
  小灵狐 AI 动画组件 — 用纯 SVG + CSS 动画实现的可爱小狐狸

  设计规格：192 × 208 像素（与现有精灵图 FRAME_W/FRAME_H 完全一致）
  默认窗口缩放：0.75（实际显示 144 × 156）

  特性：
  - 7 种 CSS 动画 + 6 个浮动粒子
  - 完全离线，无外部依赖
  - 支持 props 控制动画开关
  - 可作为现有 PetSprite.vue 的替换或叠加图层

  使用：
    <SpiritFoxAnimator :scale="0.75" :show-particles="true" />
-->
<template>
  <div
    class="spirit-fox-animator"
    :style="{ width: `${displayW}px`, height: `${displayH}px` }"
    :class="{ 'is-dragging': paused }"
  >
    <svg
      class="fox-svg"
      viewBox="0 0 192 208"
      xmlns="http://www.w3.org/2000/svg"
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <radialGradient id="fox-body-grad" cx="50%" cy="40%" r="60%">
          <stop offset="0%" stop-color="#ff9b5e"/>
          <stop offset="100%" stop-color="#e8723c"/>
        </radialGradient>
        <radialGradient id="fox-belly-grad" cx="50%" cy="50%" r="55%">
          <stop offset="0%" stop-color="#fff8ee"/>
          <stop offset="100%" stop-color="#ffe4c4"/>
        </radialGradient>
        <radialGradient id="fox-tail-grad" cx="60%" cy="40%" r="65%">
          <stop offset="0%" stop-color="#ff9b5e"/>
          <stop offset="80%" stop-color="#e8723c"/>
          <stop offset="100%" stop-color="#fff8ee"/>
        </radialGradient>
        <radialGradient id="fox-ear-inner" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#ffc8b5"/>
          <stop offset="100%" stop-color="#ff9b8e"/>
        </radialGradient>
        <radialGradient id="fox-mark-glow">
          <stop offset="0%" stop-color="#fff6c4" stop-opacity="0.9"/>
          <stop offset="50%" stop-color="#ffd97a" stop-opacity="0.4"/>
          <stop offset="100%" stop-color="#ffd97a" stop-opacity="0"/>
        </radialGradient>
        <radialGradient id="fox-particle-grad">
          <stop offset="0%" stop-color="#fff5b8"/>
          <stop offset="60%" stop-color="#ffb37a" stop-opacity="0.7"/>
          <stop offset="100%" stop-color="#ff8c5a" stop-opacity="0"/>
        </radialGradient>
      </defs>

      <!-- 地面阴影 -->
      <ellipse
        v-if="!paused"
        class="fox-shadow"
        cx="96" cy="195" rx="42" ry="5"
        fill="#000" opacity="0.25"
      />

      <!-- 身体组 -->
      <g v-if="!paused" class="fox-body-group">
        <!-- 尾巴 -->
        <g class="fox-tail">
          <path
            d="M 65 145 Q 35 130, 28 105 Q 22 80, 38 65 Q 55 55, 72 70 Q 85 85, 78 115 Q 72 138, 65 145 Z"
            fill="url(#fox-tail-grad)" stroke="#c45a2c" stroke-width="2"
          />
          <g class="fox-tail-tip">
            <ellipse cx="38" cy="78" rx="14" ry="18" fill="#fff8ee"/>
          </g>
        </g>

        <!-- 后爪 -->
        <ellipse cx="78"  cy="178" rx="14" ry="7" fill="#e8723c" stroke="#c45a2c" stroke-width="1.5"/>
        <ellipse cx="116" cy="178" rx="14" ry="7" fill="#e8723c" stroke="#c45a2c" stroke-width="1.5"/>

        <!-- 身体 -->
        <ellipse cx="96" cy="148" rx="46" ry="38" fill="url(#fox-body-grad)" stroke="#c45a2c" stroke-width="2"/>
        <ellipse cx="96" cy="155" rx="28" ry="26" fill="url(#fox-belly-grad)"/>

        <!-- 前爪 -->
        <ellipse cx="80"  cy="175" rx="10" ry="6" fill="#fff8ee" stroke="#c45a2c" stroke-width="1.5"/>
        <ellipse cx="112" cy="175" rx="10" ry="6" fill="#fff8ee" stroke="#c45a2c" stroke-width="1.5"/>
        <circle cx="80"  cy="175" r="3" fill="#ff9b8e" opacity="0.7"/>
        <circle cx="112" cy="175" r="3" fill="#ff9b8e" opacity="0.7"/>

        <!-- 头 -->
        <ellipse cx="96" cy="98" rx="48" ry="42" fill="url(#fox-body-grad)" stroke="#c45a2c" stroke-width="2"/>

        <!-- 脸颊白斑 -->
        <ellipse cx="72"  cy="110" rx="14" ry="10" fill="#fff8ee" opacity="0.7"/>
        <ellipse cx="120" cy="110" rx="14" ry="10" fill="#fff8ee" opacity="0.7"/>

        <!-- 腮红 -->
        <circle v-if="enableBlush" class="fox-blush" cx="72"  cy="118" r="6" fill="#ff8c8c" opacity="0.55"/>
        <circle v-if="enableBlush" class="fox-blush" cx="120" cy="118" r="6" fill="#ff8c8c" opacity="0.55"/>

        <!-- 耳朵 -->
        <g class="fox-ear-left">
          <path d="M 60 78 L 75 35 L 88 72 Z" fill="#e8723c" stroke="#c45a2c" stroke-width="2"/>
          <path d="M 68 70 L 78 45 L 84 68 Z" fill="url(#fox-ear-inner)"/>
        </g>
        <g class="fox-ear-right">
          <path d="M 132 78 L 117 35 L 104 72 Z" fill="#e8723c" stroke="#c45a2c" stroke-width="2"/>
          <path d="M 124 70 L 114 45 L 108 68 Z" fill="url(#fox-ear-inner)"/>
        </g>

        <!-- 灵纹光晕 -->
        <g v-if="enableSpiritMark">
          <circle class="fox-mark" cx="96" cy="78" r="12" fill="url(#fox-mark-glow)"/>
          <g class="fox-mark">
            <path d="M 96 70 L 100 78 L 96 86 L 92 78 Z" fill="#fff5b8" stroke="#ffd97a" stroke-width="0.8"/>
            <circle cx="96" cy="78" r="1.5" fill="#fff"/>
          </g>
        </g>

        <!-- 眼睛 -->
        <g class="fox-eye">
          <ellipse cx="78"  cy="105" rx="6" ry="8" fill="#1a1a2e"/>
          <ellipse cx="118" cy="105" rx="6" ry="8" fill="#1a1a2e"/>
          <circle cx="80"  cy="102" r="2" fill="#fff"/>
          <circle cx="120" cy="102" r="2" fill="#fff"/>
          <circle cx="77"  cy="108" r="1" fill="#fff" opacity="0.6"/>
          <circle cx="117" cy="108" r="1" fill="#fff" opacity="0.6"/>
        </g>

        <!-- 鼻子 -->
        <ellipse cx="96" cy="120" rx="4" ry="3" fill="#3a1f15"/>
        <circle cx="95" cy="119" r="1" fill="#fff" opacity="0.6"/>

        <!-- 嘴 -->
        <g class="fox-mouth">
          <path d="M 96 124 L 96 130" stroke="#3a1f15" stroke-width="1.8" stroke-linecap="round" fill="none"/>
          <path d="M 88 130 Q 92 136, 96 130 Q 100 136, 104 130" stroke="#3a1f15" stroke-width="1.8" stroke-linecap="round" fill="none"/>
        </g>

        <!-- 须 -->
        <line x1="68" y1="124" x2="58" y2="122" stroke="#c45a2c" stroke-width="0.8" opacity="0.6"/>
        <line x1="68" y1="128" x2="58" y2="130" stroke="#c45a2c" stroke-width="0.8" opacity="0.6"/>
        <line x1="124" y1="124" x2="134" y2="122" stroke="#c45a2c" stroke-width="0.8" opacity="0.6"/>
        <line x1="124" y1="128" x2="134" y2="130" stroke="#c45a2c" stroke-width="0.8" opacity="0.6"/>
      </g>

      <!-- 静态拖拽态：保留核心外形 -->
      <g v-else>
        <!-- 尾巴 -->
        <path
          d="M 65 145 Q 35 130, 28 105 Q 22 80, 38 65 Q 55 55, 72 70 Q 85 85, 78 115 Q 72 138, 65 145 Z"
          fill="url(#fox-tail-grad)" stroke="#c45a2c" stroke-width="2"
        />
        <ellipse cx="38" cy="78" rx="14" ry="18" fill="#fff8ee"/>
        <!-- 身体 -->
        <ellipse cx="78"  cy="178" rx="14" ry="7" fill="#e8723c" stroke="#c45a2c" stroke-width="1.5"/>
        <ellipse cx="116" cy="178" rx="14" ry="7" fill="#e8723c" stroke="#c45a2c" stroke-width="1.5"/>
        <ellipse cx="96" cy="148" rx="46" ry="38" fill="url(#fox-body-grad)" stroke="#c45a2c" stroke-width="2"/>
        <ellipse cx="96" cy="155" rx="28" ry="26" fill="url(#fox-belly-grad)"/>
        <ellipse cx="80"  cy="175" rx="10" ry="6" fill="#fff8ee" stroke="#c45a2c" stroke-width="1.5"/>
        <ellipse cx="112" cy="175" rx="10" ry="6" fill="#fff8ee" stroke="#c45a2c" stroke-width="1.5"/>
        <ellipse cx="96" cy="98" rx="48" ry="42" fill="url(#fox-body-grad)" stroke="#c45a2c" stroke-width="2"/>
        <ellipse cx="72"  cy="110" rx="14" ry="10" fill="#fff8ee" opacity="0.7"/>
        <ellipse cx="120" cy="110" rx="14" ry="10" fill="#fff8ee" opacity="0.7"/>
        <path d="M 60 78 L 75 35 L 88 72 Z" fill="#e8723c" stroke="#c45a2c" stroke-width="2"/>
        <path d="M 132 78 L 117 35 L 104 72 Z" fill="#e8723c" stroke="#c45a2c" stroke-width="2"/>
        <ellipse cx="78"  cy="105" rx="6" ry="8" fill="#1a1a2e"/>
        <ellipse cx="118" cy="105" rx="6" ry="8" fill="#1a1a2e"/>
        <ellipse cx="96" cy="120" rx="4" ry="3" fill="#3a1f15"/>
        <path d="M 88 130 Q 92 136, 96 130 Q 100 136, 104 130" stroke="#3a1f15" stroke-width="1.8" stroke-linecap="round" fill="none"/>
      </g>

      <!-- 粒子层（独立浮动） -->
      <g v-if="showParticles && !paused" class="fox-particles">
        <circle
          v-for="p in particles" :key="p.id"
          class="particle"
          :class="`p${p.id}`"
          :cx="p.x" :cy="p.y" :r="p.r"
          fill="url(#fox-particle-grad)"
          :style="p.style"
        />
      </g>
    </svg>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface Props {
  /** 缩放比例，默认 0.75（与现有 PetSprite 一致） */
  scale?: number
  /** 是否暂停动画（拖拽时） */
  paused?: boolean
  /** 是否显示魔法粒子 */
  showParticles?: boolean
  /** 是否显示腮红 */
  enableBlush?: boolean
  /** 是否显示额头灵纹（Spirit 主题） */
  enableSpiritMark?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  scale: 0.75,
  paused: false,
  showParticles: true,
  enableBlush: true,
  enableSpiritMark: true,
})

// 原始帧尺寸（与 usePetAnimation.ts 保持一致）
const FRAME_W = 192
const FRAME_H = 208
const PADDING = 4

const displayW = computed(() => Math.round(FRAME_W * props.scale) + PADDING)
const displayH = computed(() => Math.round(FRAME_H * props.scale) + PADDING)

// 粒子配置（基于 viewBox 坐标 192×208）
const particles = [
  { id: 1, x: 40,  y: 60,  r: 2.5, style: { '--dx': '6px',  '--dy': '-25px', '--dx2': '-4px', '--dy2': '-50px' } as Record<string, string> },
  { id: 2, x: 150, y: 50,  r: 2,   style: { '--dx': '-8px', '--dy': '-20px', '--dx2': '6px',  '--dy2': '-45px' } as Record<string, string> },
  { id: 3, x: 160, y: 100, r: 2.5, style: { '--dx': '-10px','--dy': '-28px', '--dx2': '4px',  '--dy2': '-55px' } as Record<string, string> },
  { id: 4, x: 30,  y: 120, r: 2,   style: { '--dx': '8px',  '--dy': '-22px', '--dx2': '-6px', '--dy2': '-48px' } as Record<string, string> },
  { id: 5, x: 170, y: 150, r: 2,   style: { '--dx': '-6px', '--dy': '-18px', '--dx2': '4px',  '--dy2': '-42px' } as Record<string, string> },
  { id: 6, x: 22,  y: 160, r: 2.5, style: { '--dx': '10px', '--dy': '-24px', '--dx2': '-4px', '--dy2': '-52px' } as Record<string, string> },
]
</script>

<style scoped>
.spirit-fox-animator {
  position: relative;
  pointer-events: auto;
  overflow: hidden;
  border-radius: 6px;
}

.fox-svg {
  width: 100%;
  height: 100%;
  display: block;
  pointer-events: none;
}

/* ========== 动画定义 ========== */

/* 1. 整体呼吸 */
.fox-body-group {
  transform-origin: 96px 130px;
  animation: fox-breathe 3.2s ease-in-out infinite;
}
@keyframes fox-breathe {
  0%, 100% { transform: translateY(0)    scale(1); }
  50%      { transform: translateY(-3px) scale(1.015); }
}

/* 2. 尾巴摇摆 */
.fox-tail {
  transform-origin: 60px 140px;
  animation: fox-tail-wag 2.4s ease-in-out infinite;
}
@keyframes fox-tail-wag {
  0%, 100% { transform: rotate(-8deg); }
  50%      { transform: rotate(12deg); }
}

/* 3. 尾巴尖 */
.fox-tail-tip {
  transform-origin: 42px 158px;
  animation: fox-tail-tip 2.4s ease-in-out infinite;
}
@keyframes fox-tail-tip {
  0%, 100% { transform: rotate(0deg); }
  50%      { transform: rotate(6deg); }
}

/* 4. 眨眼 */
.fox-eye {
  transform-origin: center;
  transform-box: fill-box;
  animation: fox-blink 4.5s ease-in-out infinite;
}
@keyframes fox-blink {
  0%, 92%, 100% { transform: scaleY(1); }
  95%           { transform: scaleY(0.1); }
}

/* 5. 腮红 */
.fox-blush {
  transform-origin: center;
  transform-box: fill-box;
  animation: fox-blush 2.6s ease-in-out infinite;
}
@keyframes fox-blush {
  0%, 100% { opacity: 0.55; transform: scale(1); }
  50%      { opacity: 0.85; transform: scale(1.08); }
}

/* 6. 耳朵抖动 */
.fox-ear-left {
  transform-origin: 75px 70px;
  animation: fox-ear-l 5s ease-in-out infinite;
}
.fox-ear-right {
  transform-origin: 117px 70px;
  animation: fox-ear-r 5s ease-in-out infinite;
}
@keyframes fox-ear-l {
  0%, 88%, 100% { transform: rotate(0deg); }
  92%           { transform: rotate(-6deg); }
}
@keyframes fox-ear-r {
  0%, 88%, 100% { transform: rotate(0deg); }
  92%           { transform: rotate(6deg); }
}

/* 7. 灵纹闪烁 */
.fox-mark {
  transform-origin: center;
  transform-box: fill-box;
  animation: fox-mark 2.8s ease-in-out infinite;
}
@keyframes fox-mark {
  0%, 100% { opacity: 0.7; transform: scale(1); }
  50%      { opacity: 1;   transform: scale(1.12); }
}

/* 8. 阴影同步呼吸 */
.fox-shadow {
  transform-origin: center;
  transform-box: fill-box;
  animation: fox-shadow 3.2s ease-in-out infinite;
}
@keyframes fox-shadow {
  0%, 100% { opacity: 0.25; transform: scaleX(1); }
  50%      { opacity: 0.18; transform: scaleX(0.88); }
}

/* 9. 嘴角微笑 */
.fox-mouth {
  transform-origin: center;
  transform-box: fill-box;
  animation: fox-mouth 3.5s ease-in-out infinite;
}
@keyframes fox-mouth {
  0%, 100% { transform: scaleY(1); }
  50%      { transform: scaleY(1.15); }
}

/* 10. 粒子浮动 */
.particle {
  transform-origin: center;
  transform-box: fill-box;
  animation: fox-float 4s ease-in-out infinite;
  opacity: 0;
}
.particle.p1 { animation-delay: 0s;    animation-duration: 4.0s; }
.particle.p2 { animation-delay: -1.2s; animation-duration: 5.0s; }
.particle.p3 { animation-delay: -2.0s; animation-duration: 4.5s; }
.particle.p4 { animation-delay: -0.6s; animation-duration: 5.5s; }
.particle.p5 { animation-delay: -1.8s; animation-duration: 4.2s; }
.particle.p6 { animation-delay: -2.6s; animation-duration: 5.2s; }
@keyframes fox-float {
  0%   { transform: translate(0, 0)              scale(0.4); opacity: 0; }
  15%  { opacity: 1; }
  50%  { transform: translate(var(--dx, 8px), var(--dy, -20px))  scale(1); }
  85%  { opacity: 0.6; }
  100% { transform: translate(var(--dx2, 4px), var(--dy2, -45px)) scale(0.3); opacity: 0; }
}

/* 拖拽时暂停所有动画 */
.is-dragging .fox-body-group,
 .is-dragging .fox-tail,
 .is-dragging .fox-tail-tip,
 .is-dragging .fox-eye,
 .is-dragging .fox-blush,
 .is-dragging .fox-ear-left,
 .is-dragging .fox-ear-right,
 .is-dragging .fox-mark,
 .is-dragging .fox-shadow,
 .is-dragging .fox-mouth,
 .is-dragging .particle {
  animation-play-state: paused;
}
</style>