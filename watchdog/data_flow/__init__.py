"""
watchdog/data_flow/
-------------------
Standalone Data Flow Monitoring Engine.

Monitors whether expected data sources are delivering data
at the right time for each foundry line.

Completely independent of existing watchdog process engines.
Does NOT read from or write to existing watchdog monitor code.

Modules:
  adapters.py   - per-source query adapters (handles different table formats)
  registry.py   - source discovery and registry management
  learner.py    - learns normal rhythm (p50/p95/p99 gaps) from history
  monitor.py    - main monitoring loop and status engine
  schema.sql    - database tables required by this engine
"""
