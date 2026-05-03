import threading

# Set by SIGUSR1 handler in run_classify. Checked by provider polling loops.
# Each classify invocation is a separate process, so this is safe as a
# module-level singleton.
ABORT_EVENT = threading.Event()
