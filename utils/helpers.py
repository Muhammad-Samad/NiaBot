def build_reassurance_prefix(mood: str, priority: str) -> str:
    """
    Returns a short, empathetic opener to prepend to the assistant's reply when
    the customer's current message was judged as "sad" (frustrated/upset/angry),
    so they feel heard before getting the substantive answer.

    Returns an empty string when no reassurance is needed (mood is "happy").
    """
    if mood != "sad":
        return ""

    if priority == "high":
        return (
            "I'm really sorry you're going through this - I completely understand "
            "the frustration, and I'm going to make sure this gets sorted out for you "
            "right away.\n\n"
        )

    return (
        "I'm sorry for the trouble this has caused you. I hear you, and I'm here to "
        "help get this resolved.\n\n"
    )
