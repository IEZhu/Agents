# Commands Directory

Contains slash commands that trigger specific agent routing in Cursor IDE.

## Structure

```
commands/
├── route.md           # Agent routing helper
├── dev.md             # Software Engineer
├── docs.md            # Tech Writer
├── research.md        # Deep Researcher
├── ... (other commands)
└── README.md
```

## Command Format

Each `.md` file defines a slash command:

```markdown
# /command_name

Description of what this command does.
See rules: `.cursor/rules/10-agent-name.mdc`
See rules: `.cursor/rules/00-router.mdc`

Profile: **Agent Name**.

Optional additional context or instructions.
```

## Available Commands

### System Commands

| Command | Agent | Purpose |
|---------|-------|---------|
| `/route` | Router | Check available agents, manual routing |
| `/universal` | Universal Agent | General tasks, planning |
| `/new_agent` | Agent Builder | Create new agent personas |
| `/new_mcp` | MCP Builder | Create new MCP servers |
| `/install_repo` | Repo Installer | Deploy framework to another repo |
| `/init_repo` | HAI Architect | Initialize repository structure |

### Development Commands

| Command | Agent | Purpose |
|---------|-------|---------|
| `/dev` | Software Engineer | Code, debugging, refactoring |
| `/security_audit` | Security Expert | Vulnerability analysis |
| `/debug_ai` | AI Debugger | Debug AI/ML systems |
| `/ai_architect` | AI Senior Engineer | HAI system design |
| `/blender` | Blender Scripter | Python bpy scripts for 3D printing |
| `/roblox` | Roblox Studio Expert | Roblox game development |

### Documentation Commands

| Command | Agent | Purpose |
|---------|-------|---------|
| `/docs` | Tech Writer | Documentation writing |
| `/commit_en` | Tech Writer | Git commit (English) |
| `/commit_ru` | Tech Writer | Git commit (Russian) |
| `/doc_today` | Tech Writer | Daily documentation |
| `/draw` | Diagram Architect | Mermaid diagrams |

### Research & Analysis Commands

| Command | Agent | Purpose |
|---------|-------|---------|
| `/research` | Deep Researcher | Deep dive, 80/20 synthesis |
| `/investigate` | Investigative Analyst | OSINT, fact-checking |
| `/analyse_data` | Data Analyst | Data analysis, statistics |
| `/purchase` | Purchase Researcher | Product research, decision matrix |
| `/find_black_hole` | Black Hole Finder | Knowledge gap detection |

### Domain-Specific Commands

| Command | Agent | Purpose |
|---------|-------|---------|
| `/doctor` | Medical Expert | Clinical analysis, diagnosis |
| `/bio_protocol` | Bio-Hacker | Health optimization |
| `/psy_session` | Psychologist | Psychological support |
| `/insta_audit` | Instagram Analyst | Social media analysis |
| `/site_audit` | Website Analyst | Website business audit |
| `/briefing` | Daily Briefing | News digest |
| `/3dprint` | 3D Print Finder | 3D model search |
| `/ocr` | Document OCR Expert | Text extraction from images/PDF |
| `/forensic` | Data Forensic | Leak analysis, timeline |
| `/semantic_parse` | Semantic Expert | Meaning reconstruction |
| `/alerts` | Alerts Describer | Infrastructure alerts |
| `/presentation` | Presentation Coach | Slide design |
| `/workout` | Fitness Coach | Spine-safe fitness programming |
| `/literary` | Literary Writer | Artistic prose creation |
| `/daily_report` | Work Reporter | Daily git activity report |

## Usage

In Cursor IDE:

1. **Type command**: `/command_name`
2. **Add your request**: `/dev fix the authentication bug`
3. **Press Enter**: Agent is loaded and responds

### Examples

```
/dev How do I optimize this SQL query?
/research What are the best practices for API versioning?
/draw Create a sequence diagram for user registration
/purchase Help me choose between MacBook Pro and Dell XPS
/doctor Interpret these blood test results
```

## Creating a New Command

1. **Create file**: `.cursor/commands/mycommand.md`

2. **Add content**:
   ```markdown
   # /mycommand

   Description of the command purpose.
   See rules: `.cursor/rules/10-my-agent.mdc`
   See rules: `.cursor/rules/00-router.mdc`

   Profile: **My Agent**.

   Additional instructions:
   1. Step one
   2. Step two
   ```

3. **Ensure agent exists**: The referenced agent must have a `system_prompt.mdc`

4. **Restart Cursor**: To load the new command

## Command Resolution

1. User types `/command`
2. Cursor loads the `.md` file content
3. Content is prepended to user's message
4. Router identifies the agent from `Profile: **Agent Name**`
5. Agent context is loaded via MCP or static mode
