package com.sixoffive.ao.jarvis.trigger

/**
 * Detect the "jarvis" wake phrase in a transcript and return the command
 * text after it. Mirrors `client/jarvis_client/listen.py:extract_command`.
 */
object Trigger {
    private val pattern = Regex(
        """\b(?:hey\s+|ok\s+|okay\s+)?jarvis\b[\s,.\-:;!?]*""",
        RegexOption.IGNORE_CASE,
    )

    /** Returns the command after the trigger, or null if no trigger or
     *  nothing follows it. */
    fun extract(text: String): String? {
        val m = pattern.find(text) ?: return null
        val rest = text.substring(m.range.last + 1).trim()
        return rest.ifEmpty { null }
    }
}
