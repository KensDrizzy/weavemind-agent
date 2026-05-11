#!/bin/bash
# WeaveMindAgent 一键安装脚本
# 用法: bash install.sh

PROJECT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Setting up WeaveMindAgent global command..."
echo ""

# 添加 alias 到 shell 配置
ALIAS_CMD="alias weavemind=\"$PROJECT_PATH/weavemind\""

if [ -f ~/.zshrc ]; then
    if ! grep -q "alias weavemind=" ~/.zshrc; then
        echo "$ALIAS_CMD" >> ~/.zshrc
        echo "✓ Added alias to ~/.zshrc"
    else
        echo "✓ Alias already exists in ~/.zshrc"
    fi
fi

if [ -f ~/.bash_profile ]; then
    if ! grep -q "alias weavemind=" ~/.bash_profile; then
        echo "$ALIAS_CMD" >> ~/.bash_profile
        echo "✓ Added alias to ~/.bash_profile"
    fi
fi

echo ""
echo "✅ Installation complete!"
echo ""
echo "📝 Next steps:"
echo "   1. Run: source ~/.zshrc"
echo "   2. Test: weavemind"
echo ""
echo "🎉 You can now run 'weavemind' from any directory!"
