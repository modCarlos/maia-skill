#!/usr/bin/env bash
# Tododeia / MAIA Skill — Installer
# Usage: curl -sL https://raw.githubusercontent.com/Hainrixz/maia-skill/main/install.sh | bash

set -e

REPO="https://github.com/Hainrixz/maia-skill.git"
SKILL_NAME="investment-analysis"
INSTALL_DIR="$HOME/.claude/skills/$SKILL_NAME"

echo ""
echo "  Tododeia — Multi-Agent Investment Analysis"
echo "  by @quebert"
echo ""

# Check if already installed
if [ -d "$INSTALL_DIR" ]; then
  echo "  Skill already installed at $INSTALL_DIR"
  echo "  Updating..."
  cd "$INSTALL_DIR" && git pull --quiet
  echo "  Updated successfully."
  echo ""
  exit 0
fi

# Clone the repo
echo "  Cloning skill..."
CLONE_DIR="$HOME/.claude/plugins/maia-skill"
mkdir -p "$HOME/.claude/plugins"
git clone --quiet "$REPO" "$CLONE_DIR"

# Symlink skill to Claude Code skills directory
mkdir -p "$HOME/.claude/skills"
ln -sf "$CLONE_DIR/.claude/skills/$SKILL_NAME" "$INSTALL_DIR"

# Install dashboard dependencies if Node.js is available
if command -v npm &> /dev/null; then
  echo "  Installing dashboard dependencies..."
  npm install --prefix "$CLONE_DIR/dashboard" --silent 2>/dev/null
  echo "  Dashboard ready."
else
  echo "  Node.js not found — dashboard will use HTML fallback."
  echo "  Install Node.js 18+ for the interactive dashboard."
fi

echo ""
echo "  Installed successfully!"
echo ""
echo "  Open Claude Code and say:"
echo "    \"Run an investment analysis\""
echo "    \"Analyze the markets\""
echo "    \"Run tododeia\""
echo ""
echo "  To uninstall:"
echo "    rm -rf $INSTALL_DIR $CLONE_DIR"
echo ""
