"""The Aurora analyst: Claude answering questions over Aurora's own numbers.

Every figure the analyst quotes comes from a read-only tool that calls the
same deterministic analytics the dashboard uses. The model chooses which
tools to call and explains; it never computes money itself. See tools.py for
the tool surface and agent.py for the loop.
"""
