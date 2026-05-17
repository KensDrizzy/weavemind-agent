# 小红书 (xiaohongshu.com)

## 特征
- 强反爬，WebFetch 完全无法获取内容
- SPA 渲染，所有内容通过 JS 动态加载
- 需要登录态才能看到完整搜索结果和笔记内容

## 推荐工具链
Chrome DevTools MCP（shared 模式）

## 搜索流程
1. `navigate_page` → `https://www.xiaohongshu.com/search_result?keyword={关键词}&source=web_search_result_notes`
2. `evaluate_script` 提取搜索结果列表：
```javascript
() => {
  const items = document.querySelectorAll('.note-item, [data-note-id], .feeds-page .note-item');
  return Array.from(items).slice(0, 10).map(el => ({
    title: (el.querySelector('.title, .note-title') || el).textContent.trim().slice(0, 100),
    link: (el.querySelector('a') || el.closest('a'))?.href || ''
  }));
}
```

## 内容提取
进入笔记详情页后：
```javascript
() => {
  const title = document.querySelector('#detail-title, .title')?.textContent || '';
  const content = document.querySelector('#detail-desc, .content, .note-text')?.textContent || '';
  return { title, content: content.slice(0, 3000) };
}
```

## 已知陷阱
- 搜索结果页需要等待加载（用 `wait_for` 或延迟）
- 笔记内容可能在弹窗中（需要点击笔记卡片触发）
- 不要尝试滚动加载更多，首屏结果足够
