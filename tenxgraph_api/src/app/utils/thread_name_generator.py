"""
Thread name generators for conversations.

``ThreadNameGenerator`` is the base class to subclass for custom thread names.
``RandomThreadNameGenerator`` picks a random readable name and ignores the messages.
``FirstMessageThreadNameGenerator`` names a thread after the user's first message.

Classes:
    ThreadNameGenerator: Abstract base class for thread name generators.
    RandomThreadNameGenerator: Random adjective-noun, action and compound names.
    FirstMessageThreadNameGenerator: The first message, cut to a short name.
"""

import secrets
from abc import ABC, abstractmethod


class ThreadNameGenerator(ABC):
    """Base class to subclass for custom thread names.

    Set it with ``thread_name_generator`` in 10xgraph.json as ``"module:attr"``.

    Example:
        >>> class UpperName(ThreadNameGenerator):
        ...     async def generate_name(self, messages: list[str]) -> str:
        ...         return messages[0].upper() if messages else "new thread"
    """

    @abstractmethod
    async def generate_name(self, messages: list[str]) -> str:
        """Generate a thread name using the list of message text.

        Args:
            messages (list[str]): List of message text.

        Returns:
            str: A meaningful thread name.
        """


class RandomThreadNameGenerator(ThreadNameGenerator):
    """Random thread names (``"thread_name_generator": "random"``).

    Patterns are adjective-noun, action-based and compound descriptive names. The messages
    are ignored.

    Example:
        >>> RandomThreadNameGenerator().random_name()
        'thoughtful-dialogue'
    """

    # Enhanced adjectives grouped by semantic meaning
    ADJECTIVES = [
        # Intellectual
        "thoughtful",
        "insightful",
        "analytical",
        "logical",
        "strategic",
        "methodical",
        "systematic",
        "comprehensive",
        "detailed",
        "precise",
        # Creative
        "creative",
        "imaginative",
        "innovative",
        "artistic",
        "expressive",
        "original",
        "inventive",
        "inspired",
        "visionary",
        "whimsical",
        # Emotional/Social
        "engaging",
        "collaborative",
        "meaningful",
        "productive",
        "harmonious",
        "enlightening",
        "empathetic",
        "supportive",
        "encouraging",
        "uplifting",
        # Dynamic
        "dynamic",
        "energetic",
        "vibrant",
        "lively",
        "spirited",
        "active",
        "flowing",
        "adaptive",
        "responsive",
        "interactive",
        # Quality-focused
        "focused",
        "dedicated",
        "thorough",
        "meticulous",
        "careful",
        "patient",
        "persistent",
        "resilient",
        "determined",
        "ambitious",
    ]

    # Enhanced nouns with more conversational context
    NOUNS = [
        # Conversation-related
        "dialogue",
        "conversation",
        "discussion",
        "exchange",
        "chat",
        "consultation",
        "session",
        "meeting",
        "interaction",
        "communication",
        # Journey/Process
        "journey",
        "exploration",
        "adventure",
        "quest",
        "voyage",
        "expedition",
        "discovery",
        "investigation",
        "research",
        "study",
        # Conceptual
        "insight",
        "vision",
        "perspective",
        "understanding",
        "wisdom",
        "knowledge",
        "learning",
        "growth",
        "development",
        "progress",
        # Solution-oriented
        "solution",
        "approach",
        "strategy",
        "method",
        "framework",
        "plan",
        "blueprint",
        "pathway",
        "route",
        "direction",
        # Creative/Abstract
        "canvas",
        "story",
        "narrative",
        "symphony",
        "composition",
        "creation",
        "masterpiece",
        "design",
        "pattern",
        "concept",
        # Collaborative
        "partnership",
        "collaboration",
        "alliance",
        "connection",
        "bond",
        "synergy",
        "harmony",
        "unity",
        "cooperation",
        "teamwork",
    ]

    # Action-based patterns for more dynamic names
    ACTION_PATTERNS = {
        "exploring": ["ideas", "concepts", "possibilities", "mysteries", "frontiers", "depths"],
        "building": ["solutions", "understanding", "connections", "frameworks", "bridges"],
        "discovering": ["insights", "patterns", "answers", "truths", "secrets", "wisdom"],
        "crafting": ["responses", "solutions", "stories", "strategies", "experiences"],
        "navigating": ["challenges", "questions", "complexities", "territories", "paths"],
        "unlocking": ["potential", "mysteries", "possibilities", "creativity", "knowledge"],
        "weaving": ["ideas", "stories", "connections", "patterns", "narratives"],
        "illuminating": ["concepts", "mysteries", "paths", "truths", "possibilities"],
    }

    # Descriptive compound patterns
    COMPOUND_PATTERNS = [
        ("deep", ["dive", "thought", "reflection", "analysis", "exploration"]),
        ("bright", ["spark", "idea", "insight", "moment", "flash"]),
        ("fresh", ["perspective", "approach", "start", "take", "view"]),
        ("open", ["dialogue", "discussion", "conversation", "exchange", "forum"]),
        ("creative", ["flow", "spark", "burst", "stream", "wave"]),
        ("mindful", ["moment", "pause", "reflection", "consideration", "thought"]),
        ("collaborative", ["effort", "venture", "journey", "exploration", "creation"]),
    ]

    def generate_simple_name(self, separator: str = "-") -> str:
        """
        Generate a simple adjective-noun combination for a thread name.

        Args:
            separator (str): String to separate words (default: "-").

        Returns:
            str: Name like "thoughtful-dialogue" or "creative-exploration".

        Example:
            >>> RandomThreadNameGenerator().generate_simple_name()
            'creative-exploration'
        """
        adj = secrets.choice(self.ADJECTIVES)
        noun = secrets.choice(self.NOUNS)
        return f"{adj}{separator}{noun}"

    def generate_action_name(self, separator: str = "-") -> str:
        """
        Generate an action-based thread name for a more dynamic feel.

        Args:
            separator (str): String to separate words (default: "-").

        Returns:
            str: Name like "exploring-ideas" or "building-understanding".

        Example:
            >>> RandomThreadNameGenerator().generate_action_name()
            'building-connections'
        """
        action = secrets.choice(list(self.ACTION_PATTERNS.keys()))
        target = secrets.choice(self.ACTION_PATTERNS[action])
        return f"{action}{separator}{target}"

    def generate_compound_name(self, separator: str = "-") -> str:
        """
        Generate a compound descriptive thread name.

        Args:
            separator (str): String to separate words (default: "-").

        Returns:
            str: Name like "deep-dive" or "bright-spark".

        Example:
            >>> RandomThreadNameGenerator().generate_compound_name()
            'deep-reflection'
        """
        base, options = secrets.choice(self.COMPOUND_PATTERNS)
        complement = secrets.choice(options)
        return f"{base}{separator}{complement}"

    def random_name(self, separator: str = "-") -> str:
        """Generate a name with a randomly chosen pattern.

        Args:
            separator (str): String to separate words (default: "-").

        Returns:
            str: A name such as "engaging-discussion".
        """
        pattern = secrets.choice(
            [self.generate_simple_name, self.generate_action_name, self.generate_compound_name]
        )
        return pattern(separator)

    async def generate_name(self, messages: list[str]) -> str:
        """Return a random name; ``messages`` is ignored."""
        return self.random_name()


# Longest name ``FirstMessageThreadNameGenerator`` makes, in characters.
FIRST_MESSAGE_NAME_LENGTH = 60


def _cut_on_word(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters at a word boundary, with nothing added.

    A first word longer than ``limit`` has no boundary to cut at, so it is cut at ``limit``.
    """
    if len(text) <= limit:
        return text
    space = text.rfind(" ", 0, limit + 1)
    return text[:space] if space > 0 else text[:limit]


class FirstMessageThreadNameGenerator(ThreadNameGenerator):
    """Names a thread after its first message (``"thread_name_generator": "first_message"``).

    The API passes the user's messages first, so this is the user's first message whenever it
    has text. Whitespace is collapsed and the text is cut to at most
    ``FIRST_MESSAGE_NAME_LENGTH`` characters on a word boundary, adding nothing. Without any
    text the name is a random one, as ``RandomThreadNameGenerator`` makes.
    """

    async def generate_name(self, messages: list[str]) -> str:
        for message in messages:
            text = " ".join(str(message or "").split())
            if text:
                return _cut_on_word(text, FIRST_MESSAGE_NAME_LENGTH)
        return RandomThreadNameGenerator().random_name()
