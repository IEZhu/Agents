#!/bin/bash

# Configuration
MCP_CONFIG_PATH="$HOME/.cursor/mcp.json"

# Colors
BLUE='\033[1;34m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
RED='\033[1;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}🔌 GitHub MCP Setup${NC}"

# 2. Check/Build Binary
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_DIR="$REPO_ROOT/.cursor/mcp/github-mcp-server"
LOCAL_BINARY="$MCP_DIR/github-mcp-server"

echo ""
if [ ! -f "$LOCAL_BINARY" ]; then
    echo -e "${YELLOW}⚠️  Local binary not found. Building from source...${NC}"
    
    # Check if Go is installed
    if ! command -v go &> /dev/null; then
        echo -e "${RED}❌ Error: 'go' is not installed.${NC}"
        echo "Please install Go (Golang) to build the server."
        exit 1
    fi

    # Clone if directory doesn't exist
    if [ ! -d "$MCP_DIR" ]; then
        echo "Cloning repository..."
        mkdir -p "$MCP_DIR"
        git clone https://github.com/github/github-mcp-server.git "$MCP_DIR"
    fi

    # Build
    echo "Building server..."
    cd "$MCP_DIR" || exit
    go build -o github-mcp-server cmd/github-mcp-server/main.go
    
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Build failed.${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}✓ Build successful${NC}"
    cd "$REPO_ROOT" || exit
else
    echo -e "${GREEN}✓ Local binary found${NC}"
fi

# 2. Instruction
echo ""
echo "To connect to GitHub, you need to create a Personal Access Token."
echo "Please follow these steps:"
echo -e "1. Open URL: ${BLUE}https://github.com/settings/tokens${NC}"
echo "2. Click 'Generate new token (classic)'"
echo "3. Fill in the fields:"
echo -e "   - Note: ${BLUE}Cursor MCP${NC}"
echo -e "   - Scopes: select ${BLUE}repo${NC}, ${BLUE}read:user${NC}, ${BLUE}user:email${NC}, ${BLUE}project${NC}"
echo "4. Click 'Generate token'"
echo "5. Copy the generated token (it starts with 'ghp_')"
echo ""
echo "--------------------------------------------------"

# 3. Request Token
while true; do
    echo ""
    read -p "Paste your token (ghp_...): " TOKEN
    if [[ "$TOKEN" == ghp_* ]] && [[ ${#TOKEN} -gt 10 ]]; then
        break
    else
        echo -e "${YELLOW}! Token must start with 'ghp_'. Please try again.${NC}"
    fi
done

# 4. Update mcp.json using Python
echo ""
echo "Updating configuration..."

# Determine path to local github-mcp-server binary if it exists
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_BINARY="$REPO_ROOT/.cursor/mcp/github-mcp-server/github-mcp-server"

# Python script to update JSON
python3 -c "
import json
import os
import sys

config_path = '$MCP_CONFIG_PATH'
token = '$TOKEN'
local_binary = '$LOCAL_BINARY'

try:
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            config = json.load(f)
    else:
        config = {'mcpServers': {}}

    if 'mcpServers' not in config:
        config['mcpServers'] = {}

    # Check if local binary exists
    if not os.path.exists(local_binary):
        print(f'ERROR: Binary not found at {local_binary}')
        sys.exit(1)
    
    print(f'Using local binary at: {local_binary}')
    config['mcpServers']['github'] = {
        'command': local_binary,
        'args': ['stdio'],
        'env': {
            'GITHUB_PERSONAL_ACCESS_TOKEN': token
        }
    }

    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)

    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {e}')
    sys.exit(1)
"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ Configuration successfully written to mcp.json${NC}"
    echo -e "${BLUE}🎉 Done! Restart Cursor (Cmd+Shift+P -> MCP: Restart MCP Servers) to apply changes.${NC}"
else
    echo -e "${RED}❌ Error updating configuration file.${NC}"
    exit 1
fi
