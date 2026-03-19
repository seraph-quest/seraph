---
name: code-review
description: Review a file with structured feedback
requires:
  tools: [shell_execute, read_file]
user_invocable: true
---

When the user asks for a code review, follow these steps:
1. Use `read_file` to read the target file
2. Analyze the code for: style issues, potential bugs, performance concerns, and readability
3. Use `shell_execute` to check for linting issues if a linter config is present
4. Provide structured feedback with sections:
   - **Style**: naming, formatting, consistency
   - **Bugs**: potential issues, edge cases, error handling
   - **Suggestions**: improvements, simplifications, best practices
5. Rate overall quality on a 1-5 scale with brief justification
