"""Expressões cron de 5 campos e as ocorrências delas num fuso horário.

Implementação própria e pequena, sem dependências: o RaPA só precisa do cron
clássico, e o cálculo das ocorrências tem de ser determinístico (a mesma
expressão, o mesmo fuso e o mesmo instante dão sempre a mesma resposta).

A expressão tem cinco campos separados por espaço, na ordem de sempre::

    minuto  hora  dia-do-mês  mês  dia-da-semana
    0-59    0-23  1-31        1-12 0-7 (0 e 7 são domingo)

Cada campo é uma lista separada por vírgulas de ``*``, ``N``, ``N-M``, ``*/P``,
``N-M/P`` ou ``N/P`` (de ``N`` até o fim, de ``P`` em ``P``). Meses e dias da
semana também aceitam os nomes em inglês de três letras (``jan``, ``mon``...).
Valem ainda os atalhos ``@hourly``, ``@daily``, ``@weekly``, ``@monthly`` e
``@yearly`` (ou ``@annually``). Nada de segundos, ``?``, ``L``, ``W`` ou ``#``.

Regras, as mesmas do cron do Vixie (o dos Linux):

- **Dia do mês e dia da semana.** Se um dos dois campos começa com ``*``, o dia
  precisa casar com os dois (o que começa com ``*`` vale para qualquer dia, ou
  para os dias do passo, como ``*/2``); se os dois estão restritos, basta casar
  com um deles (``0 9 1 * 1`` roda no dia 1 e em toda segunda).
- **Hora local.** Os campos falam da hora do relógio no fuso da agenda
  (``America/Sao_Paulo`` por padrão). As ocorrências saem em UTC.
- **Horário que não existe** (o relógio adianta no início do horário de verão):
  a ocorrência acontece uma vez, no primeiro instante depois do pulo. Várias
  ocorrências dentro do pulo viram uma só.
- **Horário que se repete** (o relógio atrasa no fim do horário de verão): a
  ocorrência acontece uma vez, na primeira passagem.
- Uma expressão que nunca acontece (``0 0 31 2 *``) é recusada.

O Brasil não tem horário de verão desde 2019, mas o histórico do fuso continua
valendo para datas antigas, e outros fusos têm as duas transições.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "DEFAULT_TIMEZONE",
    "CronError",
    "CronExpression",
    "load_zone",
]

#: Fuso das agendas quando ninguém escolhe outro.
DEFAULT_TIMEZONE = "America/Sao_Paulo"

#: Até quando a próxima ocorrência é procurada (29 de fevereiro numa segunda-feira
#: pode demorar 28 anos).
_HORIZON_DAYS = 366 * 50
_MAX_TEXT = 200

_MONTH_NAMES = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_WEEKDAY_NAMES = {
    name: number for number, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
}
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
#: Dias máximos de cada mês (fevereiro com 29: existe nos anos bissextos).
_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_ZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")


class CronError(ValueError):
    """Expressão cron ou fuso inválido, com a mensagem em pt-BR."""


@dataclass(frozen=True, slots=True)
class _Field:
    name: str
    low: int
    high: int
    names: dict[str, int] | None = None


_FIELDS = (
    _Field("minuto", 0, 59),
    _Field("hora", 0, 23),
    _Field("dia do mês", 1, 31),
    _Field("mês", 1, 12, _MONTH_NAMES),
    _Field("dia da semana", 0, 7, _WEEKDAY_NAMES),
)


def _is_ascii_number(text: str) -> bool:
    return text.isascii() and text.isdigit()


def _number(text: str, field: _Field) -> int:
    lowered = text.lower()
    if field.names is not None and lowered in field.names:
        return field.names[lowered]
    # Só os dígitos ASCII: ``isdigit`` também aceita "²" e "٣", que o cron não conhece.
    if not _is_ascii_number(text) or len(text) > 2:
        raise CronError(f"{field.name}: valor inválido")
    value = int(text)
    if not field.low <= value <= field.high:
        raise CronError(f"{field.name}: use valores de {field.low} a {field.high}")
    return value


def _parse_field(text: str, field: _Field) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        if not item:
            raise CronError(f"{field.name}: há uma vírgula sobrando")
        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not _is_ascii_number(step_text) or len(step_text) > 2 or int(step_text) < 1:
                raise CronError(f"{field.name}: o passo depois da barra precisa ser de 1 a 99")
            step = int(step_text)
        if base == "*":
            low, high = field.low, field.high
        elif "-" in base:
            start, _, end = base.partition("-")
            low, high = _number(start, field), _number(end, field)
            if low > high:
                raise CronError(f"{field.name}: o intervalo precisa ir do menor ao maior")
        else:
            low = _number(base, field)
            high = field.high if slash else low
        values.update(range(low, high + 1, step))
    return frozenset(values)


def load_zone(name: str) -> ZoneInfo:
    """O fuso IANA pelo nome (``America/Sao_Paulo``), ou :class:`CronError`."""
    if not isinstance(name, str) or not _ZONE_NAME.fullmatch(name) or len(name) > 64:
        raise CronError("fuso horário inválido; use um nome como America/Sao_Paulo")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError, OSError:
        raise CronError("fuso horário desconhecido; use um nome como America/Sao_Paulo") from None


def _resolve(naive: datetime, zone: ZoneInfo) -> datetime:
    """O instante (UTC) em que o relógio do fuso marca ``naive``.

    Horário repetido: a primeira passagem. Horário que não existe: o primeiro
    instante depois do pulo do relógio.
    """
    first = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if first.astimezone(zone).replace(tzinfo=None) == naive:
        return first
    # No pulo, fold=0 usa o deslocamento de antes (um instante depois do pulo) e
    # fold=1 o de depois (um instante antes dele): o fim do pulo está entre os dois.
    second = naive.replace(tzinfo=zone, fold=1).astimezone(UTC)
    low, high = min(first, second), max(first, second)
    while high - low > timedelta(seconds=1):
        middle = low + (high - low) // 2
        middle = middle.replace(microsecond=0)
        if middle.astimezone(zone).replace(tzinfo=None) >= naive:
            high = middle
        else:
            low = middle
    return high


@dataclass(frozen=True, slots=True)
class CronExpression:
    """Uma expressão cron de 5 campos, já conferida."""

    text: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    #: Dias da semana de 0 (domingo) a 6 (sábado).
    weekdays: frozenset[int]
    day_star: bool
    weekday_star: bool

    @classmethod
    def parse(cls, text: str) -> CronExpression:
        """Confere e lê a expressão; levanta :class:`CronError` com o motivo em pt-BR."""
        if not isinstance(text, str):
            raise CronError("a expressão cron precisa ser um texto")
        cleaned = " ".join(text.split())
        if not cleaned or len(cleaned) > _MAX_TEXT:
            raise CronError(f"a expressão cron precisa ter de 1 a {_MAX_TEXT} caracteres")
        expanded = _MACROS.get(cleaned.lower(), cleaned)
        if expanded.startswith("@"):
            raise CronError(
                "atalho desconhecido; use @hourly, @daily, @weekly, @monthly ou @yearly"
            )
        parts = expanded.split(" ")
        if len(parts) != 5:
            raise CronError(
                "a expressão cron precisa de 5 campos: minuto, hora, dia do mês, mês e dia da semana"
            )
        minutes, hours, days, months, weekdays = (
            _parse_field(part, field) for part, field in zip(parts, _FIELDS, strict=True)
        )
        expression = cls(
            text=cleaned,
            minutes=tuple(sorted(minutes)),
            hours=tuple(sorted(hours)),
            days=days,
            months=months,
            weekdays=frozenset(day % 7 for day in weekdays),
            day_star=parts[2].startswith("*"),
            weekday_star=parts[4].startswith("*"),
        )
        if not expression._possible():
            raise CronError("a expressão nunca acontece (por exemplo, 31 de fevereiro)")
        return expression

    def _possible(self) -> bool:
        if not self.day_star and self.weekday_star:
            # Só o dia do mês decide: algum mês escolhido precisa ter um dos dias.
            smallest = min(self.days)
            return any(_MONTH_DAYS[month - 1] >= smallest for month in self.months)
        return True

    def matches_day(self, day: date) -> bool:
        """Se a expressão roda em algum horário deste dia (do calendário local)."""
        if day.month not in self.months:
            return False
        by_day = day.day in self.days
        by_weekday = (day.isoweekday() % 7) in self.weekdays
        if self.day_star or self.weekday_star:
            return by_day and by_weekday
        return by_day or by_weekday

    def next_after(self, after: datetime, zone: ZoneInfo) -> datetime | None:
        """A primeira ocorrência estritamente depois de ``after``, em UTC.

        ``after`` precisa ter fuso. Devolve ``None`` se não houver ocorrência nos
        próximos 50 anos.
        """
        if after.tzinfo is None:
            raise ValueError("o instante de referência precisa ter fuso horário")
        after_utc = after.astimezone(UTC)
        floor = after_utc.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
        start = floor.date()
        for offset in range(_HORIZON_DAYS):
            day = start + timedelta(days=offset)
            if not self.matches_day(day):
                continue
            for hour in self.hours:
                for minute in self.minutes:
                    naive = datetime(day.year, day.month, day.day, hour, minute)
                    # Um horário local antes do de ``after`` nunca é depois dele.
                    if naive < floor:
                        continue
                    instant = _resolve(naive, zone)
                    if instant > after_utc:
                        return instant
        return None

    def previous_before(self, before: datetime, zone: ZoneInfo) -> datetime | None:
        """A última ocorrência estritamente antes de ``before``, em UTC.

        É o espelho de :meth:`next_after`, com as mesmas regras do horário de verão: o
        instante de um horário local nunca diminui quando o horário cresce, então a
        busca de trás para a frente para no primeiro que cabe. ``before`` precisa ter
        fuso. Devolve ``None`` se não houver ocorrência nos 50 anos anteriores.
        """
        if before.tzinfo is None:
            raise ValueError("o instante de referência precisa ter fuso horário")
        before_utc = before.astimezone(UTC)
        local = before_utc.astimezone(zone)
        ceiling = local.replace(tzinfo=None)
        if local.fold:
            # Na segunda passagem de um horário repetido, os horários locais até o fim
            # da repetição rodam na primeira passagem, que é antes de ``before``.
            first_pass = local.replace(fold=0).utcoffset() or timedelta(0)
            ceiling += first_pass - (local.utcoffset() or timedelta(0))
        start = ceiling.date()
        for offset in range(_HORIZON_DAYS):
            day = start - timedelta(days=offset)
            if not self.matches_day(day):
                continue
            for hour in reversed(self.hours):
                for minute in reversed(self.minutes):
                    naive = datetime(day.year, day.month, day.day, hour, minute)
                    # Um horário local depois do teto nunca é antes de ``before``.
                    if naive > ceiling:
                        continue
                    instant = _resolve(naive, zone)
                    if instant < before_utc:
                        return instant
        return None

    def occurrences(
        self, after: datetime, until: datetime, zone: ZoneInfo, *, limit: int
    ) -> Iterator[datetime]:
        """Ocorrências em ``(after, until]``, em ordem, no máximo ``limit``."""
        current = after
        for _ in range(limit):
            following = self.next_after(current, zone)
            if following is None or following > until:
                return
            yield following
            current = following
