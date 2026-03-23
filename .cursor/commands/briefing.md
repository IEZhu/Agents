# /briefing

Starts the generation of a daily news summary.

## Description

The command activates the **Daily Briefing Analyst** agent, which:
1. Collects news over the last 24 hours via `web_search`
2. Verifies data across multiple sources
3. Filters down to the top 12 most important events
4. Analyzes connections, stakeholders, and consequences

## Usage

```
/briefing
```

or

```
/briefing focus on economy
/briefing Russia and CIS
/briefing technology and AI
```

## Output Format

- **Events Table**: #, time, event, category, source, stakeholders, consequences
- **Analytical Block**: map of connections, influence points, trends, red flags, forecast

## Rules

See: `.cursor/rules/10-daily-briefing.mdc`
Profile: **Daily Briefing Analyst**

## Event Categories

| Icon | Category |
|--------|-----------|
| 🌍 | Geopolitics |
| 💰 | Economy |
| ⚔️ | Security/Conflicts |
| 🏛️ | Domestic Politics |
| 🔬 | Technology |
| 🌡️ | Climate/Energy |

## Source Tiers

| Tier | Reliability | Examples |
|------|------------|---------|
| Tier 1 | Highest | Reuters, AP, Bloomberg, official statements |
| Tier 2 | High | BBC, NYT, TASS, analytical centers |
| Tier 3 | Needs verification | Telegram, social media, anonymous sources |
