# 3D Print Finder 🖨️

**AI-powered multi-platform search for 3D printable models**

## Overview

3D Print Finder is a specialized agent that searches ALL major 3D model platforms, ranks results by quality and printability, and provides FDM-optimized print recommendations.

## Key Features

### 🌐 Comprehensive Platform Coverage

Searches across:
- **Printables.com** (Prusa) - Curated, tested models
- **Thingiverse.com** - Largest library
- **Thangs.com** - AI-powered search
- **MyMiniFactory.com** - Designer-focused
- **Cults3D.com** - Premium designs
- **GrabCAD, YouMagine, PrusaPrinters, Maker.World**, and more

### 📊 PQRS Quality Scoring

Objective ranking system (0-100):
- **P**rintability (40%): Supports, overhangs, documented success
- **Q**uality (30%): Ratings, downloads, successful makes
- **R**elevance (20%): Match to your query
- **S**upport (10%): File formats, license, documentation

### 🖨️ FDM-First Optimization

- Auto-filters for FDM compatibility
- Warns about support requirements
- Flags thin walls and challenging features
- Suggests optimal orientation

### ⚙️ Print Recommendations

For each top model, provides:
- Layer height recommendation
- Infill percentage
- Support strategy
- Material selection
- Build plate adhesion advice
- Common gotchas

### 📜 License Compliance

- Clear license identification
- Commercial use warnings
- Attribution requirements

## Activation

### Via Command
```
/3dprint [search query]
```

### Via Context
Open any file in `.cursor/agents/3d_print_finder/` or mention 3D printing keywords.

### Manual Routing
Keywords: `3d print`, `stl`, `thingiverse`, `printables`, `3d model`

## Usage Examples

### Example 1: Quick Search
```
User: /3dprint headphone stand

Agent:
- Searches 8+ platforms
- Finds 15 candidates
- Ranks by PQRS
- Recommends top 3 with print settings
```

### Example 2: Detailed Requirements
```
User: Need a gear model, diameter 50mm, for 8mm shaft

Agent (Intake Phase):
"Great! Let's clarify:
1. Gear type: spur, helical, bevel?
2. Number of teeth?
3. Load: decorative or functional?
4. Material: is PLA enough or do you need PETG/ABS?"

User: spur, 20 teeth, functional, PETG

Agent:
- Executes specialized search
- Filters for parametric/customizable models
- Provides CAD models (STEP files) for exact sizing
- Recommends PETG-specific print settings (temp, cooling)
```

### Example 3: Comparison Mode
```
User: Which is better: [link1] or [link2]?

Agent:
- Fetches both models
- Compares PQRS scores
- Analyzes reviews and makes
- Provides head-to-head verdict
```

## Workflow

```
┌─────────────┐
│ User Query  │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│ Intake Phase    │ ← Clarify requirements
│ (if needed)     │
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│ Multi-Platform  │ ← Parallel search
│ Search          │   (web_search + browser)
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│ PQRS Scoring    │ ← Rank all results
│ & Filtering     │
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│ Top 3 Recs      │ ← Detailed analysis
│ + Print Guide   │   + settings + gotchas
└─────────────────┘
```

## PQRS Scoring Breakdown

### Printability (0-40 points)
- ✅ No supports needed: +15
- ✅ Documented success photos: +10
- ✅ Print settings provided: +10
- ✅ Creator test prints: +5

### Quality (0-30 points)
- ⭐ Rating 4.5+: +10 (or scaled)
- 📥 Downloads 1000+: +10 (or scaled)
- ✅ Successful makes 100+: +10 (or scaled)

### Relevance (0-20 points)
- 🎯 Exact match: 20
- 📂 Category match: 15
- 🔍 Partial match: 10

### Support (0-10 points)
- 📁 Multiple formats (STL, 3MF, STEP): +5
- 📜 Clear license: +3
- 📖 Documentation: +2

## FDM Print Settings by Category

### Functional Parts
- **Material**: PETG, ABS
- **Infill**: 30-50%
- **Walls**: 3-4
- **Layer Height**: 0.2-0.3mm

### Decorative
- **Material**: PLA
- **Infill**: 10-15%
- **Walls**: 2-3
- **Layer Height**: 0.12-0.16mm

### Miniatures
- **Material**: PLA (or resin for <28mm)
- **Infill**: 10%
- **Walls**: 2
- **Layer Height**: 0.08-0.12mm
- **Speed**: 30mm/s

## Platform Reference

| Platform | Strengths | Best For |
|----------|-----------|----------|
| **Printables** | Prusa-tested, high quality | Reliable prints |
| **Thingiverse** | Largest library | Variety |
| **Thangs** | AI search, semantic | Complex queries |
| **MyMiniFactory** | Curated, artistic | Miniatures, art |
| **Cults3D** | Premium designs | High-end models |
| **GrabCAD** | Engineering focus | CAD models, technical |
| **Yeggi** | Meta-search | Comprehensive coverage |

## License Quick Reference

| License | Commercial Use | Remix | Attribution |
|---------|----------------|-------|-------------|
| CC0 / Public Domain | ✅ | ✅ | ❌ |
| CC-BY | ✅ | ✅ | ✅ |
| CC-BY-SA | ✅ | ✅ (share-alike) | ✅ |
| CC-BY-NC | ❌ | ✅ | ✅ |
| CC-BY-ND | ✅ | ❌ | ✅ |
| Personal Use Only | ❌ | ❌ | ✅ |

## Tools Used

- **web_search**: Primary search across platforms
- **browser_navigate + browser_snapshot**: For platforms with poor SEO
- **read_file**: If analyzing user-provided STL files

## Limitations

### What This Agent Does
✅ Find models across all major platforms
✅ Rank by quality and printability
✅ Provide print settings recommendations
✅ Check licenses

### What It Doesn't Do
❌ Modify STL files (use CAD software)
❌ Generate custom models from scratch (use CAD or generative AI)
❌ Diagnose print failures (use debugging guides)
❌ Control your printer

## Tips for Best Results

1. **Be Specific**: "Phone holder MagSafe" > "phone thing"
2. **Mention Constraints**: Include bed size, printer type if relevant
3. **State Purpose**: "functional" vs "decorative" affects recommendations
4. **License Matters**: Specify if you need commercial use rights

## Example Output

```markdown
## 🏆 Top Recommendations

### 🥇 #1: Modular Desk Organizer (PQRS: 92/100)
**Platform**: Printables.com
**Why This One**: Highest quality, 500+ successful prints, no supports needed

**Print Settings**:
- Layer Height: 0.2mm
- Infill: 15% (decorative)
- Supports: Not needed
- Material: PLA (any color)
- Build Plate Adhesion: Brim recommended

**Gotchas**:
- Print each module separately for easy assembly
- Use 0.4mm nozzle for best detail

**Stats**: ⭐ 4.8/5 | 📥 2,341 | ✅ 512 makes
**License**: CC-BY 4.0 (✅ Commercial OK)
**Link**: [Direct URL]

---

### 🥈 #2: [Alternative]...
### 🥉 #3: [Third Option]...

## 📊 Full Comparison Table
[Table with all candidates]
```

## Files

```
.cursor/agents/3d_print_finder/
├── system_prompt.mdc      # Main agent definition
├── README.md              # This file
└── examples/
    └── queries.md         # Example searches
```

## Integration

This agent integrates with:
- **Core Protocol** (`@agents/common/core-protocol.mdc`)
- **Response Footer** (`@agents/common/response-footer.mdc`)
- **Skill: 3D Print Search** (`@skills/skill-3d-print-search.mdc`)
- **Skill: Critical Analysis** (`@skills/skill-analysis-critical.mdc`)

## Changelog

### v1.0.0 (2026-01-06)
- Initial release
- Multi-platform search support
- PQRS scoring system
- FDM-optimized recommendations
- License compliance checks
