from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class Form(ModalScreen[dict[str, str] | None]):
    """Small, validated recovery prompts that preserve the video and output path."""

    DEFAULT_CSS = """
    Form { align: center middle; background: #10151bcc; }
    Form #dialog { width: 68; max-width: 95%; height: auto; padding: 1 2;
                   background: #18222b; border: round #71dfba; }
    Form .heading { color: #71dfba; text-style: bold; margin-bottom: 1; }
    Form .hint { color: #9cadbb; height: auto; margin-bottom: 1; }
    Form .field-label { color: #d8e2eb; height: 1; }
    Form Input { margin-bottom: 1; }
    Form #form-error { color: #f4a59b; height: auto; }
    Form Button { width: 100%; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, heading, hint, fields, *, validate=None, focus=None):
        super().__init__()
        self.heading = heading
        self.hint = hint
        self.fields = fields
        self.validate_values = validate
        self.focus_field = focus or fields[0][0]
        self.AUTO_FOCUS = f"#{self.focus_field}"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.heading), classes="heading")
            yield Static(Text(self.hint), classes="hint")
            for name, label, value, placeholder in self.fields:
                yield Static(Text(label), classes="field-label")
                yield Input(value=value, placeholder=placeholder, id=name)
            yield Static("", id="form-error")
            yield Button("Continue", id="continue", variant="success")
            yield Static("Enter to continue · Esc to go back", classes="hint")

    @on(Input.Submitted)
    def next_field(self, event):
        event.stop()
        names = [field[0] for field in self.fields]
        index = names.index(event.input.id)
        if index + 1 < len(names):
            self.query_one(f"#{names[index + 1]}", Input).focus()
        else:
            self.submit()

    @on(Button.Pressed, "#continue")
    def submit(self):
        values = {name: self.query_one(f"#{name}", Input).value.strip() for name, *_ in self.fields}
        try:
            if self.validate_values:
                self.validate_values(values)
        except ValueError as exc:
            self.query_one("#form-error", Static).update(Text(str(exc)))
            return
        self.dismiss(values)

    def action_cancel(self):
        self.dismiss(None)
