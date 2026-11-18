# Plano do RaPA (versão consolidada)

Nada foi implementado. Os números de capacidade são estimativas até as medições da Fase 1.

## Como o RaPA funciona

1. **Demonstrar:** a pessoa faz a tarefa uma vez num navegador na nuvem, vendo-o ao vivo na tela dela.
2. **Generalizar:** o Qwen propõe parâmetros, nomes dos passos e repetições; a pessoa confirma cada um.
3. **Compilar:** código determinístico monta o robô como lista de passos em JSON versionada.
4. **Validar:** a reexecução vai até o último passo de leitura; o primeiro passo que grava algo roda numa execução supervisionada.
5. **Publicar:** o robô vira tool MCP e endpoint REST, com agenda, webhook e lote por planilha.
6. **Executar:** um interpretador nosso roda os passos sobre Playwright, sem LLM.
7. **Consertar:** o modelo escolhe entre candidatos enumerados para o passo que quebrou, e uma pessoa aprova a nova versão.

"Descrever a tarefa em texto" fica como assistente experimental, fora da promessa da v1.

## Decisões tomadas

| Tema | Decisão |
|---|---|
| Plataforma | EKS no AWS Outposts é a única peça da AWS; todo o resto é aberto e roda no cluster |
| Cluster | Local, nunca estendido; preferência pela arquitetura de 6 instâncias |
| Linguagem | Python no backend e na interface |
| Interface | NiceGUI 4.0; reserva: FastAPI + Jinja2 + htmx |
| LLM | Qwen 3.8 27B e gpt-oss-20b no hub interno, só para criar e consertar |
| SDK MCP | Pacote oficial `mcp` (projeto da Linux Foundation) |
| Navegador | Chromium do Debian 13 |
| Multiempresa | Plataforma compartilhada; a empresa é fronteira obrigatória; um usuário pertence a uma só empresa na v1 |
| Identidade | Convite pelo administrador da empresa, com MFA; provedor corporativo ligado quando a empresa exigir; SCIM na Fase 4 |
| Backup | Exceção para cópia fora do Outposts: discos semanais cifrados desde a Fase 1, exportação de cada empresa para o armazenamento dela, 3 servidores em outro prédio antes da Fase 3 |
| Cofre | 5 custodiantes de pelo menos 3 áreas, quórum de 3; desbloqueio manual nas Fases 1 e 2; PKCS#11 se houver HSM |
| Retenção (a validar com o jurídico) | Telas de execuções bem-sucedidas: 7 dias. Falhas e evidências: 90 dias, travadas. Vídeo: só em falhas. Backups: 30 dias |
| LGPD | A plataforma é operadora e cada empresa é controladora, com contrato padrão de tratamento |
| Rede | Acesso só pela rede corporativa; cerca de 240 Mbit/s para 50 pessoas assistindo ao vivo; /20 para pods agora, /19 previsto; saída dos robôs pelo NAT corporativo |
| Espera humana | No máximo 10 minutos dentro de uma execução; aprovações longas ficam como estado no banco |
| Operação | Time de plataforma com plantão, nomeado antes da Fase 1 |
| Licença | Apache-2.0, se o RaPA for publicado |

## Capacidade

A capacidade do Outposts é comprada em hosts inteiros, uma família de instância por host (a confirmar com a AWS). Por isso tudo usa a família m7i (m5 em racks de 1ª geração), e o que exige quórum fica em 3 hosts distintos.

| Item | Mínimo (piloto) | Recomendado |
|---|---|---|
| Carga-alvo | 30 navegadores ativos + 10 aquecidos (cerca de 10 mil execuções/dia) | 200 ativos + 50 aquecidos (cerca de 100 mil/dia) |
| Hosts | 3 hosts m7i | 4 ou mais hosts m7i |
| Plano de controle | 3 × m7i.xlarge | 3 × m7i.2xlarge |
| Nós de navegador | 3 × m7i.8xlarge: 84 vagas, 56 com um host fora | 12 × m7i.8xlarge: 336 vagas, 252 com um host fora |
| Nós de plataforma | 3 × m7i.8xlarge | 6 × m7i.4xlarge + 3 × m7i.4xlarge só para o banco |
| Total em uso | Cerca de 204 vCPU / 816 GiB | Cerca de 552 vCPU / 2.208 GiB |
| Armazenamento | Cerca de 5 TB | Cerca de 25 TB |
| Sub-rede dos pods | /22 | /20 |

- **Plano de controle:** os tamanhos acima valem para Outposts com EBS; com armazenamento local, ele usa 6 instâncias. O tamanho não muda depois de criado, então já nasce dimensionado para crescer.
- **Regra de escala:** cada m7i.8xlarge a mais adiciona cerca de 28 execuções simultâneas, e a cada 10 nós de navegador entra 1 de plataforma. Um milhão de execuções por dia pede de 1.700 a 2.200 vCPU, em vários racks.
- **Gatilho de compra:** pico acima de 70% das vagas por duas semanas, porque hardware tem prazo de entrega.
- **Lotes grandes:** podem rodar em janelas noturnas.
- **Registro de imagens:** Zot dentro do cluster, com as imagens pré-carregadas em todos os nós.

## Multiempresa e multiárea

- **Identidade:**
  - cada empresa é uma Organização no Keycloak, com o identificador dela no token;
  - os operadores da plataforma ficam num ambiente separado, com MFA e acesso de suporte com prazo;
  - na v1, cada usuário pertence a uma só empresa, e consultores recebem uma conta por empresa.
- **Permissões:** áreas, oito papéis e o custodiante de credenciais ficam em tabelas nossas, verificadas por uma única função. Ninguém aprova a própria versão de um robô.
- **Banco:** a empresa lidera toda chave, com isolamento por linha obrigatório em todas as tabelas, e o CI falha se surgir uma tabela sem política.
- **Cofre e arquivos:** cada empresa tem um namespace no OpenBao e um bucket próprio. Os arquivos de cada execução são cifrados com chave própria, e apagar a chave apaga os dados.
- **Execuções:** o pod fica ligado a uma empresa quando pega a execução, e cada execução tem credencial própria no proxy de saída.
- **Capacidade compartilhada:**
  - 10% das vagas ficam reservadas para gravação, controle ao vivo e testes;
  - cada empresa tem vagas garantidas, e cada área tem limite;
  - empréstimo de vagas ociosas até o dobro da garantia, só para robôs curtos;
  - prioridade: interativo, depois MCP e API, depois agenda, depois lote.
- **MCP:** um endpoint só; cada empresa vê apenas os próprios robôs, filtrados por área.
- **Vigilância contínua:** duas empresas fictícias rodam em produção tentando ler os dados uma da outra, com alarme se algo vazar. Há testes cruzados em todas as camadas no CI e teste de invasão externo antes de entrar a segunda empresa real.
- **Operação:**
  - entrada e saída de empresa são automatizadas; a saída congela, exporta, apaga e destrói as chaves;
  - a auditoria é encadeada por empresa;
  - o consumo é mostrado mensalmente por empresa e área.
- **Isolamento dedicado** (hosts e banco próprios) só entra na Fase 4.

## Interface em Python

- **Framework:** NiceGUI 4.0, com login pelo Keycloak.
- **Implantação:** roda separada da API, com afinidade por cookie no Envoy Gateway. As sessões ficam numa tabela do Postgres, sem Redis.
- **Interface fina:** toda regra e todo dado ficam na API, então trocar pela reserva custa pouco.
- **JavaScript que o time mantém, e só isso:** a visão ao vivo (imagem, mouse e teclado com acentos) e o envio direto de planilhas e certificados ao backend.
- **Segurança:** atualização mensal, acompanhamento dos alertas do projeto e lista de componentes proibidos.

## Dados no Postgres

- **Clusters:** dois clusters PostgreSQL 18 com CloudNativePG, um da aplicação e outro de identidade e cofre, cada um com 3 instâncias em 3 hosts e replicação síncrona.
- **Fila:** a tabela de execuções é a fila. Os limites por empresa, área, portal e credencial são aplicados na mesma transação que pega a execução.
- **Eventos:** ficam particionados por dia.
- **Componentes extras:** Valkey, NATS e ClickHouse só entram quando um gatilho medido for atingido.
- **Depois de qualquer restauração:** execuções não concluídas viram "indeterminado". Nada que já gravou no sistema de destino é repetido automaticamente.

## Backup e recuperação

| Etapa | O que existe | Se perder o rack |
|---|---|---|
| Fases 1 e 2 | Backup contínuo no armazenamento do rack, versionado e travado; discos semanais cifrados, um num cofre em outro prédio; exportação periódica de cada empresa para o armazenamento dela | Perde até 7 dias; cada empresa mantém sua exportação |
| Fase 3 em diante | O anterior, mais 3 servidores comuns em outro prédio recebendo cópia contínua | Perde até cerca de 15 minutos |

- **Rotina:** teste automático de restauração toda semana e alertas sobre a idade do último backup.
- **Separação de funções:** o time de operação guarda os discos e os custodiantes guardam as chaves, e não podem ser as mesmas pessoas.

## Qwen 3.8 27B e o hub

- **Papel do modelo:** propõe, e o código decide.
- **Antes de usar:** uma sonda verifica o hub e bloqueia o uso se algo mudar.
- **Chamadas:** o raciocínio fica desligado ou baixo, e as respostas em JSON são validadas.
- **Acordo com o time do hub (Fase 0):**
  - apelido fixo de versão e aviso antes de trocar o modelo;
  - controle de raciocínio por requisição;
  - saída JSON com esquema;
  - imagens;
  - teto de tokens maior.
- **Se o hub cair:** só a criação de robôs para; os publicados seguem rodando.

## Segurança

- **Isolamento:** cada execução roda em pod de uso único, sem privilégios, com gVisor, saindo só pelo proxy com os domínios declarados do robô.
- **Segredos:** ficam no OpenBao. O pod recebe só os da própria execução, e o modelo vê apenas marcadores.
- **Portão determinístico:** todo domínio, elemento e valor de um robô precisa vir de uma ação gravada, de um parâmetro declarado ou do cofre.
- **Certificado A1:**
  - o custodiante aprova cada par robô e portal;
  - robôs com A1 exigem dois aprovadores;
  - o conserto nunca é automático nessas sessões;
  - há alerta de vencimento.
- **Portais:** captcha fica de fora, e cada portal passa por liberação jurídica antes de ser automatizado.

## Bibliotecas e licenças

| Camada | Escolha | Licença |
|---|---|---|
| Base | Python 3.14, uv, ruff, mypy, pytest | Abertas |
| API e MCP | FastAPI, uvicorn, `mcp` 2.3 | MIT, BSD |
| Interface | NiceGUI 4.0, Authlib | MIT, BSD-3 |
| Entrada HTTP | Envoy Gateway | Apache-2.0 |
| Banco | PostgreSQL 18, CloudNativePG, PgBouncer | PostgreSQL, Apache-2.0, ISC |
| Acesso a dados | SQLAlchemy 2.1, psycopg 3, Alembic | MIT, LGPL-3.0 |
| Objetos e imagens | SeaweedFS 4.54, Zot | Apache-2.0 |
| Identidade e cofre | Keycloak 26.8, OpenBao 2.7 | Apache-2.0, MPL-2.0 |
| Backup | plugin Barman Cloud, restic, rclone | Apache-2.0, BSD-2, MIT |
| Navegador | Playwright 1.65, Chromium do Debian 13 | Apache-2.0, BSD |
| Isolamento | gVisor, smokescreen | Apache-2.0, MIT |
| Demais | cliente `openai`, CEL, bibliotecas de arquivos e certificado, OpenTelemetry, VictoriaMetrics, GlitchTip, Helm, Argo CD, OpenTofu | Abertas |

A credencial por execução no proxy pede uma pequena extensão em Go ao smokescreen. A Fase 0 compara essa opção com o Envoy autorizado por um serviço em Python.

## Fases

- **Fase 0, sem precisar do Outposts:**
  - **O que entra:** portais de teste no repositório; gravar, compilar e reexecutar; esqueleto com 2 empresas × 2 áreas e as empresas fictícias de vigilância.
  - **Primeiro experimento:** o gravador, nas semanas 1 e 2.
  - **Em paralelo:** acordo com o time do hub, perguntas enviadas à AWS, nomeação do dono da plataforma e dos custodiantes, e critério de escolha dos portais.
  - **Saída:** cada robô de teste roda 100 vezes seguidas sem LLM, os testes cruzados entre empresas passam e você aprova 2 ou 3 portais, incluindo pelo menos um com A1 e um lote.
- **Fase 1, mínimo no Outposts:**
  - **O que entra:** montagem com as respostas da AWS, discos semanais cifrados, runbooks e a primeira simulação de falha; as medições substituem as estimativas e o gatilho de compra é ajustado.
  - **Saída:** criação de pods a 3 vezes a carga-alvo por 1 hora sem erro, perda de um host mantendo 40 vagas, corte do link sem parar execuções e restauração completa testada.
- **Fase 2, piloto com 2 ou 3 empresas:**
  - **O que entra:** portais escolhidos, exportação por empresa, provedor corporativo ligado se alguma empresa exigir e teste de invasão antes da segunda empresa real.
  - **Saída:** 4 semanas em produção, restauração de uma empresa e simulação de saída de empresa sem vazamento.
- **Fase 3, configuração recomendada:**
  - **O que entra:** servidores de backup em outro prédio antes de começar; pedido de hardware pelos números medidos e cluster definitivo.
  - **Saída:** 72 horas com 200 navegadores simultâneos e 100 mil execuções por dia, e restauração a partir da cópia externa.
- **Fase 4, multiempresa avançado:** isolamento dedicado, SCIM, IP de saída fixo, usuários em várias empresas e desbloqueio automático do cofre.
- **Fase 5, rumo a 1 milhão por dia:** segundo rack ou Outposts. Componentes novos só entram por gatilho medido.

## Riscos e tratamento

| Risco | Tratamento |
|---|---|
| Tipo de Outposts desconhecido | Respostas da AWS por escrito antes da Fase 1; com EBS sem a arquitetura nova, Kubernetes 1.32 só com caminho de atualização e cluster em GitOps |
| Sem cópia fora do rack | Discos semanais e exportação por empresa desde a Fase 1; servidores em outro prédio antes da Fase 3 |
| Vazamento entre empresas | Empresa no esquema desde o início, testes cruzados no CI, empresas fictícias vigiando em produção, teste de invasão |
| Capacidade fixa | Medições na Fase 1, gatilho de compra, janelas noturnas para lotes |
| Vulnerabilidades do NiceGUI | Interface fina, atualização mensal, componentes proibidos |
| Hub do LLM | Acordo com o time do hub e sonda bloqueando mudanças |
| Gravador | Primeiro experimento, duas implementações, Playwright fixado e testes de contrato |
| Operar seis sistemas com estado | Dono com plantão, runbooks, simulação de falha por trimestre |
| Repetir transação irreversível | Passos classificados como leitura ou gravação; estado "indeterminado" resolvido por uma pessoa |
| Uso indevido do certificado A1 | Custodiante, dois aprovadores, conserto nunca automático, alerta de vencimento |
| Portais que bloqueiam automação | Captcha fora; liberação jurídica por portal |

## Pendências que dependem de outras pessoas

1. **AWS:** geração do rack, EBS ou armazenamento local, quantos hosts e vCPU por host, se existe cluster local de 6 instâncias no seu caso, e o prazo para adicionar hosts.
2. **Nomes:** o dono da plataforma, os 5 custodiantes e quem guarda os discos.
3. **Piloto:** quais empresas participam e quais portais cada uma indica.
4. **Jurídico:** retenção, contrato de tratamento e termos de uso dos portais.
5. **Time do hub:** o acordo da Fase 0.
6. **Time de rede:** faixas de endereço livres e banda.
7. **Locais físicos:** o cofre dos discos e o prédio para os servidores de backup.
