---
name: web-access
description: |
  联网操作决策手册。当任务涉及搜索互联网、抓取网页、浏览器操作时加载此 Skill。
  包含工具选择策略、浏览器优先级链、站点经验和反模式。
version: "1.0.0"
author: WeaveMind
tags: [web, search, browser, cdp]
---

# Web Access Skill

## 浏览哲学

像人类一样浏览：明确目标 → 选择起点 → 过程校验 → 完成判断。

## 工具选择表

| 场景 | 工具 | 说明 |
|------|------|------|
| 搜索互联网信息 | `WebSearch` | 关键词搜索，返回摘要+链接 |
| 已知 URL，静态页面 | `WebFetch` | 直接抓取 Markdown，成本最低 |
| SPA/动态站点 | Chrome DevTools MCP | `navigate_page` + `take_snapshot` |
| 需要登录态 | Chrome shared 模式 | 先 `browser_connect`，再访问 |
| WebFetch 失败（空/防爬） | Chrome DevTools MCP | 自动 fallback，不要重复 WebFetch |

## 浏览器优先级链

```
WebFetch（最便宜）
  ↓ 失败/空内容/SPA
Chrome isolated 模式（无登录态）
  ↓ 需要登录
browser_connect → Chrome shared 模式（有登录态）
```

## Chrome DevTools 使用要点

1. **优先 `evaluate_script` 提取文本**，而非 `take_screenshot`
2. **`take_snapshot`** 用于理解页面结构（DOM 树），不要默认截图
3. **搜索结果页**：用 `evaluate_script` 提取标题+链接列表
4. **帖子详情页**：用 `evaluate_script` 提取正文文本
5. **最多访问 5 个详情页**，然后总结

## 登录态处理

1. 先用 isolated 模式尝试访问
2. 如果返回登录页/401/403 → `close_page` → `browser_connect` → 重新访问
3. `browser_connect` 失败 → 停止，告知用户开启 Chrome 远程调试
4. shared 模式下不要执行敏感操作（关注/删除/退出登录）

## 站点经验

### 小红书 (xiaohongshu.com)
- 强反爬，WebFetch 无法获取内容，**必须用 Chrome DevTools**
- 搜索页 URL: `https://www.xiaohongshu.com/search_result?keyword=...`
- 需要登录态才能看到完整搜索结果
- 用 `evaluate_script` 提取笔记标题和链接
- 点击笔记后用 `evaluate_script` 提取正文

### 微信公众号 (mp.weixin.qq.com)
- SPA 渲染，WebFetch 可能返回空内容
- 优先尝试 WebFetch，失败则用 Chrome DevTools
- 文章内容在 `#js_content` 元素中

### 语雀 (yuque.com)
- 需要登录态查看私有文档
- 公开文档可用 WebFetch
- 私有文档需要 Chrome shared 模式

### GitHub (github.com)
- 公开仓库/文件用 WebFetch
- 私有仓库需要 Chrome shared 模式
- API 优先：`https://api.github.com/repos/{owner}/{repo}/contents/{path}`

### 知乎 (zhihu.com)
- 懒加载，需要滚动触发
- 用 Chrome DevTools + `evaluate_script` 提取回答内容
- 不要反复滚动，提取首屏可见内容即可

### 掘金 (juejin.cn)
- SSR 友好，WebFetch 通常可用
- 文章内容在 `.article-content` 中

## 反模式（禁止）

- ❌ WebFetch 返回空内容后继续重试 WebFetch
- ❌ 默认用 `take_screenshot` 代替 `take_snapshot`/`evaluate_script`
- ❌ 反复滚动加载更多内容（最多滚动 2 次）
- ❌ 在 shared 模式下执行用户未要求的写操作
- ❌ 用 Bash + curl/wget 代替 WebSearch/WebFetch
- ❌ 对同一页面重复截图/快照
