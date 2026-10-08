---
id: task-completion-report
title: Task completion report
description: End each task with a concise work summary and server-generated UTC timing
default: true
always: true
---

# Task completion report

At the end of every completed task, include a concise report for the operator. Use this exact heading and list format:

```markdown
## Task report

- Work completed: <the actions, commands, and checks performed>
- Result: <what changed, was found, or could not be completed>
- Evidence: <relevant command output, artifact, or limitation>
```

Apply these rules:

1. Write the report even when the task made no changes or cannot be completed.
2. State facts only. Do not claim a command, change, or result that did not occur.
3. Keep the report focused on the operator's task. Mention material errors, blockers, and skipped work.
4. Do not write task start or completion timestamps yourself. The server appends authoritative UTC timestamps under `## Task timing` after your response is complete.
