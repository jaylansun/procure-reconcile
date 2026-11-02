# UI 设计参考与本轮应用方向

记录日期：2026-10-16（Asia/Shanghai）。

本文记录设计来源、借鉴理由与本项目的应用边界。它不是本轮界面的运行验收报告；截图 QA、原生窗口检查及交互回归结果由主任务另行补充。

## 用户已确认的方向

使用浅色黑白灰作为主体，只加入少量蓝色强调。当前实现约定的颜色如下，均为本项目自行选择，不宣称与某个第三方产品逐值一致。

| 用途 | 当前实现色值 | 使用范围 |
| --- | --- | --- |
| 主色 | `#3264d6` | 主要操作、焦点与少量选中状态 |
| 页面背景 | `#f8f9fb` | 主工作区底色 |
| 侧栏背景 | `#fbfbfc` | 持续可见的导航区域 |
| 分隔与边框 | `#e5e7eb` | 面板、表单与导航区域之间的细分隔 |

保持服务连接、管理员登录和真实服务概况。尚未开放的业务模块继续明确显示建设中；不为填充布局添加模拟业务统计、虚构状态或假操作。

## 怎样区分参考证据

- **产品截图或官方示例**：用于参考导航关系、信息密度、工作区域与桌面窗口结构。宣传图可能使用特定版本、主题或示例数据，不能据此认定本项目具备相同功能。
- **浅色主题 token**：用于理解背景、文字、边框与交互颜色的职责。精确色值以相应源码或官方主题文档为依据，不从压缩截图中猜测。
- **共享样式入口或组件源码**：用于理解选中、悬停、焦点和布局规则；它不一定包含完整浅色色板。
- **本项目设计判断**：借鉴上述关系后自行设计，不复制第三方组件代码、图标、Logo、截图或其他素材到产品中，也不因为参考某个项目而引入其技术栈。

以下链接指向公开分支或官网，后续可能变化。本文保留原始入口，便于继续核对来源。

## 1. Twenty

- 仓库：[twentyhq/twenty](https://github.com/twentyhq/twenty)。
- 截图来源：[官方 README 的浅色封面](https://raw.githubusercontent.com/twentyhq/twenty/main/packages/twenty-website/public/images/readme/github-cover-light.webp)；[官方界面与布局介绍入口](https://github.com/twentyhq/twenty#everything-you-need)。封面和产品展示图用于观察整体关系，不作为本项目业务数据来源。
- 浅色 token 来源：[ThemeLight.ts](https://github.com/twentyhq/twenty/blob/main/packages/twenty-ui/src/theme/constants/ThemeLight.ts)。该文件将背景、文字、边框、强调色、图标和间距分开定义。
- 借鉴点：采用稳定侧栏、清晰的页面标题与紧凑属性区域；通过中性背景和细分隔建立层次，让内容本身成为视觉中心。
- 本项目应用：连接与会话信息按明确分组展示，减少大面积品牌底色和与当前操作无关的宣传元素。

## 2. Plane

- 仓库：[makeplane/plane](https://github.com/makeplane/plane)。
- 截图来源：[官方 README 的工作项界面](https://media.docs.plane.so/GitHub-readme/github-work-items.webp)；[完整截图入口](https://github.com/makeplane/plane#-screenshots)。用于参考侧栏、标题、操作与列表之间的关系。
- 样式来源：[Web 全局样式入口](https://github.com/makeplane/plane/blob/preview/apps/web/styles/globals.css)与[tailwind-config 样式入口](https://github.com/makeplane/plane/blob/preview/packages/tailwind-config/index.css)。当前后者从 `@makeplane/propel/styles` 引入主题 token，不能把入口文件中的少量局部颜色当成完整浅色主题，也不在本文推断其精确浅色色值。
- 借鉴点：全局导航保持稳定，页面局部操作就近放置；主要操作、筛选与信息列表具有明确优先级。
- 本项目应用：连接检查、登录和刷新概况分别靠近对应内容；页面标题与操作区收紧，避免所有说明都占据同等视觉权重。

## 3. shadcn/ui

- 仓库：[shadcn-ui/ui](https://github.com/shadcn-ui/ui)。
- 界面来源：[官方 Dashboard 示例](https://ui.shadcn.com/examples/dashboard)。这是可交互示例，不是本项目的运行截图。
- 主题来源：[官方 Theming 文档](https://ui.shadcn.com/docs/theming)。文档通过背景、前景、边框、输入、焦点环和侧栏等语义变量组织浅色与深色主题。
- 借鉴点：统一输入框、按钮、标签、错误提示与焦点样式，通过尺寸和语义保持一致，而非为每张卡片单独设计一套视觉语言。
- 本项目应用：保持中性表面和少量蓝色主操作；参考其分组与控件关系，但不照搬 Dashboard 中的收入、客户数和趋势示例，也不把引入该组件库列为本次改版前提。

## 4. GitButler

- 仓库：[gitbutlerapp/gitbutler](https://github.com/gitbutlerapp/gitbutler)。
- 截图来源：[官方 README 引用的浅色桌面预览](https://gitbutler-docs-images-public.s3.us-east-1.amazonaws.com/app-preview-light.png)。截图呈现紧凑顶栏、导航轨、次级列表与主任务区域；它是布局参考，不表示本项目需要复制其多列工作流。
- 样式来源：[SidebarRow.module.css](https://github.com/gitbutlerapp/gitbutler/blob/master/packages/ui-react/src/SidebarRow.module.css)与[共享交互变量](https://github.com/gitbutlerapp/gitbutler/blob/master/packages/ui-svelte/src/styles/core/variables.css)。两者用于参考行密度及悬停、选中和焦点状态；共享变量文件本身不等于完整浅色色板。
- 借鉴点：窗口框架与任务内容紧密相接，导航行有稳定尺寸和明确选中反馈，不依赖大幅标题或装饰占位。
- 本项目应用：保留中文图标加文字导航，缩小重复抬头和多余留白；把空间交给表单与真实服务信息。

## 5. Yaak

- 仓库：[mountain-loop/yaak](https://github.com/mountain-loop/yaak)；原 `yaakapp/app` 地址目前重定向至此。
- 截图来源：[官方 README 引用的桌面截图](https://yaak.app/static/screenshot.png)。该截图使用深色主题，用于参考紧凑顶栏、导航选中行及并列工作面，不能将它描述成浅色截图。
- 浅色 token 来源：[defaultThemes.ts 中的 defaultLightTheme](https://github.com/mountain-loop/yaak/blob/main/packages/theme/src/defaultThemes.ts)。浅色配置分别定义白色工作面、接近白色的侧栏，以及不同强度的正文与边框。
- 借鉴点：将环境与上下文放在紧凑位置，依靠细分隔组织区域；选中反馈集中在当前行，而不是让整块导航染上品牌色。
- 本项目应用：开发与正式环境持续清楚显示；本项目采用已确认的蓝色强调，不复制 Yaak 的品牌紫色或深色截图配色。

## 6. Beekeeper Studio

- 仓库：[beekeeper-studio/beekeeper-studio](https://github.com/beekeeper-studio/beekeeper-studio)。
- 截图来源：[官方 README 引用的桌面截图](https://user-images.githubusercontent.com/279769/203650152-4a34af1f-8a38-47cf-a273-d34d1c84feeb.png)。该截图使用深色主题，用于参考导航、工作面与底部状态区域的分工。
- 浅色 token 来源：[light/variables.scss](https://github.com/beekeeper-studio/beekeeper-studio/blob/master/apps/studio/src/assets/styles/themes/light/variables.scss)。浅色源码采用灰白背景与分层黑色文字，并分别定义标题栏、侧栏、标签及状态栏表面。
- 借鉴点：大部分空间留给持续工作的内容，状态信息放在稳定、易找的位置；利用边框和表面差异组织区域，减少相互竞争的大卡片。
- 本项目应用：服务环境、连接及账号状态保持稳定可查；概况采用属性行与适度分组，不引入数据库编辑器、查询标签或其他无关能力。

## 本轮取舍与后续 QA 边界

本轮沿用现有连接和登录能力，主要调整视觉层级、颜色、排版、控件一致性和桌面框架。六个参考分别提供业务工作台结构、任务布局、控件语义、桌面密度、环境上下文和状态组织方面的思路，不拼接其产品页面。

后续 QA 需要单独记录实际窗口与构建版本，并检查导航、输入焦点、连接错误、登录失败、会话退出、建设中页面及真实概况刷新。浏览器预览与原生桌面测试分别记录；本文不预先填写通过结论。

## 2026-10-16 实际检查记录

本轮已实现上述配色、紧凑导航、统一表单和状态提示，并替换桌面图标；没有引入新的 UI 框架或业务依赖。

- 浏览器预览检查了1280×850与980×720两种视口，均无横向溢出；小窗口按内容纵向滚动。默认窗口的工作台预览已完整显示，辅助文字至少12px。
- 工作台、连接设置及未开放业务页已检查；普通浏览器清楚显示预览说明，原生表单操作全部禁用，控制台未发现错误或警告。
- 在独立浏览器上下文注入一次性原生桥桩，检查修改地址后禁用登录、登录错误后清空密码与恢复快照、六位前导零字符串传递、会话有但概况未加载、刷新、退出，以及连接失败后的状态。桩未进入源码、构建产物或实际客户端，检查结束已关闭该上下文。
- `pnpm check:client`、`pnpm client:build`、`pnpm test:ui`（现有2项）与`pnpm check:rust`通过；桌面PNG/ICNS/ICO及Windows图标已通过Tauri CLI重生成，Rust编译使用了新图标。
- 浏览器状态检查不代表真实Tauri登录、系统凭据、macOS/Windows安装包验收；这些仍按原实施计划单独执行。本轮没有部署Docker或触发Jenkins。

本机工作台预览保存在`.local/ui/procure-reconcile-ui-20261016.png`，它包含明确的浏览器预览标识，没有模拟已连接或已登录数据。
