"""Lean paper trader: a simple paper-only trader beside the current desk. No signing, no broadcasting, no real funds."""
import logging

# Library convention: no output unless the entry point (python -m lean) configures logging.
logging.getLogger(__name__).addHandler(logging.NullHandler())
