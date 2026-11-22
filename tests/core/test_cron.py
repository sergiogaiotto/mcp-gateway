"""Expressões cron: leitura, recusas e as ocorrências nos fusos (com e sem horário de verão)."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from rapa.core.cron import DEFAULT_TIMEZONE, CronError, CronExpression, load_zone

SAO_PAULO = load_zone(DEFAULT_TIMEZONE)
NEW_YORK = load_zone("America/New_York")
LORD_HOWE = load_zone("Australia/Lord_Howe")  # o relógio muda meia hora


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)  # type: ignore[misc]


def next_runs(text: str, after: datetime, zone: object, count: int = 3) -> list[datetime]:
    cron = CronExpression.parse(text)
    out: list[datetime] = []
    current = after
    for _ in range(count):
        found = cron.next_after(current, zone)  # type: ignore[arg-type]
        assert found is not None
        out.append(found)
        current = found
    return out


# -- leitura ---------------------------------------------------------------------


def test_fields_lists_ranges_steps_and_names() -> None:
    cron = CronExpression.parse("  */15  8-18/2 1,15 jan-mar,DEC mon-fri ")
    assert cron.text == "*/15 8-18/2 1,15 jan-mar,DEC mon-fri"
    assert cron.minutes == (0, 15, 30, 45)
    assert cron.hours == (8, 10, 12, 14, 16, 18)
    assert cron.days == {1, 15}
    assert cron.months == {1, 2, 3, 12}
    assert cron.weekdays == {1, 2, 3, 4, 5}
    assert not cron.day_star and not cron.weekday_star


def test_sunday_is_zero_or_seven_and_n_slash_step_goes_to_the_end() -> None:
    assert CronExpression.parse("0 0 * * 7").weekdays == {0}
    assert CronExpression.parse("0 0 * * 0,7").weekdays == {0}
    assert CronExpression.parse("50/5 * * * *").minutes == (50, 55)


@pytest.mark.parametrize(
    ("macro", "same_as"),
    [
        ("@hourly", "0 * * * *"),
        ("@daily", "0 0 * * *"),
        ("@weekly", "0 0 * * 0"),
        ("@monthly", "0 0 1 * *"),
        ("@yearly", "0 0 1 1 *"),
        ("@ANNUALLY", "0 0 1 1 *"),
    ],
)
def test_macros(macro: str, same_as: str) -> None:
    a, b = CronExpression.parse(macro), CronExpression.parse(same_as)
    assert (a.minutes, a.hours, a.days, a.months, a.weekdays) == (
        b.minutes,
        b.hours,
        b.days,
        b.months,
        b.weekdays,
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 13 *",
        "* * * * 8",
        "5-1 * * * *",
        "*/0 * * * *",
        "*/100 * * * *",
        "1,,2 * * * *",
        "a * * * *",
        "* * * foo *",
        "-1 * * * *",
        "@reboot",
        "0 0 L * *",
        "0 0 ? * *",
        "0 0 31 2 *",
        "0 0 30,31 2 *",
        "x" * 201,
        # Só dígitos ASCII: "²" passa no isdigit() e o int() dele levantava ValueError.
        "² * * * *",
        "*/² * * * *",
        "1-² * * * *",
        "0 ¹ * * *",
        "٣ * * * *",
        "*/٣ * * * *",
        "０ * * * *",
    ],
)
def test_invalid_expressions_are_refused_in_portuguese(text: str) -> None:
    with pytest.raises(CronError) as caught:
        CronExpression.parse(text)
    assert str(caught.value)


def test_february_29_is_possible() -> None:
    cron = CronExpression.parse("0 12 29 2 *")
    found = cron.next_after(utc(2026, 11, 21), SAO_PAULO)
    assert found == utc(2028, 2, 29, 15)


@pytest.mark.parametrize("name", ["", "Nowhere/City", "../etc/passwd", "/etc/localtime", "a b"])
def test_unknown_zones_are_refused(name: str) -> None:
    with pytest.raises(CronError):
        load_zone(name)


# -- ocorrências --------------------------------------------------------------------


def test_business_days_at_nine_in_sao_paulo() -> None:
    # Sexta-feira, 20 de novembro de 2026, às 18h em São Paulo (21h UTC).
    runs = next_runs("0 9 * * 1-5", utc(2026, 11, 20, 21), SAO_PAULO)
    assert runs == [utc(2026, 11, 23, 12), utc(2026, 11, 24, 12), utc(2026, 11, 25, 12)]


def test_next_is_strictly_after_and_ignores_seconds() -> None:
    cron = CronExpression.parse("30 10 * * *")
    exact = utc(2026, 5, 4, 13, 30)
    assert cron.next_after(exact, SAO_PAULO) == exact + timedelta(days=1)
    assert cron.next_after(exact - timedelta(seconds=1), SAO_PAULO) == exact
    assert cron.next_after(exact - timedelta(microseconds=1), SAO_PAULO) == exact


def test_naive_reference_is_refused() -> None:
    with pytest.raises(ValueError):
        CronExpression.parse("* * * * *").next_after(datetime(2026, 1, 1), SAO_PAULO)


def test_day_of_month_or_weekday_when_both_are_restricted() -> None:
    # Dia 1 de cada mês e toda segunda: basta casar com um dos dois.
    runs = next_runs("0 8 1 * 1", utc(2026, 11, 28, 12), SAO_PAULO, 4)
    assert [r.astimezone(SAO_PAULO).date().isoformat() for r in runs] == [
        "2026-11-30",
        "2026-12-01",
        "2026-12-07",
        "2026-12-14",
    ]


def test_starred_step_in_a_day_field_means_both_must_match() -> None:
    # "*/2" começa com "*": dias ímpares do mês que caem num domingo.
    cron = CronExpression.parse("0 0 */2 * 0")
    found = cron.next_after(utc(2026, 11, 1, 12), SAO_PAULO)
    assert found is not None
    local = found.astimezone(SAO_PAULO)
    assert local.day % 2 == 1 and local.isoweekday() == 7


def test_sao_paulo_had_daylight_saving_until_2019() -> None:
    # 4 de novembro de 2018: meia-noite não existiu (o relógio pulou para 1h).
    cron = CronExpression.parse("0 0 * * *")
    found = cron.next_after(utc(2018, 11, 3, 12), SAO_PAULO)
    assert found == utc(2018, 11, 4, 3)
    assert found.astimezone(SAO_PAULO).hour == 1
    # 16 de fevereiro de 2019: 23h30 aconteceu duas vezes; a agenda roda na primeira.
    cron = CronExpression.parse("30 23 * * *")
    first = cron.next_after(utc(2019, 2, 16, 12), SAO_PAULO)
    assert first == utc(2019, 2, 17, 1, 30)
    assert cron.next_after(first, SAO_PAULO) == utc(2019, 2, 18, 2, 30)
    # Desde 2019, sempre UTC-3.
    assert next_runs("0 0 * * *", utc(2026, 11, 1), SAO_PAULO, 1) == [utc(2026, 11, 1, 3)]


def test_spring_forward_gap_runs_once_at_the_end_of_the_jump() -> None:
    # Nova York, 8 de março de 2026: das 2h às 3h não existe.
    cron = CronExpression.parse("*/15 2 * * *")
    found = cron.next_after(utc(2026, 3, 8, 5), NEW_YORK)
    assert found == utc(2026, 3, 8, 7)  # 3h00 no horário de verão
    # As outras ocorrências do pulo não repetem: a próxima é no dia seguinte.
    assert cron.next_after(found, NEW_YORK) == utc(2026, 3, 9, 6)
    # 2h30 e 3h00 caem no mesmo instante e viram uma só.
    cron = CronExpression.parse("30 2 * * *")
    assert cron.next_after(utc(2026, 3, 8, 5), NEW_YORK) == utc(2026, 3, 8, 7)


def test_fall_back_repeated_hour_runs_once() -> None:
    # Nova York, 1º de novembro de 2026: 1h30 acontece às 5h30 e às 6h30 UTC.
    cron = CronExpression.parse("30 1 * * *")
    first = cron.next_after(utc(2026, 11, 1, 4), NEW_YORK)
    assert first == utc(2026, 11, 1, 5, 30)
    assert cron.next_after(first, NEW_YORK) == utc(2026, 11, 2, 6, 30)
    # Mesmo começando na segunda passagem, não roda de novo naquele dia.
    assert cron.next_after(utc(2026, 11, 1, 6, 0), NEW_YORK) == utc(2026, 11, 2, 6, 30)
    # De hora em hora, a 1h repetida também roda uma vez só (a segunda passagem, 6h UTC, não).
    hourly = next_runs("0 * * * *", utc(2026, 11, 1, 4, 30), NEW_YORK, 3)
    assert hourly == [utc(2026, 11, 1, 5), utc(2026, 11, 1, 7), utc(2026, 11, 1, 8)]


def test_half_hour_jump_in_lord_howe() -> None:
    # Lord Howe adianta meia hora às 2h de 4 de outubro de 2026 (de +10:30 para +11).
    cron = CronExpression.parse("10 2 * * *")
    found = cron.next_after(utc(2026, 10, 3, 12), LORD_HOWE)
    assert found is not None
    assert found.astimezone(LORD_HOWE).strftime("%H:%M") == "02:30"
    assert found == utc(2026, 10, 3, 15, 30)


def test_occurrences_between_are_ordered_and_limited() -> None:
    cron = CronExpression.parse("*/10 * * * *")
    start = utc(2026, 1, 1, 12, 0)
    found = list(cron.occurrences(start, start + timedelta(hours=1), SAO_PAULO, limit=100))
    assert found == [start + timedelta(minutes=10 * n) for n in range(1, 7)]
    assert len(list(cron.occurrences(start, start + timedelta(days=1), SAO_PAULO, limit=5))) == 5


def test_deterministic_for_the_same_reference() -> None:
    cron = CronExpression.parse("17 */3 * * *")
    reference = utc(2026, 7, 9, 10, 11, 12)
    assert {cron.next_after(reference, SAO_PAULO) for _ in range(20)} == {utc(2026, 7, 9, 12, 17)}


# -- a ocorrência anterior (a recuperação depois de o serviço ficar fora do ar) ------------


def _transitions(zone: object, year: int) -> list[datetime]:
    """Os instantes (UTC) em que o relógio do fuso muda, no ano."""
    found: list[datetime] = []
    moment = utc(year, 1, 1)
    offset = moment.astimezone(zone).utcoffset()  # type: ignore[arg-type]
    while moment.year == year:
        moment += timedelta(hours=1)
        current = moment.astimezone(zone).utcoffset()  # type: ignore[arg-type]
        if current != offset:
            found.append(moment)
        offset = current
    return found


@pytest.mark.parametrize(
    ("zone_name", "year"),
    [
        ("America/Sao_Paulo", 2018),
        ("America/New_York", 2026),
        ("Australia/Lord_Howe", 2026),
        ("Antarctica/Troll", 2026),
        ("America/Havana", 2026),
    ],
)
def test_previous_before_mirrors_next_after_across_clock_changes(zone_name: str, year: int) -> None:
    # Perto de cada mudança do relógio, a anterior a um instante é sempre a última da
    # cadeia de next_after antes dele, com as mesmas regras do horário de verão.
    zone = load_zone(zone_name)
    for text in ("*/7 * * * *", "30 0 * * *", "15,45 1,2 * * *", "59 23 * * *", "0 2 * * *"):
        cron = CronExpression.parse(text)
        for change in _transitions(zone, year):
            chain = list(
                cron.occurrences(
                    change - timedelta(days=2), change + timedelta(days=2), zone, limit=2000
                )
            )
            assert len(chain) >= 2, (text, change)
            probe = chain[0] + timedelta(seconds=1)
            while probe <= chain[-1]:
                expected = max(at for at in chain if at < probe)
                assert cron.previous_before(probe, zone) == expected, (text, zone_name, probe)
                probe += timedelta(minutes=11, seconds=13)
            for before, at in pairwise(chain):
                assert cron.previous_before(at, zone) == before, (text, zone_name, at)


def test_previous_before_is_strict_and_needs_a_zone() -> None:
    cron = CronExpression.parse("0 9 * * 1-5")
    friday_nine = utc(2026, 11, 20, 12)  # 9h em São Paulo
    assert cron.previous_before(friday_nine, SAO_PAULO) == utc(2026, 11, 19, 12)
    assert cron.previous_before(friday_nine + timedelta(seconds=1), SAO_PAULO) == friday_nine
    # De segunda de manhã, a anterior é a de sexta.
    assert cron.previous_before(utc(2026, 11, 23, 11), SAO_PAULO) == friday_nine
    with pytest.raises(ValueError):
        cron.previous_before(datetime(2026, 1, 1), SAO_PAULO)
    yearly = CronExpression.parse("0 0 29 2 *")
    assert yearly.previous_before(utc(2026, 11, 21), SAO_PAULO) == utc(2024, 2, 29, 3)
