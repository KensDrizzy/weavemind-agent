# 🎉 WeaveMindAgent 升级完成！

## ✨ 新增功能

### 1. 全局命令 `weavemind`
不需要每次都输入 `python main.py`，现在可以从任何地方输入 `weavemind` 启动！

### 2. 炫酷启动画面
```
╦ ╦┌─┐┌─┐┬  ┌─┐  ╔╦╗┬┌┐┌┌┬┐  ╔═╗┌─┐┌─┐┌┐┌┌┬┐
║║║├┤ ├─┤└┐ ├┤    ║║║││││ ││  ╠═╣│ ┬├┤ │││ │
╚╩╝└─┘┴ ┴┗┴ └─┘   ╩ ╩┘└┘└─┘┘  ╩ ╩└─┘└─┘┘└┘ ┴

🚀 Provider: mimo | Model: mimo-v2.5-pro
Type /help for commands · Ctrl+C to exit
```

### 3. 改进的 CLI 交互
- **更好的提示符**: `🤖 >` 替代普通 `>`
- **思考状态**: 处理请求时显示 `⏳ Agent thinking...`
- **美化的命令输出**: 使用表格和面板
- **更好的错误消息**: 配有 emoji 和颜色
- **权限提示**: 权限改变时清晰显示

### 4. 改进的命令系统
运行 `/help` 显示美化的命令列表：

```
📖 Available Commands
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┓
│ Command        │ Description              │
├────────────────┼──────────────────────────┤
│ /help          │ 显示此帮助信息            │
│ /memory        │ 查看项目记忆              │
│ /sessions      │ 列出所有会话             │
│ /mode [MODE]   │ 切换权限模式             │
│ /clear         │ 清空屏幕                 │
│ /exit          │ 退出 Agent               │
└────────────────┴──────────────────────────┘
```

---

## 🚀 快速开始（3 步）

### 步骤 1️⃣：运行安装脚本

```bash
cd /Users/lqf/projects/agentcode/WeaveMindAgent
bash install.sh
```

### 步骤 2️⃣：刷新 shell 配置

```bash
source ~/.zshrc  # 或 source ~/.bash_profile
```

### 步骤 3️⃣：测试！

```bash
# 从任何地方都可以运行
cd ~
weavemind

# 或从其他目录
cd /tmp
weavemind

# 或者用完整路径（无需 alias）
/Users/lqf/projects/agentcode/WeaveMindAgent/weavemind
```

---

## 📝 使用示例

### 启动 Agent
```bash
weavemind
```

### 看到这个画面（成功！）
```
        ╦ ╦┌─┐┌─┐┬  ┌─┐  ╔╦╗┬┌┐┌┌┬┐  ╔═╗┌─┐┌─┐┌┐┌┌┬┐
        ║║║├┤ ├─┤└┐ ├┤    ║║║││││ ││  ╠═╣│ ┬├┤ │││ │
        ╚╩╝└─┘┴ ┴┗┴ └─┘   ╩ ╩┘└┘└─┘┘  ╩ ╩└─┘└─┘┘└┘ ┴
        
                    🚀 Provider: mimo | Model: mimo-v2.5-pro                    
                    Type /help for commands · Ctrl+C to exit                    

🤖 >
```

### 与 Agent 对话
```
🤖 > 帮我列出所有 Python 文件
⏳ Agent thinking...
✓ Tool executed successfully (1 tools used)

目录中找到了 40 个 Python 文件...

🤖 > 读一下 config.yaml 的前 5 行
⏳ Agent thinking...
✓ Tool executed successfully (1 tools used)

llm:
  provider: mimo
  model: mimo-v2.5-pro
  ...

🤖 >
```

### 查看帮助
```
🤖 > /help
```

### 退出
```
🤖 > /exit
👋 Goodbye!
```

---

## 🔧 设置选项

### 选项 A：使用 alias（推荐）
已通过 `install.sh` 自动设置。修改 `~/.zshrc`：
```bash
alias weavemind="/Users/lqf/projects/agentcode/WeaveMindAgent/weavemind"
```

### 选项 B：使用软链接
```bash
sudo ln -s /Users/lqf/projects/agentcode/WeaveMindAgent/weavemind /usr/local/bin/weavemind
```
然后从任何地方运行：
```bash
weavemind
```

### 选项 C：直接使用完整路径
```bash
/Users/lqf/projects/agentcode/WeaveMindAgent/weavemind
```

---

## 🎨 文件说明

| 文件 | 说明 |
|------|------|
| `weavemind` | 可执行脚本，激活虚拟环境并运行 agent |
| `install.sh` | 一键安装脚本，自动添加 alias |
| `QUICK_START.md` | 详细的快速开始指南 |
| `cli/app.py` | 改进的 CLI 主程序（新增启动画面） |
| `cli/commands.py` | 改进的命令处理（美化输出） |
| `cli/renderer.py` | 改进的响应渲染器 |

---

## 📊 改进总结

### 之前 ❌
```bash
$ python main.py
$ cd /some/path
$ python /Users/lqf/projects/agentcode/WeaveMindAgent/main.py
```

### 现在 ✅
```bash
$ weavemind
$ cd /some/path
$ weavemind  # 任何地方都可以！
```

### 视觉改进
- ❌ 普通提示：`> `
- ✅ 新提示：`🤖 > `

- ❌ 普通帮助：`Commands: /help /memory ...`
- ✅ 新帮助：美化表格 + 详细说明

- ❌ 普通错误：`Error: ...`
- ✅ 新错误：`❌ Error: ...` （带 emoji 和颜色）

---

## 🎯 下一步

1. **安装全局命令** (3 分钟)
   ```bash
   bash /Users/lqf/projects/agentcode/WeaveMindAgent/install.sh
   source ~/.zshrc
   ```

2. **开始使用**
   ```bash
   weavemind
   ```

3. **探索功能**
   ```
   🤖 > /help
   🤖 > 帮我做 XXX
   🤖 > /exit
   ```

---

## 💡 常见问题

**Q: 我运行 install.sh 后还是不能用 weavemind？**
A: 需要刷新 shell 配置：
```bash
source ~/.zshrc  # 或 source ~/.bash_profile
```

**Q: 如何验证安装成功？**
A: 运行：
```bash
alias | grep weavemind  # 应该显示 alias 定义
which weavemind        # 应该显示脚本路径
```

**Q: 可以从任何 shell（bash/zsh）运行吗？**
A: 是的！脚本对所有 shell 都兼容。但需要在对应的配置文件中添加 alias。

**Q: 脚本如何工作？**
A: 它做三件事：
1. 找到项目所在的目录
2. 激活虚拟环境（`.venv`）
3. 运行 `main.py`

---

## 🎉 大功告成！

现在你可以：
- ✅ 从任何地方输入 `weavemind` 启动 agent
- ✅ 享受炫酷的启动画面
- ✅ 更好的 CLI 交互体验
- ✅ 更清晰的命令输出

**开始使用吧！** 🚀
```bash
weavemind
```
