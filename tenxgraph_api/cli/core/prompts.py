"""Guided prompts for the 10xGraph CLI.

Every interactive question in the CLI goes through this service rather than
calling Questionary directly, so that prompts share one theme, one cancellation
contract, and one non-interactive policy. Commands describe *what* they need to
ask; how it is rendered, and what happens when there is nobody to answer, is
decided here.

Cancellation is a normal outcome, not an error: each method returns ``None`` when
the user presses Ctrl+C or Esc, and callers are expected to treat that as "the
user changed their mind" and exit cleanly.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import questionary
from prompt_toolkit.styles import Style

from tenxgraph_api.cli.core.theme import ACCENT, AMBER, MUTED, PENDING, SUCCESS
from tenxgraph_api.cli.exceptions import ValidationError


# Mirrors cli.core.theme, expressed in prompt_toolkit's style syntax. Question and typed
# text use the terminal's own foreground so prompts read on light and dark themes.
PROMPT_STYLE = Style(
    [
        ("qmark", f"fg:{AMBER} bold"),
        ("question", "bold"),
        ("answer", f"fg:{ACCENT} bold"),
        ("pointer", f"fg:{AMBER} bold"),
        ("highlighted", f"fg:{ACCENT} bold"),
        ("selected", f"fg:{SUCCESS} bold"),
        ("separator", f"fg:{PENDING}"),
        ("instruction", f"fg:{MUTED}"),
        ("text", ""),
        ("disabled", f"fg:{PENDING} italic"),
    ]
)

MULTI_SELECT_HINT = "space to toggle · a all · i invert · enter to confirm"
SINGLE_SELECT_HINT = "arrows to move · enter to select"

_UNPROMPTABLE = (
    "Could not open an interactive prompt on this terminal. "
    "Pass the value as a command-line flag instead."
)


def prompt_toolkit_available() -> bool:
    """Whether a full-screen-capable prompt can actually be driven here.

    ``stdin.isatty()`` is not sufficient on Windows: a stdin that reports as a
    TTY under an MSYS/Cygwin shell still leaves prompt-toolkit unable to attach
    to a console, and it raises rather than degrading. Probing once up front
    turns that into an ordinary "cannot prompt" answer.
    """
    try:
        from prompt_toolkit.input.defaults import create_input
        from prompt_toolkit.output.defaults import create_output

        create_input()
        create_output()
    except Exception:
        return False
    return True


def _accept_any(_selected: list[str]) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class Choice:
    """One option in a select or multi-select prompt."""

    value: str
    title: str
    description: str | None = None
    checked: bool = False
    disabled: str | None = None

    def to_questionary(self) -> questionary.Choice:
        """Render the option, keeping its description visually secondary."""
        label: list[tuple[str, str]] = [("class:text", self.title)]
        if self.description:
            label.append(("class:instruction", f"  {self.description}"))
        return questionary.Choice(
            title=label,
            value=self.value,
            checked=self.checked,
            disabled=self.disabled,
        )


class PromptService:
    """Ask the user questions, or explain why they cannot be asked."""

    def __init__(
        self,
        *,
        interactive: bool | None = None,
        on_answered: Callable[[], None] | None = None,
    ) -> None:
        if interactive is None:
            interactive = sys.stdin.isatty() and prompt_toolkit_available()
        self._interactive = interactive
        # Prompt-toolkit erases from the cursor to the end of the screen, which
        # reaches past a scrolling region and takes the pinned footer with it.
        # The owner of that chrome redraws it here once each prompt is done.
        self._on_answered = on_answered

    @property
    def interactive(self) -> bool:
        """Whether there is a user on the other end who can answer."""
        return self._interactive

    def require_interactive(self, *, field: str, alternatives: str) -> None:
        """Fail with a recoverable message when a prompt is impossible."""
        if self._interactive:
            return
        raise ValidationError(
            f"{field} is required and stdin is not interactive. {alternatives}",
            field=field,
        )

    def select(
        self,
        message: str,
        choices: Sequence[Choice],
        *,
        default: str | None = None,
        instruction: str | None = SINGLE_SELECT_HINT,
    ) -> str | None:
        """Pick exactly one option. Returns None if cancelled."""
        options = [choice.to_questionary() for choice in choices]
        default_option = next((o for o in options if o.value == default), None)
        return self._ask(
            lambda: questionary.select(
                message,
                choices=options,
                default=default_option,
                instruction=instruction,
                style=PROMPT_STYLE,
                qmark="?",
                use_shortcuts=False,
                use_indicator=False,
            )
        )

    def checkbox(
        self,
        message: str,
        choices: Sequence[Choice],
        *,
        instruction: str | None = MULTI_SELECT_HINT,
        validate: Callable[[list[str]], bool | str] | None = None,
    ) -> list[str] | None:
        """Toggle any number of options with space. Returns None if cancelled."""
        return self._ask(
            lambda: questionary.checkbox(
                message,
                choices=[choice.to_questionary() for choice in choices],
                instruction=instruction,
                style=PROMPT_STYLE,
                qmark="?",
                # Questionary requires a validator; accept anything by default.
                validate=validate or _accept_any,
            )
        )

    def confirm(self, message: str, *, default: bool = False) -> bool | None:
        """Ask a yes/no question. Returns None if cancelled."""
        return self._ask(
            lambda: questionary.confirm(message, default=default, style=PROMPT_STYLE, qmark="?")
        )

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Callable[[str], bool | str] | None = None,
    ) -> str | None:
        """Ask for a free-text value. Returns None if cancelled."""
        return self._ask(
            lambda: questionary.text(
                message,
                default=default,
                validate=validate,
                style=PROMPT_STYLE,
                qmark="?",
            )
        )

    def _ask(self, build: Callable[[], questionary.Question]) -> Any:
        """Build and run one question, restoring chrome it may have erased.

        The question is constructed inside the guard, not passed in ready-made:
        questionary builds its prompt-toolkit application eagerly, so a terminal
        it cannot drive raises during construction rather than during ``ask``.

        The answer type depends on the question, so each caller narrows it in
        its own return annotation.
        """
        try:
            return build().ask()
        except ValidationError:
            raise
        except Exception as exc:
            # A terminal that cannot host a prompt is a usage problem with a
            # clear fix, not an internal error — report it as one.
            raise ValidationError(_UNPROMPTABLE, field="input") from exc
        finally:
            if self._on_answered is not None:
                self._on_answered()
