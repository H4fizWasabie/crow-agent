---
name: remote-procurement-audit
description: Audit procurement data on remote servers by executing commands, searching files, and querying systems to investigate and act on findings.
intent: automation
triggers:
  - ssh_exec
---

# remote-procurement-audit

Audit procurement data on remote servers by executing commands, searching files, and querying systems to investigate and act on findings.

## Steps

1. Execute SSH command to prepare or connect to remote server
2. Search files for procurement-related patterns or logs
3. Query procurement system for detailed information
4. Search files again to correlate or verify query results
5. Execute final SSH command to implement changes or report findings
