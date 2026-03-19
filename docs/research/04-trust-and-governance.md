# 04. Trust And Governance

## Goal

Seraph should become more autonomous only when the trust model gets stronger with it.

## Design Direction

The system needs:

- explicit policy modes
- audit trails for meaningful actions
- scoped secret usage
- approval paths for high-risk execution
- clear separation between safe observation, planning, and privileged action

## Core Principle

If Seraph is going to act with more agency than a chatbot, the human needs clearer control surfaces than a chatbot.

## Open Research Questions

- where should approvals live as native product UX rather than just API mechanics?
- how should policy differ for native tools, MCP tools, workflows, and future channels?
- how should Seraph ask for permission without destroying flow?
- how should privileged execution be isolated from the planning layer?
