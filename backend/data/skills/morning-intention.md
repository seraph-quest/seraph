---
name: morning-intention
description: Set a soul-aligned daily intention with top 3 tasks
requires:
  tools: [get_goals, view_soul]
user_invocable: true
---

When the user asks to set a morning intention or start their day, follow these steps:

1. Use `view_soul` to review the user's identity and values
2. Use `get_goals` to fetch active daily and weekly goals
3. Synthesize a morning briefing with:
   - **Intention**: one sentence capturing the day's spirit, aligned with soul values
   - **Top 3 tasks**: the most impactful goals/tasks to focus on today
   - **Mindset note**: a brief encouragement tied to their identity
4. Keep it concise and energizing — this should feel like a quick morning ritual
