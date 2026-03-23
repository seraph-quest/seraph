---
name: weekly-planner
description: Plan the week from soul priorities and active goals
requires:
  tools: [get_goals, create_goal, view_soul]
user_invocable: true
---

When the user asks to plan their week, follow these steps:

1. Use `view_soul` to review the user's identity, values, and current priorities
2. Use `get_goals` to fetch all active goals across all levels (mission, monthly, weekly, daily)
3. Identify gaps: which soul priorities lack corresponding weekly/daily goals?
4. Propose a weekly plan with:
   - **Focus areas**: 2-3 themes aligned with soul values
   - **Key goals**: existing goals to advance this week
   - **New goals**: suggest 2-3 new weekly/daily goals to fill gaps
5. Ask the user which suggestions to adopt
6. Use `create_goal` to create any approved new goals
7. Summarize the final weekly plan in a clean format
