"""A chave transit da empresa (criada pelo primeiro embrulho, sem exportar) e a destruição
das chaves na saída, com as políticas de cada papel (o OpenBao falso as aplica)."""

import asyncio
import os
from pathlib import Path

import pytest

from rapa.control.keys import KeyUnavailable, TransitCompanyKeys, WrappedKey
from rapa.control.vault import OpenBaoKeyDestroyer
from rapa.core.ids import new_id

from .fakebao import OPERATION_ROLE, FakeBao
from .support import bao_client


async def test_the_company_key_is_created_by_the_first_wrap_never_exportable(
    tmp_path: Path,
) -> None:
    """Com a política do despachante: o encrypt cria a chave ("create"); nada em keys/."""
    bao = FakeBao()
    client = bao_client(bao, tmp_path)
    keys = TransitCompanyKeys(client)
    company, other, run = new_id(), new_id(), new_id()
    data_key = os.urandom(32)
    try:
        first = await keys.wrap(company, run, data_key)
        second = await keys.wrap(company, new_id(), os.urandom(32))
        assert first.wrapped.startswith(b"vault:v1:") and first.version == 1
        assert data_key not in first.wrapped
        assert await keys.unwrap(company, run, first) == data_key
        key = bao.keys[f"empresa-{company}"]
        assert (key.exportable, key.plaintext_backup) == (False, False)
        # O despachante nunca escreve no caminho das chaves (lá, todo POST é "update").
        assert not [path for _, path in bao.requests if path.startswith("rapa-transit/keys/")]
        assert bao.refusals == []
        assert second.wrapped != first.wrapped
        # A chave de outra empresa não desembrulha (nem existe, nem é a mesma).
        await keys.wrap(other, run, os.urandom(32))
        with pytest.raises(KeyUnavailable):
            await keys.unwrap(other, run, first)
        with pytest.raises(KeyUnavailable):
            await keys.unwrap(company, run, WrappedKey(b"local1:abc", 1))
    finally:
        await client.aclose()


async def test_two_dispatchers_wrapping_for_a_new_company_at_once(tmp_path: Path) -> None:
    """O primeiro encrypt cria a chave; o que chega junto já a encontra ("update")."""
    bao = FakeBao()
    one, two = bao_client(bao, tmp_path), bao_client(bao, tmp_path)
    company = new_id()
    runs = [new_id() for _ in range(6)]
    data = [os.urandom(32) for _ in runs]
    try:
        wrapped = await asyncio.gather(
            *(
                TransitCompanyKeys(client).wrap(company, run, key)
                for client, run, key in zip([one, two] * 3, runs, data, strict=True)
            )
        )
        for run, key, sealed in zip(runs, data, wrapped, strict=True):
            assert await TransitCompanyKeys(two).unwrap(company, run, sealed) == key
    finally:
        await one.aclose()
        await two.aclose()
    assert list(bao.keys) == [f"empresa-{company}"]
    assert bao.keys[f"empresa-{company}"].exportable is False and bao.refusals == []


async def test_destroy_takes_every_secret_of_the_company_and_its_transit_key(
    tmp_path: Path,
) -> None:
    """Com a política da operação (a do comando de apagar)."""
    bao = FakeBao()
    client = bao_client(bao, tmp_path, role=OPERATION_ROLE)
    company, other = new_id(), new_id()
    for path in ("portal", "fiscal/sefaz/a1", "fiscal/sefaz/senha", "a/b/c/d/e"):
        bao.put_secret(f"empresas/{company}/{path}", {"password": "x"})
    bao.put_secret(f"empresas/{other}/portal", {"password": "y"})
    dispatcher = bao_client(bao, tmp_path)
    keys = TransitCompanyKeys(dispatcher)
    try:
        for who in (company, other):
            await keys.wrap(who, new_id(), os.urandom(32))
        counts = await OpenBaoKeyDestroyer(client).destroy(company)
        assert counts == {"segredos": 4, "chaves_transit": 1}
        assert list(bao.secrets) == [f"empresas/{other}/portal"]
        assert set(bao.keys) == {f"empresa-{other}"}
        # A exclusão foi liberada antes (o OpenBao recusa apagar uma chave sem isso).
        config = [i["body"] for i in bao.bodies if i["path"].endswith(f"{company}/config")]
        assert config == [{"deletion_allowed": "true"}]
        # De novo, ou numa empresa sem nada no cofre: nada a apagar, sem erro.
        assert await OpenBaoKeyDestroyer(client).destroy(company) == {
            "segredos": 0,
            "chaves_transit": 0,
        }
        only_transit = OpenBaoKeyDestroyer(client, secrets=False)
        assert await only_transit.destroy(other) == {"chaves_transit": 1}
        assert list(bao.secrets) == [f"empresas/{other}/portal"]
        assert bao.refusals == []
    finally:
        await client.aclose()
        await dispatcher.aclose()
