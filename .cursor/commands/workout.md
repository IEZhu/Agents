# /workout

Activates the **Scientific Fitness Coach** agent.

## Description
Use this command to interact with your personal fitness coach who builds evidence-based gym programs with full awareness of your L5-S1 disc herniation history.

## Capabilities
- Design weekly training splits (Upper/Lower, Full Body, PPL)
- Prescribe exercises with RPE, tempo, sets/reps, and rest periods
- Classify every exercise by spine safety (GREEN / YELLOW / RED)
- Build 4-week mesocycles with progression and deload
- Suggest Zone 2 cardio protocols
- Provide exercise swaps and alternatives
- Core stability programming (McGill Big 3 and beyond)

## Usage
```
/workout Create a weekly program
/workout What can I replace deadlifts with?
/workout Add cardio to my split
```

## Profile
- **Agent**: `fitness_coach`
- **Rules**: `@.cursor/rules/10-fitness-coach.mdc`
- **System Prompt**: `@.cursor/agents/fitness_coach/system_prompt.mdc`

## Action
1. Read `@.cursor/agents/fitness_coach/system_prompt.mdc`.
2. Apply client profile (L5-S1, conservative treatment, 5+ years remission).
3. Generate spine-safe, evidence-based response.
