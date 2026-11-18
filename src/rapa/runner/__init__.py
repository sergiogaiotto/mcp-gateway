"""Executor do RaPA: interpreta a lista de passos sobre Playwright, sem LLM.

Módulos:

- :mod:`rapa.runner.interpreter`: o interpretador determinístico;
- :mod:`rapa.runner.browser`: abertura do Chromium e do contexto de cada execução;
- :mod:`rapa.runner.ports`: segredos, arquivos, artefatos e eventos (com implementações locais);
- :mod:`rapa.runner.result`: o resultado de uma execução (RESULT.json);
- :mod:`rapa.runner.errors`: códigos de erro estáveis;
- :mod:`rapa.runner.totp`: códigos TOTP só com a biblioteca padrão;
- :mod:`rapa.runner.worker`: linha de comando ``python -m rapa.runner.worker``.

``result``, ``errors``, ``ports`` e ``totp`` não dependem do Playwright, para que o
plano de controle leia resultados sem instalar o navegador. Este pacote não importa
nada de propósito.
"""
