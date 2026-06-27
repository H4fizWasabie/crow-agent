---
name: graphify
description: Convert any input (code, docs, concepts) into a knowledge graph
intent: analysis
triggers:
  - graphify
  - knowledge graph
  - concept map
  - relationship diagram
  - entity extraction
  - graph
---

# Graphify

Transform any input into a structured knowledge graph representation.

## Process

1. **Identify entities** — key nouns, concepts, systems, components, people
   - Each entity gets: name, type, brief description
   - Types: System, Component, Concept, Person, File, Module, Protocol, etc.

2. **Extract relationships** — connections between entities
   - Each relationship: source → verb → target (e.g., "auth module → validates → JWT token")
   - Note relationship strength/type (depends_on, contains, implements, uses)

3. **Build graph output** in this format:
   ```json
   {
     "entities": [
       {"id": "1", "name": "...", "type": "...", "description": "..."}
     ],
     "relationships": [
       {"source": "1", "target": "2", "label": "depends_on", "description": "..."}
     ]
   }
   ```

4. **Optionally visualize** as ASCII or Mermaid if requested

## Use cases
- Analyze unfamiliar codebases → map component relationships
- Extract architecture from docs → identify missing constraints
- Trace data flow through a system → find coupling points
