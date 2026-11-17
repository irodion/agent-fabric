"""The `Asker` a person answers: `questionary`'s lists, picked with the arrow keys and space (docs/design.md §4.11).
Only `kb setup` imports this module, so the hooks and notes never load `questionary`."""

from collections.abc import Callable
from typing import Any

import questionary

from agent_fabric.setup import Quit


class QuestionaryAsker:
    def __init__(self, **session: Any) -> None:
        self.session = session  # prompt_toolkit's `input` and `output`, for tests

    def _ask(self, question: questionary.Question) -> Any:
        try:
            answer = question.ask()
        except EOFError as e:  # Ctrl-D, or input that ends
            raise Quit from e
        if answer is None:  # Ctrl-C
            raise Quit
        return answer

    def checkbox(self, message: str, choices: list[str], checked: list[str]) -> list[str]:
        listed = [questionary.Choice(choice, checked=choice in checked) for choice in choices]
        return list(self._ask(questionary.checkbox(message, listed, **self.session)))

    def select(self, message: str, choices: list[str], default: str) -> str:
        return str(self._ask(questionary.select(message, choices, default=default, **self.session)))

    def confirm(self, message: str, default: bool) -> bool:
        return bool(self._ask(questionary.confirm(message, default=default, **self.session)))

    def text(self, message: str, validate: Callable[[str], str | None]) -> str:
        return str(
            self._ask(questionary.text(message, validate=lambda typed: validate(typed) or True, **self.session))
        )

    def tell(self, message: str) -> None:
        questionary.print(f"  {message}", style="fg:ansibrightblack", output=self.session.get("output"))
