"""A terminal dashboard for the auto-applier. It only reads what the daemon already writes (state.db, daemon.log, the
per-job folders under applications/) and asks Ollama which models are loaded; it never writes and never touches the daemon."""
