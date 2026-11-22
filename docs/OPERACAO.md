# Operação das empresas

Roteiro curto para o time da plataforma: a entrada de uma empresa, congelar, exportar e apagar. Os comandos são de `python -m rapa.control.admin`. As regras completas estão nas docstrings de `rapa.control.services.lifecycle`, `rapa.control.services.company` e da migração `v0007_operacao_empresas`.

Duas URLs de banco:

- a do papel da aplicação (`rapa_app`), em `--url` ou `RAPA_DATABASE_URL`, para a entrada e o consumo;
- a do dono das tabelas (`rapa_owner`), em `--url-dono` ou `RAPA_OWNER_DATABASE_URL`, para congelar, descongelar, exportar e apagar. Com a URL da aplicação no lugar, o comando recusa com uma mensagem clara.

`--por` diz quem pediu e vai para a auditoria (padrão: o usuário do sistema). Nenhum comando imprime segredos.

Exportar e apagar usam o mesmo armazenamento, as mesmas chaves e o mesmo cofre do servidor (`RAPA_ARMAZENAMENTO`, `RAPA_S3_*` com `AWS_ACCESS_KEY_ID` e `AWS_SECRET_ACCESS_KEY`, `RAPA_CHAVES`, `RAPA_COFRE` e `RAPA_BAO_*`; veja as regras do cofre em [CONTRIBUINDO.md](CONTRIBUINDO.md)). Com o OpenBao, o comando entra com o papel `rapa-operacao` (`RAPA_BAO_PAPEL=rapa-operacao`, num Job com a conta de serviço `rapa-operacao`; o preparo está em `deploy/openbao/preparar.sh`), que desembrulha as chaves das execuções (exportar) e apaga os segredos e a chave transit da empresa (apagar), sem nunca ler o valor de um segredo.

## 1. Entrada

1. Crie a Organização da empresa no Keycloak e anote o id dela (ou o apelido).
2. Crie a empresa, com quem vai administrá-la e as primeiras áreas, numa transação só:

```bash
uv run python -m rapa.control.admin empresa criar "Nome da empresa" --organizacao ID_DA_ORGANIZACAO --admin SUB_DO_ADMIN --nome-admin "Nome da pessoa" --area "Fiscal" --area "Compras" --vagas 2 --fila 1000 --emprestimo 2.0
```

O comando imprime o id da empresa. A organização fica ligada à empresa no banco (`companies.idp_organization`): o serviço passa a aceitar os tokens dela em até 60 segundos, sem reiniciar. O mapa estático `RAPA_ORGANIZATION_ALIASES` continua valendo; se ele e o banco discordarem sobre uma organização, o login é recusado.

O formato diz se `--organizacao` é o id ou o apelido: um valor no formato de UUID é sempre o id da organização (guardado em minúsculas, como o Keycloak o põe no token) e é comparado só com o id do token; qualquer outro valor é o apelido e é comparado só com o apelido. Prefira o id. Uma organização cujo apelido tem o formato de UUID só pode ser ligada pelo id.

3. Quem entra pela primeira vez com um token dessa organização vira usuário sem papel nenhum e só vê o pedido para um administrador dar um papel. O administrador da empresa dá os papéis na tela **Empresa** (ou em `PUT /v1/empresa/usuarios/{id}/papeis`).

## 2. Consumo do mês

```bash
uv run python -m rapa.control.admin empresa consumo ID_DA_EMPRESA --mes 2026-11
```

`--json` imprime o mesmo relatório da API (`GET /v1/empresa/consumo?mes=2026-11`). O mês é o do fuso `America/Sao_Paulo`, pela data em que cada execução foi pedida.

## 3. Saída

A saída é sempre nesta ordem: congelar, exportar, entregar o arquivo, apagar.

### Congelar

```bash
uv run python -m rapa.control.admin empresa congelar ID_DA_EMPRESA --motivo "contrato encerrado"
```

Daí em diante a empresa lê tudo, mas nada novo começa (a API responde 423, `empresa_congelada`): nem execução (REST, MCP, interface, agenda, webhook, lote), nem versão, publicação (também a aprovação de um robô com certificado A1), agenda, webhook, lote, sessão de gravação ou conserto, nem registrar, renovar ou autorizar um certificado. O banco também recusa a linha nova nessas tabelas, mesmo de um caminho que não passe pelos serviços. Revogar uma credencial ou uma autorização do custodiante continua valendo (só tira acesso), e as evidências continuam abertas para quem vê a execução. As execuções que estavam na fila são canceladas (código `empresa_congelada`); as que estavam rodando terminam normalmente (o despachante nem pega o que sobrar na fila: a conferência do certificado A1 nem chega a rodar). As agendas registram cada ocorrência como pulada (`empresa_congelada`). Enquanto estiver congelada, nada da empresa expira na retenção (telas, vídeos, arquivos baixados, retratos, chaves de idempotência e a partição de eventos em que ela tem eventos) e ela não recebe aviso de vencimento do certificado: tudo fica como estava para a exportação. Para voltar:

```bash
uv run python -m rapa.control.admin empresa descongelar ID_DA_EMPRESA
```

### Exportar

```bash
uv run python -m rapa.control.admin empresa exportar ID_DA_EMPRESA --saida empresa.tar.gz
```

O arquivo nasce com permissão 0600 e nunca sobrescreve outro. Links simbólicos nas pastas da empresa nunca são seguidos (nem um arquivo trocado por um link no meio da exportação), e o que entra no arquivo é exatamente o que o manifesto confere. Dentro dele: `manifesto.json` (o SHA-256 e o número de linhas de cada arquivo), `tabelas/<tabela>.jsonl` (uma linha por registro, de toda tabela com `company_id`, mais a da empresa: também as credenciais, as autorizações do custodiante, as aprovações e os avisos de vencimento do A1, e as execuções com as marcas de artefatos expirados) os arquivos de entrada da empresa em `arquivos/` (de `RAPA_PASTA_ARQUIVOS/<empresa>` ou `--pasta-arquivos`) e os artefatos em `artefatos/<execução>/<tentativa>/<nome>` (as telas, os vídeos e os arquivos baixados que a retenção ainda não apagou, lidos pelo armazenamento configurado: a pasta `RAPA_PASTA_ARTEFATOS` ou `--pasta-artefatos` e, no S3, os objetos decifrados, como a pessoa os vê pela API; um artefato que não abre fica de fora e é contado em `artefatos_ilegiveis` no manifesto). Material de chave nunca sai: o sal dos webhooks e as chaves embrulhadas das execuções (`run_keys.wrapped_key`) vêm como `null`; de uma credencial sai só a referência ao cofre e os metadados (o valor nunca passa pelo RaPA). Confira o manifesto antes de entregar:

```bash
tar -xzf empresa.tar.gz && sha256sum tabelas/*.jsonl
```

### Apagar

Só uma empresa congelada, sem execuções rodando nem sessões de gravação abertas, e com o nome exato:

```bash
uv run python -m rapa.control.admin empresa apagar ID_DA_EMPRESA --confirmar "Nome da empresa"
```

Numa transação só, o comando apaga todas as linhas da empresa, das filhas para as mães (inclusive as do certificado A1, as das evidências e as chaves das execuções), e por fim a empresa; ainda dentro dela, apaga os artefatos dela no armazenamento e, com o OpenBao, os segredos (todas as versões, em `empresas/<empresa>/`) e a chave transit `empresa-<uuid>` (sem a chave da empresa, nada que tenha sobrado num backup do S3 abre), e grava em `platform_events` o registro `empresa.apagada` (o id, o SHA-256 do nome, as contagens por tabela, quantos artefatos e o que saiu do cofre, só contagens, quem pediu e quando). Se o S3 ou o cofre falharem, nada do banco é apagado: repita o comando. Antes de apagar, o comando confere que a configuração alcança tudo o que a empresa tem fora do banco e, se não alcança, recusa sem apagar nada, dizendo o que falta: artefatos cifrados no S3 pedem `RAPA_ARMAZENAMENTO=s3` e `RAPA_S3_*`; chaves do transit, `RAPA_CHAVES=openbao`; credenciais registradas, `RAPA_COFRE=openbao` (com `RAPA_BAO_*` e o papel `rapa-operacao`). Se os segredos das credenciais não estão no OpenBao (o arquivo de desenvolvimento), diga com `--segredos-fora-do-openbao` (ou rode com `RAPA_COFRE=dev`): o registro `empresa.apagada` conta quantas credenciais ficaram com o segredo fora (`segredos_fora_do_openbao`). Depois, a pasta dos arquivos de entrada. A mensagem final diz quantos artefatos e quais pastas saíram (confira que são as do servidor). Se o comando parar depois de apagar as linhas (a pasta dos arquivos de entrada que não sai, por exemplo por permissão), corrija e rode o mesmo comando de novo, com o mesmo nome: ele confere o registro `empresa.apagada` e termina a pasta, os artefatos e as chaves (o que já saiu conta zero). Não tem volta: a exportação é a única cópia.

Para conferir depois:

```sql
SELECT at, action, actor, data FROM platform_events ORDER BY id DESC LIMIT 5;
```

## Resíduos

- Os comandos antigos (`empresa NOME`, `area` e `usuario`) continuam valendo, auditados (com `--por`), mas sem o vínculo com a organização.
- Uma organização que ainda não estava ligada fica "não ligada" no cache de cada processo por até 60 segundos.
- As travas do banco aceitam apagar só pelo dono das tabelas com a marca `rapa.purge_company`: quem tem a senha do dono continua podendo apagar à mão (como sempre pôde, desligando as travas).
- Sem o OpenBao, nenhuma chave por empresa existe para destruir (o comando só registra); as chaves dos webhooks derivadas do sal somem com as linhas.
- Uma exclusão que falha depois de o S3 ou o cofre terem apagado uma parte deixa o banco inteiro; a repetição apaga o resto, e o registro conta só o que sobrou.
- Uma empresa que fica congelada por muito tempo segura a remoção das partições de eventos em que tem eventos (as das outras empresas ficam junto, porque a partição é de todas).

## Observabilidade e saúde

As variáveis valem para cada processo de `python -m rapa.server` (as regras estão em `docs/CONTRIBUINDO.md`, "Regras da observabilidade e da saúde"):

- `RAPA_METRICAS_PORTA` (padrão 9100; 0 desliga): a porta do `GET /metrics`, no mesmo endereço do serviço (o `--host`; no pod, `--host 0.0.0.0`) e nunca publicada pelo gateway; o coletor (VictoriaMetrics) lê direto do pod. Não pode ser a porta do serviço.
- `RAPA_METRICAS_POR_EMPRESA=1`: as execuções concluídas, a fila e os avisos do A1 ganham o rótulo `empresa` (o uuid). Desligado por padrão, para não multiplicar as séries.
- `RAPA_LOG_JSON=1`: um objeto JSON por linha (`hora`, `nivel`, `logger`, `mensagem` e `rastreio`).
- `OTEL_EXPORTER_OTLP_ENDPOINT` (e, se preciso, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` e as outras variáveis padrão do exportador OTLP/HTTP), `OTEL_SERVICE_NAME` (padrão `rapa`): os traços. Precisam do extra `observability`.
- `RAPA_GLITCHTIP_DSN`: as exceções não tratadas, limpas, no GlitchTip. Precisa do extra `observability`. O DSN tem a chave do projeto: guarde-o num Secret.

Sondas: vida em `/saude`, prontidão em `/saude/pronto` (503 com os nomes do que falhou: `banco`, `banco_despachante`, `despachante` e, com o cofre e o armazenamento de objetos, os deles). Para conferir à mão:

```bash
curl -s http://127.0.0.1:8000/saude/pronto
curl -s http://127.0.0.1:9100/metrics | grep -E '^rapa_(fila|despachante_saudavel|a1_)'
```

Alertas que valem a pena: `rapa_despachante_saudavel == 0`; `rapa_despachante_etapa_ok == 0` por mais de alguns minutos; `rapa_fila_espera_maxima_segundos` alta (fila cheia); `time() - rapa_banco_lido_em_segundos > 120` (o despachante não lê o banco); `rapa_a1_dias_para_vencer < 7`; `rapa_a1_em_aviso{tipo="vencido"} > 0`; aumento de `rapa_webhook_entregas_total{resultado="assinatura_invalida"}`; `rapa_http_pedidos_total{codigo=~"5.."}`. Com mais de um despachante, a leitura da fila é a mesma em todos (`max`) e as execuções concluídas somam (`sum`).

