---
name: goal-reflection
description: Weekly goal reflection comparing progress to soul values
requires:
  tools: [get_goals, get_goal_progress, view_soul]
user_invocable: true
---

When the user asks for a goal reflection, follow these steps:
1. Use `view_soul` to review the user's identity, values, and priorities
2. Use `get_goals` to fetch all active goals
3. Use `get_goal_progress` to check progress on each goal
4. Synthesize a reflection with:
   - **Progress summary**: which goals advanced, which stalled
   - **Alignment check**: are active goals aligned with soul values?
   - **Wins**: celebrate completed milestones
   - **Adjustments**: suggest goal reprioritization if needed
5. End with one actionable recommendation for the coming week
