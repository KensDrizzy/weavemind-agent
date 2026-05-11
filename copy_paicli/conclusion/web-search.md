# WeaveMindAgent 联网搜索升级方案

## 一、PaiCLI 联网搜索实现分析

### 1.1 整体架构

PaiCLI 的联网模块放在 `com.paicli.web` 包下，共 8 个类，核心设计决策：

1. **搜索和抓取是两个独立工具**：web_search（"我不知道去哪找"）和 web_fetch（"我知道 URL，帮我把内容拿回来"）。Agent 根据用户意图自主选择，也可先搜再抓（搜到 URL 后再 fetch 详情）。
2. **搜索引擎做了 Provider 抽象**：三个实现各有特点，通过工厂模式自动选择。

架构依赖关系：

```
┌─────────────────────────────────────────────────────┐
│                  ToolRegistry                        │
│                                                     │
│  ┌─────────────────┐    ┌──────────────────────┐    │
│  │  web_search 工具 │    │  web_fetch 工具       │    │
│  └─────────┬───────┘    └──────────┬───────────┘    │
│            │                       │                │
│            ▼                       ▼                │
│  ┌─────────────────┐    ┌──────────────────────┐    │
│  │ SearchProvider   │    │  WebFetcher          │    │
│  │ (策略模式抽象)   │    │  (HTTP抓取+正文提取) │    │
│  │                 │    │                      │    │
│  │ ┌─ZhipuProvider │    │  ┌─NetworkPolicy     │    │
│  │ ├─SerpApiProvider│    │  │  (SSRF防护+限流) │    │
│  │ └─SearXNGProvider│    │  └─HtmlExtractor    │    │
│  │                 │    │    (噪声清理+正文提取│    │
│  └─────────────────┘    │    +Markdown转换)    │    │
│                         └──────────────────────┘    │
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │  SearchProviderFactory                       │    │
│  │  (工厂模式：环境变量→自动选择Provider)       │    │
│  └─────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘
```

### 1.2 SearchProvider 接口设计

```java
public interface SearchProvider {
    String name();                       // provider 名称
    boolean isReady();                   // 是否可用（API Key 是否配好）
    String unavailableHint();            // 不可用时的友好提示
    List<SearchResult> search(String query, int topK) throws IOException;
}
```

**关键设计点**：`isReady()` + `unavailableHint()` 是防御式设计。用户可能还没配 API Key 就开始用，这时候不是抛异常让程序崩掉，而是友好提示"请配置 XXX"。

### 1.3 三个搜索引擎 Provider

| Provider | 优点 | 缺点 | 适用场景 |
|----------|------|------|----------|
| **智谱搜索** | 和 GLM_API_KEY 共用，零额外配置；0.01元/次 | 国内搜索为主 | 国内用户默认选择 |
| **SerpAPI** | 国际搜索能力强；Google 结果 | 需单独注册付费 | 海外用户/国际搜索 |
| **SearXNG** | 完全免费；聚合多引擎 | 需自己部署 Docker | 隐私优先/零成本 |

工厂自动选择逻辑：
```
显式指定 SEARCH_PROVIDER → 优先
GLM_API_KEY 已配 → zhipu
SERPAPI_KEY 已配 → serpapi
SEARXNG_URL 已配 → searxng
默认 → zhipu（占位）
```

### 1.4 WebFetch 抓取器

三步流程：HTTP 请求拿原始 HTML → 安全检查 → 正文提取转 Markdown

**HTTP 抓取关键参数**：
- 响应体上限 5MB（流式读取，每次 8KB，防 OOM）
- 30 秒整体超时
- 字符集从 Content-Type charset 获取，兜底 UTF-8

**网络安全策略（NetworkPolicy）**：
1. URL 安全检查：只允许 http/https，屏蔽 file://、ftp://、localhost、127.0.0.1、内网地址（防 SSRF）
2. 请求频率限制：60秒内最多30次（防 Agent 重试循环）

**HTML 正文提取（HtmlExtractor）**：四步流程：
1. 清理噪声标签（script、style、nav、aside、footer、header、form、iframe + ads/banner/sidebar/comment 关键词元素）
2. 找主语义容器（优先 `<article>`、`<main>`、`[role=main]`）
3. 打分兜底（文本长度 × (1 - 链接密度惩罚)，文本多链接少的元素更可能是正文）
4. 转 Markdown（h1-h6→标题、p→段落、a→链接、pre/code→代码块、table→表格）

已知边界：JS 渲染的 SPA 页面抓回来可能空白；Cloudflare 反爬页面抓回来是验证脚本。遇到空正文返回提示让 Agent 知道是已知边界，不要反复重试浪费 token。

### 1.5 工具注册和 Agent 集成

工具描述文本是给 LLM 看的，决定 LLM 什么时候用什么工具：
- web_search："搜索互联网，获取实时信息（最新版本、官方文档、技术资讯等）"
- web_fetch："抓取指定 URL，提取正文转 Markdown。适用静态/SSR 页面。JS 渲染或防爬站会返回空正文"

SearchProvider 和 WebFetcher 都是懒加载——第一次用到才创建实例。加了 synchronized 保证多线程只初始化一次。

---

## 二、主流 Agent 联网搜索方案对比

### 2.1 Claude Code

Claude Code 的联网搜索采用 **MCP（Model Context Protocol）** 架构：

- **不内置搜索工具**，而是通过 MCP Server 接入外部搜索能力
- 用户可配置 Firecrawl、Brave Search、SearXNG 等 MCP Server
- Agent 自动判断何时需要联网，调用对应的 MCP Tool
- 优点：极致解耦，用户自由选择搜索后端
- 缺点：配置门槛高，需要用户自己搭建 MCP Server

### 2.2 OpenAI Codex CLI

Codex 的联网搜索策略：

- 内置 `web_search` 工具，底层调用 OpenAI 的搜索 API
- 搜索结果直接注入 Agent 上下文
- 没有独立的 web_fetch，搜索结果已包含摘要
- 优点：开箱即用，无需额外配置
- 缺点：搜索质量依赖 OpenAI 搜索 API，不可替换

### 2.3 LangChain Agent

LangChain 提供了最丰富的搜索工具生态：

- **TavilySearchResults** — Tavily 专用（AI 优化的搜索 API）
- **SerpAPIWrapper** — Google 搜索封装
- **DuckDuckGoSearchResults** — 免费搜索
- **GoogleSearchAPIWrapper** — Google Custom Search
- **WebBaseLoader** — 网页抓取 + BeautifulSoup 提取
- 优点：工具丰富，即插即用
- 缺点：各工具接口不统一，切换成本高

### 2.4 Cursor / Aider

- **Cursor**：内置 web search，底层用 Perplexity API 或自建搜索
- **Aider**：不内置联网搜索，依赖用户手动提供上下文

### 2.5 对比总结

| 维度 | PaiCLI | Claude Code | Codex | LangChain | WeaveMind(当前) |
|------|--------|-------------|-------|-----------|-----------------|
| 搜索引擎 | 策略模式多Provider | MCP外挂 | OpenAI内置 | 多工具各自封装 | 仅Tavily |
| 网页抓取 | 独立web_fetch | MCP外挂 | 无 | WebBaseLoader | 简陋正则提取 |
| SSRF防护 | 有 | 无 | 无 | 无 | 无 |
| 限流 | 有(令牌桶) | 无 | 无 | 无 | 无 |
| HTML正文提取 | 四步算法 | 无 | 无 | BeautifulSoup | 正则去标签 |
| Provider切换 | 工厂模式 | MCP配置 | 不可切换 | 换工具类 | 不可切换 |
| 懒加载 | 有 | MCP自动 | 有 | 无 | 无 |
| SPA/反爬处理 | 提示不重试 | MCP处理 | 无 | 无 | 无 |

**WeaveMind 当前问题**：
1. WebSearch 只支持 Tavily，无法切换搜索引擎
2. WebFetch 用正则去标签，噪声太多（导航栏、广告、页脚全混在一起）
3. 没有 SSRF 防护，Agent 可以抓取 localhost 等内网地址
4. 没有限流，Agent 可能陷入重试循环
5. 没有 Provider 抽象，换搜索引擎要改代码
6. 没有懒加载，每次启动都初始化网络组件

---

## 三、WeaveMind Python 实现方案

### 3.1 目标架构

参照 PaiCLI 的策略模式 + 工厂模式，将 WeaveMind 的联网模块重构为：

```
tools/builtin/
├── web_search.py       # WebSearchTool（重写）
└── web_fetch.py        # WebFetchTool（重写）

web/
├── __init__.py
├── providers/          # 搜索引擎 Provider
│   ├── __init__.py
│   ├── base.py         # SearchProvider 抽象基类
│   ├── tavily.py       # TavilyProvider（保留）
│   ├── searxng.py      # SearXNGProvider（新增）
│   └── factory.py      # SearchProviderFactory
├── fetcher/            # 网页抓取
│   ├── __init__.py
│   ├── fetcher.py      # WebFetcher（HTTP抓取）
│   ├── policy.py       # NetworkPolicy（SSRF防护+限流）
│   └── extractor.py    # HtmlExtractor（正文提取）
└── models.py           # SearchResult 数据模型
```

### 3.2 SearchProvider 抽象基类（web/providers/base.py）

```python
from abc import ABC, abstractmethod
from typing import List, Optional
from web.models import SearchResult


class SearchProvider(ABC):
    """搜索引擎 Provider 抽象基类。
    
    关键设计：
    - is_ready() + unavailable_hint() 是防御式设计
    - 用户没配 API Key 时不会崩溃，而是友好提示
    """
    
    @abstractmethod
    def name(self) -> str:
        """Provider 名称。"""
        ...
    
    @abstractmethod
    def is_ready(self) -> bool:
        """是否可用（API Key 是否配好）。"""
        ...
    
    @abstractmethod
    def unavailable_hint(self) -> str:
        """不可用时的友好提示。"""
        ...
    
    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        """执行搜索，返回结果列表。"""
        ...
```

### 3.3 TavilyProvider（web/providers/tavily.py）

```python
import os
from typing import List
from web.providers.base import SearchProvider
from web.models import SearchResult


class TavilyProvider(SearchProvider):
    """Tavily 搜索引擎 Provider。"""
    
    def name(self) -> str:
        return "tavily"
    
    def is_ready(self) -> bool:
        return bool(os.environ.get("TAVILY_API_KEY"))
    
    def unavailable_hint(self) -> str:
        return "WebSearch 不可用：请设置环境变量 TAVILY_API_KEY（https://tavily.com 获取）"
    
    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        from tavily import TavilyClient
        
        api_key = os.environ["TAVILY_API_KEY"]
        client = TavilyClient(api_key=api_key)
        response = client.search(query, max_results=top_k)
        
        results = []
        for item in response.get("results", []):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
            ))
        return results
```

### 3.4 SearXNGProvider（web/providers/searxng.py）

```python
import os
from typing import List
import httpx
from web.providers.base import SearchProvider
from web.models import SearchResult


class SearXNGProvider(SearchProvider):
    """SearXNG 搜索引擎 Provider（自部署，免费）。"""
    
    def name(self) -> str:
        return "searxng"
    
    def is_ready(self) -> bool:
        return bool(os.environ.get("SEARXNG_URL"))
    
    def unavailable_hint(self) -> str:
        return (
            "WebSearch 不可用：请部署 SearXNG 并设置 SEARXNG_URL\n"
            "  docker run --rm -p 8888:8888 searxng/searxng\n"
            "  export SEARXNG_URL=http://localhost:8888"
        )
    
    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        base_url = os.environ["SEARXNG_URL"].rstrip("/")
        resp = httpx.get(
            f"{base_url}/search",
            params={"q": query, "format": "json", "categories": "general"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        
        results = []
        for item in data.get("results", [])[:top_k]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
            ))
        return results
```

### 3.5 SearchProviderFactory（web/providers/factory.py）

```python
import os
from typing import Optional
from web.providers.base import SearchProvider


class SearchProviderFactory:
    """搜索引擎工厂：根据环境变量自动选择 Provider。"""
    
    @staticmethod
    def create(provider_name: Optional[str] = None) -> SearchProvider:
        """创建 Provider 实例。
        
        选择逻辑：
        1. 显式指定 provider_name → 优先
        2. TAVILY_API_KEY 已配 → tavily
        3. SEARXNG_URL 已配 → searxng
        4. 默认 → tavily（占位）
        """
        if provider_name:
            return SearchProviderFactory._create_by_name(provider_name)
        
        # 自动检测
        from web.providers.tavily import TavilyProvider
        from web.providers.searxng import SearXNGProvider
        
        providers = [TavilyProvider(), SearXNGProvider()]
        for p in providers:
            if p.is_ready():
                return p
        
        # 默认返回 Tavily（即使没配 Key，调用时会友好提示）
        return TavilyProvider()
    
    @staticmethod
    def _create_by_name(name: str) -> SearchProvider:
        name = name.lower().strip()
        if name == "tavily":
            from web.providers.tavily import TavilyProvider
            return TavilyProvider()
        elif name == "searxng":
            from web.providers.searxng import SearXNGProvider
            return SearXNGProvider()
        else:
            raise ValueError(f"未知的搜索引擎: {name}（支持: tavily, searxng）")
```

### 3.6 NetworkPolicy（web/fetcher/policy.py）

```python
import time
from urllib.parse import urlparse
import socket
from typing import Optional


class NetworkPolicy:
    """网络安全策略：SSRF 防护 + 限流。"""
    
    def __init__(self, rate_limit: int = 30, rate_window: int = 60):
        self._rate_limit = rate_limit
        self._rate_window = rate_window
        self._timestamps: list[float] = []
    
    def check_url(self, url: str) -> Optional[str]:
        """检查 URL 安全性，返回 None 表示通过，返回字符串表示拒绝原因。"""
        parsed = urlparse(url)
        
        # 只允许 http/https
        if parsed.scheme not in ("http", "https"):
            return f"禁止访问 {parsed.scheme}:// 协议（仅支持 http/https）"
        
        # 检查 host
        host = parsed.hostname or ""
        return self._check_host(host)
    
    def _check_host(self, host: str) -> Optional[str]:
        """检查 host 是否安全。"""
        lower = host.lower()
        
        # 屏蔽 localhost
        if lower in ("localhost", "127.0.0.1", "::1"):
            return "禁止访问 localhost"
        
        # 屏蔽内网地址前缀
        if lower.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.",
                            "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                            "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                            "172.29.", "172.30.", "172.31.")):
            return "禁止访问内网地址"
        
        # DNS 解析检查
        try:
            addrs = socket.getaddrinfo(host, None)
            for _, _, _, _, sockaddr in addrs:
                ip = sockaddr[0]
                if ip in ("127.0.0.1", "::1"):
                    return "禁止访问环回地址"
                # 检查解析后的 IP 是否是内网
                if any(ip.startswith(prefix) for prefix in 
                       ("192.168.", "10.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                        "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                        "172.29.", "172.30.", "172.31.")):
                    return "禁止访问内网地址"
        except socket.gaierror:
            pass  # DNS 解析失败不阻断，交给后续 HTTP 请求处理
        
        return None
    
    def check_rate(self) -> Optional[str]:
        """检查请求频率，返回 None 表示通过，返回字符串表示超限。"""
        now = time.time()
        # 清理过期记录
        self._timestamps = [t for t in self._timestamps if now - t < self._rate_window]
        
        if len(self._timestamps) >= self._rate_limit:
            return f"请求过于频繁（{self._rate_window}秒内最多{self._rate_limit}次）"
        
        self._timestamps.append(now)
        return None
```

### 3.7 HtmlExtractor（web/fetcher/extractor.py）

```python
from typing import Optional
from bs4 import BeautifulSoup, Tag


class HtmlExtractor:
    """HTML 正文提取器：噪声清理 + 语义定位 + 评分兜底 + Markdown 转换。"""
    
    # 需要清理的噪声标签
    NOISE_TAGS = {"script", "style", "nav", "aside", "footer", "header", 
                  "form", "iframe", "noscript", "svg", "canvas"}
    
    # 噪声关键词（class/id 中包含这些词的元素会被清理）
    NOISE_KEYWORDS = {"ads", "ad-", "banner", "sidebar", "comment", "widget",
                      "footer", "header", "nav", "menu", "breadcrumb"}
    
    def extract(self, html: str) -> str:
        """从 HTML 提取正文，返回 Markdown。"""
        soup = BeautifulSoup(html, "html.parser")
        
        # Step 1: 清理噪声标签
        self._remove_noise(soup)
        
        # Step 2: 找主语义容器
        main = self._find_main_content(soup)
        
        # Step 3: 如果没找到语义容器，用打分兜底
        if main is None:
            main = self._score_and_pick(soup)
        
        if main is None:
            return "未提取到正文。可能是 JS 渲染或防爬页面。"
        
        # Step 4: 转 Markdown
        return self._to_markdown(main)
    
    def _remove_noise(self, soup: BeautifulSoup):
        """清理噪声标签和元素。"""
        # 删除噪声标签
        for tag_name in self.NOISE_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        
        # 删除 class/id 包含噪声关键词的元素
        for tag in soup.find_all(True):
            classes = " ".join(tag.get("class", []))
            tag_id = tag.get("id", "")
            combined = f"{classes} {tag_id}".lower()
            if any(kw in combined for kw in self.NOISE_KEYWORDS):
                tag.decompose()
    
    def _find_main_content(self, soup: BeautifulSoup) -> Optional[Tag]:
        """优先找语义化标签。"""
        # 优先级：article > main > [role=main]
        for selector in ["article", "main", "[role=main]"]:
            result = soup.select_one(selector)
            if result and len(result.get_text(strip=True)) > 100:
                return result
        return None
    
    def _score_and_pick(self, soup: BeautifulSoup) -> Optional[Tag]:
        """给所有 block 元素打分，选最高分的。"""
        candidates = []
        for tag in soup.find_all(["div", "section", "article", "td", "main"]):
            score = self._score_element(tag)
            if score > 0:
                candidates.append((score, tag))
        
        if not candidates:
            return None
        
        # 按分数降序，返回最高分
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    
    def _score_element(self, el: Tag) -> float:
        """评分公式：文本长度 × (1 - 链接密度惩罚)。"""
        text = el.get_text(strip=True)
        text_len = len(text)
        
        # 文本太短不考虑
        if text_len < 80:
            return 0
        
        # 计算链接密度
        link_len = 0
        for a in el.find_all("a"):
            link_len += len(a.get_text(strip=True))
        
        link_ratio = link_len / text_len if text_len > 0 else 0
        penalty = min(link_ratio * 2.0, 1.0)
        
        return text_len * (1.0 - penalty)
    
    def _to_markdown(self, el: Tag) -> str:
        """将 HTML 元素转换为 Markdown。"""
        lines = []
        
        for child in el.descendants:
            if not isinstance(child, Tag):
                continue
            
            # 标题
            if child.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(child.name[1])
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"\n{'#' * level} {text}\n")
            
            # 段落
            elif child.name == "p":
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"\n{text}\n")
            
            # 代码块
            elif child.name == "pre":
                code = child.get_text()
                lines.append(f"\n```\n{code.strip()}\n```\n")
            
            elif child.name == "code" and child.parent.name != "pre":
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"`{text}`")
            
            # 链接
            elif child.name == "a":
                href = child.get("href", "")
                text = child.get_text(strip=True)
                if href and text and not href.startswith("#"):
                    lines.append(f"[{text}]({href})")
            
            # 粗体
            elif child.name in ("strong", "b"):
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"**{text}**")
            
            # 列表项
            elif child.name == "li":
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"- {text}")
        
        # 去重连续空行
        result = "\n".join(lines)
        while "\n\n\n" in result:
            result = result.replace("\n\n\n", "\n\n")
        
        return result.strip()
```

### 3.8 WebFetcher（web/fetcher/fetcher.py）

```python
import httpx
from typing import Optional
from web.fetcher.policy import NetworkPolicy
from web.fetcher.extractor import HtmlExtractor


class WebFetcher:
    """网页抓取器：HTTP 请求 + 安全检查 + 正文提取。"""
    
    def __init__(self, max_size: int = 5 * 1024 * 1024, timeout: int = 30):
        self._policy = NetworkPolicy()
        self._extractor = HtmlExtractor()
        self._max_size = max_size  # 5MB
        self._timeout = timeout
    
    def fetch(self, url: str, extract_content: bool = True) -> dict:
        """抓取网页内容。
        
        Args:
            url: 目标 URL
            extract_content: 是否提取正文（False 返回原始 HTML）
            
        Returns:
            {"title": str, "content": str, "url": str}
        """
        # 安全检查
        url_err = self._policy.check_url(url)
        if url_err:
            raise RuntimeError(f"URL 安全检查失败: {url_err}")
        
        rate_err = self._policy.check_rate()
        if rate_err:
            raise RuntimeError(rate_err)
        
        # HTTP 请求
        headers = {"User-Agent": "Mozilla/5.0 (compatible; WeaveMind/1.0)"}
        
        try:
            resp = httpx.get(
                url, 
                headers=headers,
                follow_redirects=True, 
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # SSL 回退
            if "ssl" not in str(e).lower():
                raise RuntimeError(f"WebFetch 请求失败: {e}") from e
            resp = httpx.get(
                url, headers=headers, follow_redirects=True,
                timeout=self._timeout, verify=False,
            )
            resp.raise_for_status()
        
        # 截断大响应
        content = resp.text[:self._max_size]
        
        # 提取正文
        if extract_content:
            extracted = self._extractor.extract(content)
            return {
                "title": self._extract_title(content),
                "content": extracted,
                "url": str(resp.url),
            }
        else:
            return {
                "title": self._extract_title(content),
                "content": content,
                "url": str(resp.url),
            }
    
    def _extract_title(self, html: str) -> str:
        """从 HTML 提取标题。"""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        title = soup.find("title")
        return title.get_text(strip=True) if title else ""
```

### 3.9 WebSearchTool 重写（tools/builtin/web_search.py）

```python
from typing import Optional
from tools.base import WeaveMindTool
from web.providers.factory import SearchProviderFactory


class WebSearchTool(WeaveMindTool):
    """搜索互联网，获取实时信息。"""
    
    name: str = "WebSearch"
    description: str = (
        "搜索互联网，获取实时信息（最新版本、官方文档、技术资讯等）。"
        "参数：query（搜索关键词），top_k（返回数量，默认5）"
    )
    
    def _run(self, query: str, top_k: int = 5) -> str:
        from web.providers.factory import SearchProviderFactory
        
        provider = SearchProviderFactory.create()
        
        if not provider.is_ready():
            return provider.unavailable_hint()
        
        try:
            results = provider.search(query, top_k)
        except Exception as e:
            return f"WebSearch 请求失败: {e}"
        
        if not results:
            return "未找到相关结果。"
        
        # 格式化输出
        formatted = []
        for i, r in enumerate(results, 1):
            formatted.append(f"[{i}. {r.title}]({r.url})\n{r.content}")
        
        return "\n\n---\n\n".join(formatted)
```

### 3.10 WebFetchTool 重写（tools/builtin/web_fetch.py）

```python
from tools.base import WeaveMindTool
from web.fetcher.fetcher import WebFetcher


class WebFetchTool(WeaveMindTool):
    """抓取指定 URL，提取正文转 Markdown。"""
    
    name: str = "WebFetch"
    description: str = (
        "抓取指定 URL，提取正文转 Markdown。适用静态/SSR 页面。"
        "JS 渲染或防爬站会返回空正文。"
        "参数：url（完整 URL），max_chars（最大字符数，默认 8000）"
    )
    
    def _run(self, url: str, max_chars: int = 8000) -> str:
        fetcher = WebFetcher()
        
        try:
            result = fetcher.fetch(url, extract_content=True)
        except RuntimeError as e:
            return f"WebFetch 失败: {e}"
        
        content = result["content"][:max_chars]
        title = result["title"]
        
        if not content or content.startswith("未提取到正文"):
            return f"未提取到正文。可能是 JS 渲染或防爬页面。\nURL: {url}"
        
        return f"# {title}\n\n{content}"
```

### 3.11 SearchResult 数据模型（web/models.py）

```python
from dataclasses import dataclass


@dataclass
class SearchResult:
    """搜索结果。"""
    title: str
    url: str
    content: str
```

---

## 四、WeaveMind 联网搜索升级计划

### 4.1 Phase 1：基础架构（优先级 P0）

**目标**：替换现有简陋实现，建立可扩展的 Provider 架构。

**任务清单**：

| # | 任务 | 文件 | 难度 | 预计时间 |
|---|------|------|------|----------|
| 1 | 创建 web/ 目录结构 | `web/__init__.py` | 低 | 5分钟 |
| 2 | 实现 SearchProvider 抽象基类 | `web/providers/base.py` | 低 | 10分钟 |
| 3 | 实现 TavilyProvider | `web/providers/tavily.py` | 低 | 15分钟 |
| 4 | 实现 SearchProviderFactory | `web/providers/factory.py` | 低 | 15分钟 |
| 5 | 重写 WebSearchTool | `tools/builtin/web_search.py` | 中 | 20分钟 |
| 6 | 测试 WebSearch | `tests/test_web_search.py` | 中 | 30分钟 |

**验收标准**：
- `WebSearch(query="Python 3.12")` 能返回格式化结果
- 未配置 TAVILY_API_KEY 时返回友好提示，不崩溃
- 所有测试通过

### 4.2 Phase 2：WebFetch 增强（优先级 P0）

**目标**：替换正则提取，实现基于 BeautifulSoup 的正文提取。

**任务清单**：

| # | 任务 | 文件 | 难度 | 预计时间 |
|---|------|------|------|----------|
| 1 | 实现 NetworkPolicy（SSRF防护+限流） | `web/fetcher/policy.py` | 中 | 30分钟 |
| 2 | 实现 HtmlExtractor（四步提取算法） | `web/fetcher/extractor.py` | 中 | 45分钟 |
| 3 | 实现 WebFetcher（HTTP+安全+提取） | `web/fetcher/fetcher.py` | 中 | 30分钟 |
| 4 | 重写 WebFetchTool | `tools/builtin/web_fetch.py` | 中 | 20分钟 |
| 5 | 测试 WebFetch | `tests/test_web_fetch.py` | 中 | 30分钟 |

**验收标准**：
- 抓取博客文章能提取干净正文，无导航栏/广告/页脚
- 抓取 localhost 返回安全错误提示
- 连续快速请求触发限流提示
- 所有测试通过

### 4.3 Phase 3：多搜索引擎支持（优先级 P1）

**目标**：支持 SearXNG 自部署搜索引擎。

**任务清单**：

| # | 任务 | 文件 | 难度 | 预计时间 |
|---|------|------|------|----------|
| 1 | 实现 SearXNGProvider | `web/providers/searxng.py` | 中 | 20分钟 |
| 2 | 更新 SearchProviderFactory | `web/providers/factory.py` | 低 | 10分钟 |
| 3 | 添加配置支持 | `config.yaml` | 低 | 5分钟 |
| 4 | 测试 SearXNG | `tests/test_web_search.py` | 中 | 20分钟 |

**验收标准**：
- 配置 `SEARCH_PROVIDER=searxng` 后自动切换
- SearXNG 未部署时返回部署指南
- 工厂能自动检测可用 Provider

### 4.4 Phase 4：高级功能（优先级 P2）

**目标**：增强健壮性和用户体验。

**任务清单**：

| # | 任务 | 文件 | 难度 | 预计时间 |
|---|------|------|------|----------|
| 1 | 懒加载 Provider | `tools/builtin/web_search.py` | 低 | 10分钟 |
| 2 | 搜索结果缓存 | `web/providers/cache.py` | 中 | 30分钟 |
| 3 | 重试机制 | `web/fetcher/retry.py` | 中 | 20分钟 |
| 4 | 支持更多搜索引擎 | `web/providers/` | 中 | 每个20分钟 |

**可选扩展**：
- **Brave Search**：免费额度，国际搜索
- **DuckDuckGo**：完全免费，无需 API Key
- **Google Custom Search**：需要 Google API Key

### 4.5 配置示例（config.yaml）

```yaml
# 联网搜索配置
web:
  search:
    provider: auto  # auto, tavily, searxng
    # tavily:
    #   api_key: ${TAVILY_API_KEY}
    # searxng:
    #   url: http://localhost:8888
  
  fetch:
    timeout: 30
    max_size: 5242880  # 5MB
    rate_limit: 30     # 60秒内最多30次
```

### 4.6 依赖更新（requirements.txt）

```
# 新增依赖
beautifulsoup4>=4.12.0  # HTML 解析
httpx>=0.25.0           # HTTP 客户端（已有）
tavily-python>=0.3.0    # Tavily SDK（已有）
```

### 4.7 测试策略

**单元测试**：
- `test_providers.py` — 测试各 Provider 的 is_ready/search
- `test_policy.py` — 测试 SSRF 防护和限流
- `test_extractor.py` — 测试 HTML 正文提取
- `test_fetcher.py` — 测试完整抓取流程

**集成测试**：
- 真实 API 调用（需配置 Key）
- 边界情况：空响应、超大响应、SSL 错误

**手动测试**：
- 抓取知名博客验证正文提取质量
- 测试 SPA 页面（预期返回提示）

---

## 五、总结

### 5.1 核心改动

| 组件 | 当前状态 | 升级后 |
|------|----------|--------|
| WebSearch | 只支持 Tavily | 策略模式多 Provider |
| WebFetch | 正则去标签 | BeautifulSoup 四步提取 |
| SSRF 防护 | 无 | NetworkPolicy 完整防护 |
| 限流 | 无 | 令牌桶限流 |
| 错误处理 | 崩溃 | 友好提示 |
| 扩展性 | 改代码 | 加配置/加 Provider |

### 5.2 预期效果

1. **搜索质量提升**：支持多种搜索引擎，按需切换
2. **抓取质量提升**：正文提取准确率从 ~30% 提升到 ~80%
3. **安全性提升**：防 SSRF、防重试循环
4. **用户体验提升**：友好错误提示，不会崩溃
5. **可维护性提升**：模块化架构，易于扩展

### 5.3 风险和注意事项

1. **依赖管理**：beautifulsoup4 是新增依赖，需确保兼容性
2. **API 配额**：Tavily/SerpAPI 有免费额度限制，需监控使用量
3. **反爬对策**：部分网站有 Cloudflare 等防护，当前方案无法绕过
4. **JS 渲染**：SPA 页面需要 headless browser，当前不支持

### 5.4 后续演进

- **Phase 5**：集成 Firecrawl（需 API Key）
- **Phase 6**：集成 Chrome DevTools MCP（处理 JS 渲染）
- **Phase 7**：搜索结果缓存 + 向量化（与 RAG 结合）