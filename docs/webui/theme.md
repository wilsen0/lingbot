# 主题 · Theme tokens

linling-webui 的视觉语言以"狐妖小红娘 · 情缘"为锚，落地为三层抽象：

1. **CSS variables** — 在 `packages/webui/frontend/src/theme/tokens.css` 定义
2. **Tailwind theme** — 在 `tailwind.css` 的 `@theme` 块映射上面的变量
3. **装饰组件 Deco-\*** — 消费这些 tokens 动画、绘图

## 颜色 · 双模式

| token | light · 月白 | dark · 暮紫 | 用途 |
|-------|--------------|-------------|-----|
| `--color-bg` | `#FAF6F1` 宣纸底 | `#1B1523` 夜色 | 页面底 |
| `--color-bg-veil` | `#FFFDFA` 月纱 | `#261B31` | 玻璃态卡片 |
| `--color-ink` | `#2B2A2F` 墨竹 | `#F5EDE8` 素绢 | 正文 |
| `--color-ink-soft` | `#6B6870` 远山灰 | `#B7ADB9` | 次要文字 |
| `--color-sorrow` | `#B8002D` 苦情红 | `#D44466` | 主强调 |
| `--color-thread` | `#E03A4A` 红线 | `#F06A7C` | 链接 / accent |
| `--color-bell` | `#E0A95A` 铃铛金 | `#F2C579` | 通知 / loader |
| `--color-petal` | `#F7C8D3` 花瓣粉 | `#9B5A73` | 装饰粒子 |
| `--color-jade` | `#6CA893` 灵玉青 | `#8DCBB3` | 成功 |
| `--color-alert` | `#C8442D` 警玉朱 | `#E06653` | 错误 |
| `--color-ash` | `#9A8C8C` 尘灰 | `#7A6C73` | 禁用 |

对比度：
- light 正文 13.4:1（WCAG AAA）
- dark 正文 11.8:1（AAA）
- sorrow / bg > 4.8:1（AA）

## 字体

| token | 栈 | 场景 |
|-------|----|------|
| `--font-display` | Ma Shan Zheng · Noto Serif SC · Songti SC | 大标题 · 手写楷体点缀 |
| `--font-serif` | Noto Serif SC | 副标题 |
| `--font-sans` | HarmonyOS Sans SC · PingFang SC · Noto Sans SC · system-ui | 正文 |
| `--font-mono` | JetBrains Mono · Noto Sans Mono CJK SC | 代码 · key |

Noto 系列自宿 woff2，`font-display: swap`；首屏仅 sans 常规体关键字，display 延后加载。

## 间距 / 圆角 / 阴影

- spacing: `--space-1..8` = 4/8/12/16/24/32/48/64 px
- radius: `--radius-sm/md/lg/xl` = 8/14/22/32 px
- 卡片默认 `--shadow-petal`；活跃态 `--shadow-bell`
- 玻璃态 utility `.glass` = 62% bg-veil + `backdrop-filter: blur(14px) saturate(120%)`

## 动效 tokens

| token | 描述 |
|-------|-----|
| `--motion-bell-swing` | 铃铛摆动 1.2s alternate |
| `--motion-petal-fall` | 花瓣下落 8-14s linear |
| `--motion-breeze-drift` | 雾层漂移 24s linear |
| `--motion-thread-draw` | 红线绘出 600ms cubic |
| `--motion-fade-in-up` | 列表项浮出 240ms |
| `--motion-sheet-rise` | bottom-sheet 上滑 320ms |
| `--motion-tap` | 按钮按压 120ms |

**Reduced-motion 硬性**：`@media (prefers-reduced-motion: reduce)` 下所有动画压到 0.001ms 并卸掉装饰层（`DecoPetalCanvas`/`DecoBreezeLayer`/`DecoBellAccent`）。

## 装饰开关

用户在「绳结」中切三档：

| 等级 | 花瓣 | 雾层 | 铃铛 |
|------|------|------|------|
| 尽兴 | 24 片同时 | 径向 + 漂移 | 可响 |
| 含蓄 | 8 片 | 静态径向 | 可响 |
| 静默 | 关闭 | 关闭 | 只显形 |

偏好写 `localStorage.linling.prefs.decor`，跨设备需用户手动同步。

## 触达规范

- 所有可点击元素 `min-height/width: 44px`
- focus ring：`--ring-sorrow`（1px sorrow + 3px 透明外环）
- 底部 tab 每格高度 56px + `pb-safe`
