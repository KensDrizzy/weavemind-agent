# WeaveMind 多模态输入使用指南

> 适用版本：多模态升级 Phase 1 + Phase 2

WeaveMind 支持把图片作为用户消息的一部分发送给 LLM，目前支持以下使用方式：

- `@image:路径` 引用本地图片
- `@clipboard` 读取剪贴板图片
- `F5` 快捷键一键插入剪贴板图片
- MCP 浏览器工具截图自动注入对话

视觉模型默认使用 **kimi-k2.7**（Moonshot OpenAI 兼容端点）。

---

## 一、前置配置

确保 `config.yaml` 中已启用多模态并配置 Moonshot：

```yaml
llm:
  provider: moonshot
  model: kimi-k2.7
  max_tokens: 8192
  temperature: 0

providers:
  moonshot:
    base_url: https://api.moonshot.cn/v1
    api_key_env: MOONSHOT_API_KEY
    default_model: kimi-k2.7

multimodal:
  enabled: true
  max_image_file_size: 20971520       # 单文件 20MB
  allowed_mime_types: ["image/png", "image/jpeg", "image/webp", "image/gif"]
  allowed_image_dirs: []              # 额外白名单目录
  clipboard:
    enabled: true
    timeout_seconds: 8
  image_pruning:
    keep_last_n_rounds: 1             # 只保留最近 1 轮图片实体

audit:
  enabled: true
  dir: .weavemind/audit
```

环境变量：

```bash
export MOONSHOT_API_KEY="your-key"
```

---

## 二、使用本地图片：`@image`

### 2.1 基本语法

在输入框中直接写：

```
分析这张截图 @image:./screenshot.png
```

支持的形式：

| 形式 | 示例 |
|------|------|
| 相对路径 | `@image:./assets/shot.png` |
| 绝对路径 | `@image:/Users/alice/Desktop/shot.png` |
| 带空格路径 | `@image:</Users/alice/my shots/shot.png>` |
| file 协议 | `@image:file:///Users/alice/Desktop/shot.png` |

### 2.2 多图输入

一条消息可附加多张图片：

```
对比这两张图 @image:./a.png 和 @image:./b.png
```

### 2.3 路径安全限制

图片路径必须位于以下范围内，否则会被拒绝：

1. 当前项目根目录（`git` 仓库根或运行目录）
2. `multimodal.allowed_image_dirs` 中配置的白名单目录

禁止通过 `..` 读取项目外部文件，例如 `@image:../../../etc/passwd` 会报错。

---

## 三、使用剪贴板图片

### 3.1 命令式：`@clipboard`

把图片复制到剪贴板，然后输入：

```
@clipboard 请描述这张图片
```

提交时会实时读取剪贴板。**注意**：如果在复制图片后、按回车前又复制了其他内容，提交时会读取到最新剪贴板内容。

### 3.2 快捷键：F5（推荐）

1. 复制图片到剪贴板
2. 在 WeaveMind 输入框按 `F5`
3. 输入框自动插入 `@clipboard` 标记
4. 按回车提交

**优点**：`F5` 按下时立即捕获剪贴板图片并预缓存，避免提交前剪贴板被覆盖。

首次使用会提示：

```
提示：图片将上传至当前 LLM provider。
```

### 3.3 跨平台支持

| 平台 | 实现方式 |
|------|---------|
| macOS | AppleScript 读取剪贴板 PNG/TIFF |
| Linux | Pillow `ImageGrab.grabclipboard()` |
| Windows | Pillow `ImageGrab.grabclipboard()` |

---

## 四、MCP 浏览器截图

如果启用了 MCP Chrome 工具，浏览器操作返回的截图会自动注入对话。

典型流程：

```
打开 https://example.com 并截图分析
```

Agent 会调用 `browser_navigate` 等工具，工具返回的 screenshot image 会被自动转成 `image_url` block 追加到对话，模型可以直接“看到”页面内容。

---

## 五、实际示例

### 示例 1：分析代码截图

```
这段代码有 bug 吗 @image:./code.png
```

### 示例 2：UI 走查

```
这个按钮位置是否合理 @image:./ui.png
```

### 示例 3：对比两张设计稿

```
左边和右边哪个布局更紧凑 @image:./a.png @image:./b.png
```

### 示例 4：结合浏览器截图

```
访问 https://news.ycombinator.com 并总结首页内容
```

---

## 六、图片预处理说明

发送给 LLM 前，图片会经过自动预处理：

1. **尺寸限制**：最大边不超过 2000 像素
2. **大小限制**：base64 编码后不超过 5MB
3. **透明通道**：RGBA 图片会被白底 flatten 成 RGB
4. **压缩策略**：超大图会逐级降低 JPEG 质量（85% → 25%）

终端会显示预处理后的信息：

```
[已附加图片: image/png, base64≈123456 bytes]
```

---

## 七、历史图片裁剪

为避免长对话中 base64 撑爆上下文，系统默认只保留**最近 1 轮**图片实体。更早的图片会被替换为占位文本：

```
[图片已省略，参见上文描述]
```

可通过 `multimodal.image_pruning.keep_last_n_rounds` 调整保留轮数。

---

## 八、审计与隐私

### 8.1 审计日志

每次图片加载（本地、剪贴板、MCP）都会写入：

```
.weavemind/audit/audit-YYYY-MM-DD.jsonl
```

记录内容包括：时间戳、来源、MIME 类型、大小、是否成功、错误信息。

### 8.2 隐私提示

- 图片会通过 Moonshot API 上传到 LLM provider
- 不要上传包含密码、身份证、银行卡等敏感信息的截图
- 剪贴板可能包含临时敏感内容，请确认后再按 `F5`

---

## 九、常见问题

### Q1: 提示“当前模型不支持图片输入”

请检查 `config.yaml` 是否使用 vision 模型，例如：

```yaml
llm:
  provider: moonshot
  model: kimi-k2.7
```

### Q2: `@image` 提示“图片路径不在允许范围内”

图片必须放在项目目录内，或配置 `multimodal.allowed_image_dirs`。

### Q3: F5 没反应

- macOS 需确保 WeaveMind 运行在图形会话下（AppleScript 需要访问剪贴板）
- Linux/Windows 需安装 Pillow

### Q4: 剪贴板图片提交时变了

使用 `F5` 热键而非手动输入 `@clipboard`，`F5` 会预缓存图片。

### Q5: MCP 截图没有注入

确保 MCP Chrome Server 已连接：

```
/browser status
```

---

## 十、快捷键速查

| 快捷键 | 功能 |
|--------|------|
| `F5` | 捕获剪贴板图片并插入 `@clipboard` |
| `Ctrl+O` | 展开/收起流式详情 |
| `Ctrl+C` 连按两次 | 退出 REPL |
