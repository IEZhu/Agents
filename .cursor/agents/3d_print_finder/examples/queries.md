# Example Queries for 3D Print Finder

## Basic Searches

### Household Items
```
/3dprint cable organizer
/3dprint toothbrush holder
/3dprint phone stand
/3dprint wall hooks
/3dprint small items box
```

### Workshop & Tools
```
/3dprint screwdriver holder
/3dprint drilling template
/3dprint oil funnel
/3dprint calibration cube
/3dprint 20 teeth gear
```

### Hobby & Gaming
```
/3dprint D&D dragon miniature
/3dprint dice tower
/3dprint miniature stand
/3dprint wargaming terrain
/3dprint articulated dragon
```

### Replacement Parts
```
/3dprint fridge handle
/3dprint socket cover
/3dprint battery cover
/3dprint oven knob
```

## Advanced Searches (with Requirements)

### With Size Constraints
```
User: /3dprint desk organizer
Agent: [asks for details]
User: size no more than 200x200mm, for screwdrivers and pliers
```

### With Material Preference
```
User: /3dprint hot pot holder
Agent: [clarifies]
User: functional, need PETG or ABS for heat resistance
```

### With License Requirements
```
User: /3dprint decorative vase
Agent: [searches]
User: need commercial license, going to sell
Agent: [filters for CC-BY, CC0, or commercial licenses]
```

## Comparison Queries

```
User: Compare these two models:
[link to Thingiverse model]
[link to Printables model]

Agent: [fetches both, does head-to-head PQRS comparison]
```

## Platform-Specific Searches

```
/3dprint durable gear site:grabcad.com
/3dprint tested by prusa site:printables.com
/3dprint miniature dragon site:myminifactory.com
```

## Troubleshooting Queries

```
User: /3dprint "XYZ widget" but all results are low quality

Agent:
"Checked 8 platforms, best option has PQRS: 45/100 (below quality threshold of 60).

**Issues**:
- Low ratings (< 3.5★)
- Few successful makes (< 10)
- No photos of finished models

**Recommendations**:
1. Try alternative terms: [suggest synonyms]
2. Consider related categories: [suggest related]
3. Custom model order: Fiverr, Cults3D commissions (~$20-50)

**Best available**: [shows best-of-bad with caveats]"
```

## Edge Cases

### No Results
```
User: /3dprint "very specific obscure thing"

Agent:
"🔍 Searched 8 platforms, no results found.

**Checked**:
- Printables: 0
- Thingiverse: 0
- Thangs: 0
[...]

**Alternatives**:
1. Related queries: [related terms]
2. Order modeling: $20-100 on Fiverr
3. Create it yourself: [suggest CAD tools]"
```

### Too Many Results
```
User: /3dprint box

Agent:
"Found 10,000+ models for query 'box'. Let's clarify:

1. **Purpose**: Storage for what? (tools, jewelry, cards, batteries)
2. **Size**: Small (< 50mm), medium (50-150mm), large (> 150mm)?
3. **Features**: With lid? Dividers? Transparent? Decorative?
4. **Style**: Minimalist, technical, decorative?"
```

## Expected Outputs

### Compact List (10+ results)
```markdown
## Search Results
Found **23** models across **5** platforms.

### Top Candidates (sorted by PQRS Score):

---

**1. Universal Cable Clips (5-pack)** | PQRS: **89**/100 | Printables
⭐ 4.7/5 | 📥 1,234 | ✅ 287 makes | Supports: Not needed
🔗 https://printables.com/model/...

---

**2. Parametric Cable Organizer** | PQRS: **84**/100 | Thangs
⭐ 4.5/5 | 📥 892 | ✅ 156 makes | Supports: Optional
🔗 https://thangs.com/designer/...

[... 8 more ...]
```

### Detailed Top 3
```markdown
## 🏆 Top Recommendations

### 🥇 #1: Universal Cable Clips (PQRS: 89/100)

**Platform**: Printables.com
**Creator**: @MakerName
**Why This One**: Highest community success rate, tested by Prusa, versatile sizing

**Stats**:
- ⭐ Rating: 4.7/5 (89 reviews)
- 📥 Downloads: 1,234
- ✅ Successful makes: 287

**Printability**:
- **Supports**: Not needed (FDM-optimized)
- **Est. print time**: 45 min for 5 clips (@0.2mm, 60mm/s)
- **Material**: PLA sufficient (PETG for flexibility)

**Print Settings** (recommended):
- **Layer Height**: 0.2mm
- **Infill**: 20% (functional but light)
- **Perimeters**: 3 walls
- **Build Plate Adhesion**: Brim (small contact area)
- **Speed**: 50-60mm/s

**Files**: STL, 3MF, Fusion 360 source
**License**: CC-BY 4.0 (✅ Commercial use OK, attribution required)

**Gotchas**:
- Print with opening facing up for best strength
- Comes in 5 sizes (5mm to 25mm cables) - print test set first
- Some users report needing to scale 105% for thicker cables

**Link**: https://printables.com/model/12345

---

### 🥈 #2: Parametric Cable Organizer (PQRS: 84/100)

**When to Choose This**: If you need custom sizing (parametric Fusion 360 source included)

**Platform**: Thangs.com
**Stats**: ⭐ 4.5/5 | 📥 892 | ✅ 156 makes
**Supports**: Optional tree supports for bridge
**License**: CC-BY-NC (❌ Commercial use restricted)

**Link**: https://thangs.com/designer/...

---

### 🥉 #3: Adhesive Cable Clip (PQRS: 78/100)

**When to Choose This**: If you need permanent mounting (designed for 3M adhesive backing)

**Platform**: Thingiverse
[Brief details]

---

## 📊 Full Comparison Table

| Model | PQRS | Platform | Rating | Makes | Supports | License | Link |
|-------|------|----------|--------|-------|----------|---------|------|
| Universal Cable Clips | 89 | Printables | ⭐ 4.7 | 287 | ❌ | CC-BY | 🔗 |
| Parametric Organizer | 84 | Thangs | ⭐ 4.5 | 156 | ⚠️ Optional | CC-BY-NC | 🔗 |
| Adhesive Clip | 78 | Thingiverse | ⭐ 4.3 | 124 | ❌ | CC0 | 🔗 |
[... rest of results ...]

---

## 🛠️ General FDM Tips for Cable Organizers

- **Material**: PLA works for most cases; PETG if you need flexibility
- **Orientation**: Print flat side down for maximum strength
- **Scaling**: If clips are too tight, scale 102-105% in your slicer
- **Adhesion**: Use brim for small contact areas
- **Post-processing**: Light sanding of contact surfaces helps cables slide in

---

## 📚 Sources
- Printables.com (8 results)
- Thingiverse.com (12 results)
- Thangs.com (3 results)
```

## Real-World Use Cases

### Case 1: Replacement Part with Exact Dimensions
```
User: Need a handle for IKEA HEMNES cabinet, original broke

Agent Process:
1. Search "IKEA HEMNES handle replacement"
2. Filter for parametric/customizable models
3. Provide measurement guide
4. Recommend model with STEP file for exact sizing
5. Suggest PETG for durability
6. Include assembly tips (screws needed, etc.)
```

### Case 2: Gift/Decorative Print
```
User: Want to print a gift for a friend, he likes dragons

Agent Process:
1. Clarify: Size? Detail level? Articulated or static?
2. Search "dragon figurine" on MyMiniFactory, Thingiverse
3. Filter for high-detail, photo-proven prints
4. Recommend PLA for detail
5. Suggest layer height: 0.12mm
6. Provide orientation and support tips
7. Note: Consider painting (prime with spray)
```

### Case 3: Functional Prototype
```
User: Need to quickly test an idea — dual monitor arm with c-clamp

Agent Process:
1. Search "dual monitor arm" + "vesa mount"
2. Filter for parametric models
3. Recommend draft settings (0.3mm, 10% infill) for speed
4. Suggest PLA for prototype, ABS/PETG for final
5. Warn about stress points (add perimeters)
6. Provide remix suggestions if nothing perfect found
```

## Tips for Writing Good Queries

### ✅ Good Queries
- "wall mounted filament spool holder"
- "20 teeth gear module 1.5"
- "28mm dragon miniature for DnD"
- "handle replacement for Bosch TWK7 kettle"

### ❌ Vague Queries
- "thing" (what thing?)
- "model" (too broad)
- "something for kitchen" (what specifically?)

### 💡 Pro Tips
1. Include specific dimensions if known
2. Mention brand/model for replacement parts
3. Specify functional vs decorative
4. State if you have tight bed size limits
5. Mention if you need commercial license
