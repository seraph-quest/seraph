---
name: daily-standup
description: Generate a standup report from git and goals
requires:
  tools: [execute_code, get_goals]
user_invocable: true
---

When the user asks for a standup report, follow these steps:
1. Use `get_goals` to fetch active daily/weekly goals
2. Use `execute_code` to run `git log --oneline --since="yesterday"`
3. Synthesize a standup with three sections: Yesterday, Today, Blockers
4. Format as a clean bullet-point summary
