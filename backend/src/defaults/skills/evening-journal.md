---
name: evening-journal
description: Guided evening reflection on wins, learnings, and gratitude
requires:
  tools: [get_goals, get_goal_progress]
user_invocable: true
---

When the user asks for an evening reflection or journal prompt, follow these steps:

1. Use `get_goals` to fetch today's active goals
2. Use `get_goal_progress` to check what was accomplished
3. Guide the user through a structured reflection:
   - **Wins**: what goals advanced or completed today? Celebrate progress.
   - **Learnings**: what surprised you? What would you do differently?
   - **Gratitude**: name one thing from today you're grateful for
   - **Tomorrow**: one thing to carry forward or start fresh
4. Keep the tone warm and reflective — this is a wind-down ritual
5. Summarize their responses into a brief journal entry they can keep
