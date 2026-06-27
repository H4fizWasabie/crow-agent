# Crow Agent — Database Schema

Last updated: 2026-06-21

## tables

### sessions
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | Session UUID |
| chat_id | INTEGER | Telegram chat ID |
| username | TEXT | Telegram username |
| created_at | TEXT | ISO timestamp |
| last_active | TEXT | ISO timestamp |
| parent_session_id | TEXT | For compression splits |

### turns
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| session_id | TEXT | FK → sessions.id |
| role | TEXT | 'user' or 'assistant' |
| content | TEXT | Full message text (0 when send_telegram used) |
| prompt_tokens | INTEGER | LLM prompt tokens |
| completion_tokens | INTEGER | LLM completion tokens |
| created_at | TEXT | ISO timestamp |

### turns_fts
Virtual FTS5 table on turns.content + session_id. Auto-synced via content triggers.

### tool_outputs
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | Output UUID (e.g. o_2f2464b6) |
| session_id | TEXT | FK → sessions.id |
| tool_name | TEXT | e.g. web_search, send_telegram |
| output | TEXT | Tool return value (or full message for send_telegram) |
| arguments | TEXT | JSON string of tool arguments |
| turn_id | INTEGER | FK → turns.id (nullable, added 2026-06-21) |
| created_at | TEXT | ISO timestamp |

Stores: all tool outputs >6000 chars (compression swap), plus send_telegram messages (always).

### tool_outputs_fts
Virtual FTS5 table on tool_outputs.output + arguments + tool_name. Manually synced via store_tool_output().

### turn_metrics
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| session_id | TEXT | FK → sessions.id |
| turn_count | INTEGER | Cumulative turn number in session |
| phase | TEXT | 'assemble', 'call', 'respond' |
| duration_ms | INTEGER | Phase duration |
| tool_name | TEXT | Tool name (nullable) |
| provider | TEXT | LLM provider used |
| prompt_tokens | INTEGER | |
| completion_tokens | INTEGER | |
| failure | INTEGER | 1 if failed |
| created_at | TEXT | ISO timestamp |

### tasks
Queue of pending/active tasks. Columns: id, session_id, title, description, status, priority, created_at, updated_at.

## Key relationships
- `turns.session_id` → `sessions.id`
- `tool_outputs.session_id` → `sessions.id`
- `tool_outputs.turn_id` → `turns.id` (nullable, for post-hoc correlation)
- `turn_metrics.session_id` → `sessions.id`

## Query patterns
- Last N turns: `ORDER BY id DESC LIMIT N`
- Turns in window: `WHERE session_id=X AND created_at BETWEEN Y AND Z`
- Tool calls for turn: `WHERE turn_id=X`
- Tool calls in window: `JOIN turns ON turns.id = tool_outputs.turn_id WHERE turns.created_at BETWEEN Y AND Z`
