"""Human-agent handoff: hands a NiaBot conversation to a CSR in naheedopsv2.

Kept separate from the AI domains (shopping / operations / policy): the AI
only reports *signals* (handoff_signals on a routed result); this package
decides whether to hand over, queues the conversation, pauses the AI while a
human owns it and serves agent replies back to the chat widget.

  detector.py   - turns a routed AI result (or an exception) into a handoff reason
  repository.py - ncb_handoff_conversations / ncb_conversation_messages access
  service.py    - start a handoff, gate customer messages, widget updates
  routes.py     - GET /api/handoff/updates (widget polling)
"""
