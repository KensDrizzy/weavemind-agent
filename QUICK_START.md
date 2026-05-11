# 🚀 WeaveMindAgent 快速开始

## 安装与设置

### 方式 1：全局命令（推荐）

在 shell 配置文件中添加别名（一次性设置）

#### macOS/Linux - 编辑 `~/.zshrc` 或 `~/.bash_profile`

```bash
# 1. 打开配置文件
nano ~/.zshrc  # 或 vim ~/.zshrc

# 2. 在文件末尾添加以下行
alias weavemind="/Users/lqf/projects/agentcode/WeaveMindAgent/weavemind"

# 3. 保存并刷新配置
source ~/.zshrc
```

#### 验证安装

```bash
# 现在可以从任何地方运行
cd ~
weavemind

# 或者在项目目录
cd /some/other/path
weavemind
```

### 方式 2：将脚本添加到 PATH

```bash
# 创建软链接到 /usr/local/bin
sudo ln -s /Users/lqf/projects/agentcode/WeaveMindAgent/weavemind /usr/local/bin/weavemind

# 现在可以从任何地方运行
weavemind
```

---

## 🎨 启动效果

启动后会显示：

```
╦ ╦┌─┐┌─┐┬  ┌─┐  ╔╦╗┬┌┐┌┌┬┐  ╔═╗┌─┐┌─┐┌┐┌┌┬┐
║║║├┤ ├─┤└┐ ├┤    ║║║││││ ││  ╠═╣│ ┬├┤ │││ │
╚╩╝└─┘┴ ┴┗┴ └─┘   ╩ ╩┘└┘└─┘┘  ╩ ╩└─┘└─┘┘└┘ ┴

🚀 Provider: mimo | Model: mimo-v2.5-pro
Type /help for commands · Ctrl+C to exit

🤖 >
```

---

## 💬 基本使用

### 普通对话

```
🤖 > 你好，请帮我列出所有 Python 文件
```

Agent 会自动理解你的需求并调用工具。

### 特殊命令

```bash
/help                    # 显示所有可用命令
/mode default           # 切换权限模式（default | acceptEdits | bypassPermissions）
/sessions               # 查看历史会话
/memory                 # 查看项目记忆
/clear                  # 清屏
/exit 或 /quit          # 退出 Agent
```

### 工具调用示例

```
🤖 > 帮我找一下所有的测试文件
⏳ Agent thinking...
✓ 找到 3 个测试文件:
- tests/test_permissions.py
- tests/test_subagents.py
- tests/test_tools.py

🤖 > 读一下 config.yaml 的前 10 行
⏳ Agent thinking...
✓ llm:
  provider: mimo
  model: mimo-v2.5-pro
  max_tokens: 8192
  ...

🤖 >
```

---

## 🔐 权限模式

### 默认模式 (default)
所有工具可用（除了黑名单）

### 编辑模式 (acceptEdits)  
仅允许文件编辑、删除等操作

### 完全模式 (bypassPermissions)
绕过所有权限检查

### 切换模式

```
🤖 > /mode acceptEdits
🔐 Permission Mode: acceptEdits
```

---

## 🛠️ 支持的工具

Agent 可以自动调用以下工具：

| 工具 | 功能 |
|------|------|
| **ReadTool** | 读取文件内容 |
| **WriteTool** | 写入/创建文件 |
| **EditTool** | 编辑文件内容 |
| **BashTool** | 执行 shell 命令 |
| **GlobTool** | 查找匹配文件 |
| **GrepTool** | 搜索文件内容 |
| **WebSearchTool** | 网络搜索 |
| **WebFetchTool** | 获取网页内容 |
| **AskUserTool** | 向用户提问 |
| **SubAgentTool** | 启动子 agent |

---

## 📝 配置文件

修改 `config.yaml` 可调整：

```yaml
llm:
  provider: mimo              # LLM 提供商（anthropic | deepseek | mimo | openai）
  model: mimo-v2.5-pro        # 模型名称
  max_tokens: 8192            # 最大 token
  temperature: 0              # 创意度（0=确定，1=随机）

permissions:
  default_mode: default       # 默认权限模式
```

---

## 🐛 故障排除

**问题：命令找不到**
```bash
# 解决：检查别名是否生效
alias | grep weavemind

# 如果没有，重新执行：
source ~/.zshrc
```

**问题：权限被拒绝**
```bash
# 确保脚本可执行
chmod +x /Users/lqf/projects/agentcode/WeaveMindAgent/weavemind
```

**问题：虚拟环境激活失败**
```bash
# 检查虚拟环境是否存在
ls -la /Users/lqf/projects/agentcode/WeaveMindAgent/.venv

# 如果不存在，重新创建
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## ✨ 建议的工作流

1. **启动 Agent**
   ```bash
   weavemind
   ```

2. **描述任务**
   ```
   🤖 > 帮我检查 src 目录下所有 Python 文件是否有语法错误
   ```

3. **Agent 自动执行**
   - 找出所有 Python 文件
   - 运行语法检查
   - 报告结果

4. **继续对话**
   ```
   🤖 > 修复这些错误
   ```

5. **退出**
   ```
   🤖 > /exit
   ```

---

## 🎯 下一步

- 查看 `CLAUDE.md` 了解项目架构
- 在 `.weavemind/agents/` 中创建自定义 agent
- 在 `tools/builtin/` 中扩展新工具

享受 WeaveMindAgent！🚀
