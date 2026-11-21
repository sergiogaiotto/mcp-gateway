"""Tradução das mensagens de entrada em comandos do CDP (``Input.*``).

Funções puras: cada uma devolve a lista de ``(método, parâmetros)`` que o
hospedeiro manda ao navegador. Como o ``keyboard.type`` do Playwright:

- um caractere do teclado americano (letras sem acento, dígitos, pontuação e
  espaço) vira ``keyDown`` com o texto e ``keyUp``, para a página ver as teclas
  (máscaras que escutam ``keydown`` e ``keypress``);
- qualquer outro texto (acentos, ``ç``, teclas mortas já compostas, IME, colar)
  vai inteiro por ``Input.insertText``, que gera ``beforeinput`` e ``input`` com
  o texto certo, seja qual for o teclado de quem demonstra;
- teclas especiais (Enter, Tab, setas, Backspace...) e atalhos com Ctrl, Alt ou
  Meta vêm como ``tecla``; uma tecla desconhecida é ignorada.

Os modificadores seguem o CDP: Alt = 1, Ctrl = 2, Meta = 4, Shift = 8.
"""

from dataclasses import dataclass, field
from typing import Any

from rapa.session.protocol import KeyMessage, MouseMessage, TextMessage, WheelMessage

__all__ = [
    "ALT",
    "CTRL",
    "META",
    "SHIFT",
    "Command",
    "MouseState",
    "key_commands",
    "text_commands",
    "wheel_commands",
]

ALT, CTRL, META, SHIFT = 1, 2, 4, 8

type Command = tuple[str, dict[str, Any]]

_BUTTONS = {"nenhum": "none", "esquerdo": "left", "meio": "middle", "direito": "right"}
#: Bit de cada botão em ``buttons`` (como em ``MouseEvent.buttons``).
_BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}


@dataclass(frozen=True, slots=True)
class _Key:
    key: str
    code: str
    key_code: int
    text: str | None = None
    location: int = 0


def _special_keys() -> dict[str, _Key]:
    keys = {
        "Enter": _Key("Enter", "Enter", 13, "\r"),
        "Tab": _Key("Tab", "Tab", 9),
        "Backspace": _Key("Backspace", "Backspace", 8),
        "Delete": _Key("Delete", "Delete", 46),
        "Escape": _Key("Escape", "Escape", 27),
        "ArrowUp": _Key("ArrowUp", "ArrowUp", 38),
        "ArrowDown": _Key("ArrowDown", "ArrowDown", 40),
        "ArrowLeft": _Key("ArrowLeft", "ArrowLeft", 37),
        "ArrowRight": _Key("ArrowRight", "ArrowRight", 39),
        "Home": _Key("Home", "Home", 36),
        "End": _Key("End", "End", 35),
        "PageUp": _Key("PageUp", "PageUp", 33),
        "PageDown": _Key("PageDown", "PageDown", 34),
        "Insert": _Key("Insert", "Insert", 45),
    }
    for n in range(1, 13):
        keys[f"F{n}"] = _Key(f"F{n}", f"F{n}", 111 + n)
    return keys


_SPECIAL = _special_keys()

#: Pontuação do teclado americano: caractere → (código da tecla, keyCode, com Shift).
_PUNCTUATION: dict[str, tuple[str, int, bool]] = {
    " ": ("Space", 32, False),
    "-": ("Minus", 189, False),
    "_": ("Minus", 189, True),
    "=": ("Equal", 187, False),
    "+": ("Equal", 187, True),
    "[": ("BracketLeft", 219, False),
    "{": ("BracketLeft", 219, True),
    "]": ("BracketRight", 221, False),
    "}": ("BracketRight", 221, True),
    "\\": ("Backslash", 220, False),
    "|": ("Backslash", 220, True),
    ";": ("Semicolon", 186, False),
    ":": ("Semicolon", 186, True),
    "'": ("Quote", 222, False),
    '"': ("Quote", 222, True),
    ",": ("Comma", 188, False),
    "<": ("Comma", 188, True),
    ".": ("Period", 190, False),
    ">": ("Period", 190, True),
    "/": ("Slash", 191, False),
    "?": ("Slash", 191, True),
    "`": ("Backquote", 192, False),
    "~": ("Backquote", 192, True),
}
_SHIFTED_DIGITS = dict(zip(")!@#$%^&*(", "0123456789", strict=True))


def _us_key(char: str) -> tuple[_Key, bool] | None:
    """A tecla do teclado americano que produz ``char`` (e se precisa de Shift)."""
    if len(char) != 1:
        return None
    if "a" <= char <= "z":
        return _Key(char, f"Key{char.upper()}", ord(char.upper()), char), False
    if "A" <= char <= "Z":
        return _Key(char, f"Key{char}", ord(char), char), True
    if "0" <= char <= "9":
        return _Key(char, f"Digit{char}", ord(char), char), False
    if char in _SHIFTED_DIGITS:
        digit = _SHIFTED_DIGITS[char]
        return _Key(char, f"Digit{digit}", ord(digit), char), True
    if char in _PUNCTUATION:
        code, key_code, shift = _PUNCTUATION[char]
        return _Key(char, code, key_code, char), shift
    return None


def _key_event(kind: str, key: _Key, modifiers: int, *, repeat: bool = False) -> Command:
    params: dict[str, Any] = {
        "type": kind,
        "key": key.key,
        "code": key.code,
        "windowsVirtualKeyCode": key.key_code,
        "nativeVirtualKeyCode": key.key_code,
        "modifiers": modifiers,
        "autoRepeat": repeat,
        "location": key.location,
    }
    if kind == "keyDown" and key.text is not None:
        params["text"] = key.text
        params["unmodifiedText"] = key.text
    return "Input.dispatchKeyEvent", params


def text_commands(message: TextMessage) -> list[Command]:
    """Texto digitado ou colado: teclas para um caractere americano, ``insertText`` para o resto."""
    text = message.texto.replace("\r\n", "\n")
    if len(text) == 1:
        found = _us_key(text)
        if found is not None:
            key, shift = found
            modifiers = SHIFT if shift else 0
            return [
                _key_event("keyDown", key, modifiers),
                _key_event("keyUp", key, modifiers),
            ]
        if text == "\n":
            enter = _SPECIAL["Enter"]
            return [_key_event("keyDown", enter, 0), _key_event("keyUp", enter, 0)]
    return [("Input.insertText", {"text": text})]


#: Atalhos de caractere que chegam ao navegador da nuvem: só os de edição (Ctrl ou Meta, sem
#: Alt). Os outros são comandos do próprio Chromium (Ctrl+T, Ctrl+N, Ctrl+H, Ctrl+J...) que
#: abrem páginas internas (chrome://), e da caixa da nova aba se chega a file:// da máquina.
_EDIT_SHORTCUTS = frozenset("acvxyz")
#: Teclas especiais que o Chromium trata como comando do navegador (com qualquer Alt ou Meta,
#: Shift+Esc, Ctrl+Shift+Delete e as teclas de função, menos F5).
_BROWSER_KEYS = frozenset({f"F{n}" for n in range(1, 13)} - {"F5"})


def _browser_command(message: KeyMessage) -> bool:
    modifiers = message.modificadores
    if modifiers & (ALT | META):
        # Alt+setas (voltar e avançar) ficam; Alt+Home abre a página inicial (chrome://).
        return not (modifiers == ALT and message.tecla in {"ArrowLeft", "ArrowRight"})
    if message.tecla in _BROWSER_KEYS:
        return True
    if message.tecla == "Escape" and modifiers & SHIFT:
        return True
    return message.tecla == "Delete" and modifiers & (CTRL | SHIFT) == CTRL | SHIFT


def key_commands(message: KeyMessage) -> list[Command]:
    """Tecla especial ou atalho. Teclas de caractere sem Ctrl, Alt ou Meta chegam como ``texto``."""
    modifiers = message.modificadores
    special = _SPECIAL.get(message.tecla)
    if special is not None:
        if _browser_command(message):
            return []
        if message.acao == "descer":
            kind = "keyDown" if special.text is not None else "rawKeyDown"
            return [_key_event(kind, special, modifiers, repeat=message.repetida)]
        return [_key_event("keyUp", special, modifiers)]
    found = _us_key(message.tecla) if message.tecla != " " else None
    if found is None or not modifiers & (CTRL | ALT | META):
        # Um caractere sem atalho viria como texto; o resto não tem tradução segura.
        return []
    letter = message.tecla.lower()
    if (
        modifiers & ALT
        or letter not in _EDIT_SHORTCUTS
        or (modifiers & SHIFT and letter not in "vz")
    ):
        return []
    key, _ = found
    # Atalho (Ctrl+A, Ctrl+Z...): a tecla sem texto, para o navegador tratar o comando.
    shortcut = _Key(key.key, key.code, key.key_code)
    if message.acao == "descer":
        return [_key_event("rawKeyDown", shortcut, modifiers, repeat=message.repetida)]
    return [_key_event("keyUp", shortcut, modifiers)]


@dataclass
class MouseState:
    """Botões apertados agora (o CDP pede o conjunto em cada evento)."""

    pressed: set[str] = field(default_factory=set)

    def buttons(self) -> int:
        return sum(_BUTTON_BITS[b] for b in self.pressed)

    def commands(self, message: MouseMessage, *, width: int, height: int) -> list[Command]:
        """Evento de mouse dentro da área visível (as coordenadas são cortadas nas bordas)."""
        x = min(message.x, max(width - 1, 0))
        y = min(message.y, max(height - 1, 0))
        button = _BUTTONS[message.botao]
        params: dict[str, Any] = {"x": x, "y": y, "modifiers": message.modificadores}
        if message.acao == "mover":
            params.update(type="mouseMoved", button="none", buttons=self.buttons())
            # Num arraste, o CDP quer o botão que está apertado.
            for candidate in ("left", "middle", "right"):
                if candidate in self.pressed:
                    params["button"] = candidate
                    break
            return [("Input.dispatchMouseEvent", params)]
        if button == "none":
            return []
        clicks = max(1, message.cliques)
        if message.acao == "apertar":
            self.pressed.add(button)
            params.update(
                type="mousePressed", button=button, buttons=self.buttons(), clickCount=clicks
            )
        else:
            self.pressed.discard(button)
            params.update(
                type="mouseReleased", button=button, buttons=self.buttons(), clickCount=clicks
            )
        return [("Input.dispatchMouseEvent", params)]


def wheel_commands(message: WheelMessage, *, width: int, height: int) -> list[Command]:
    """Rolagem com a roda do mouse no ponto informado."""
    return [
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": min(message.x, max(width - 1, 0)),
                "y": min(message.y, max(height - 1, 0)),
                "deltaX": message.dx,
                "deltaY": message.dy,
                "modifiers": message.modificadores,
            },
        )
    ]
