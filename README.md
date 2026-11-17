# Agente local de contas a receber

Projeto incremental para leitura local de boletos. Esta entrega cobre leitura
de PDF e imagem, OCR com Tesseract e extração textual inicial dos dados.

## Requisitos

- Python 3.10 ou superior.
- Dependencias Python listadas em `requirements.txt`.
- Tesseract OCR instalado no sistema, com os dados do idioma portugues (`por`).
  Os documentos sao processados localmente; o Tesseract deve estar disponivel
  no `PATH`.

Instale as dependencias Python com:

```powershell
python -m pip install -r requirements.txt
```

Para executar os testes:

```powershell
python -m pytest
```

## Leitura de documentos

O modulo `recebiveis.extrator` aceita PDF, PNG, JPG e JPEG:

```python
from recebiveis.extrator import extrair_texto

texto = extrair_texto("entrada/boleto.pdf", idioma_ocr="por")
```

PDFs com texto selecionavel sao lidos diretamente. Paginas sem texto usam OCR
local. Imagens passam diretamente pelo Tesseract. O idioma padrao e configurado
em `config.yaml`, mas o carregamento dessa configuracao ainda nao faz parte
deste incremento; por enquanto, informe `idioma_ocr` ao chamar a funcao quando
precisar alterar o padrao.

Os testes criam documentos sinteticos e simulam a resposta do Tesseract, portanto
nao precisam de dados reais nem da instalacao do binario Tesseract para validar
o roteamento e a integracao com OCR.

## Extração inicial

`extrair_dados_boleto(texto)` recebe o texto extraido e retorna `DadosBoleto`,
com CNPJ/nome do pagador, valor (`Decimal`) e vencimento (`date`). CNPJ e nome
sao procurados somente no trecho iniciado pelo rotulo `Pagador` e encerrado
por `Beneficiario` ou `Cedente`; CNPJ encontrado nesse trecho e validado pelos
digitos verificadores. Valor e vencimento sao extraidos somente quando seus
rotulos aparecem explicitamente, na mesma linha ou na linha seguinte ao
rotulo. CNPJ com 15 digitos e zero a esquerda (formato de alguns bancos) tem o
zero removido e ainda passa pelos digitos verificadores. Campos ausentes
permanecem `None`. Dado invalido ou ausente nao levanta excecao: o motivo fica
em `DadosBoleto.motivos_revisao`, para o boleto seguir para revisao humana.

## Linha digitável e regra FEBRABAN

`extrair_dados_boleto(texto, referencia=None)` localiza a linha digitável (47
dígitos) ou, na falta dela, o código de barras (44 dígitos), valida e deriva
`valor` e `vencimento`. Ela é a fonte primária; os rótulos do texto só
confirmam. Divergência de valor ou vencimento entre texto e linha envia o
boleto para revisão, sem escolher sozinho. O código de barras (44 dígitos)
fica em `DadosBoleto.codigo_barras` e é o identificador do boleto (nunca o
nome do arquivo), base da reconciliação futura.

A regra está documentada no cabeçalho de `recebiveis/validador.py`:

- Linha digitável: 5 campos. DV dos campos 1 a 3 por módulo 10 (pesos 2,1
  da direita para a esquerda, produtos de dois dígitos somam os algarismos).
- DV geral do código de barras por módulo 11 (pesos 2 a 9; resto 0, 10 ou
  11 vira 1).
- Valor: 10 dígitos em centavos (zero = valor não informado).
- Fator de vencimento: dias desde 07/10/1997 (fator 0000). No fator 9999
  (21/02/2025) o ciclo reinicia em 1000 (22/02/2025); como o mesmo fator
  corresponde a datas 9000 dias distantes, usa-se a data mais próxima da
  `referencia` (hoje por padrão). Fator 0000 = sem vencimento.
- Fora de escopo: cobrança de arrecadação/convênio (48 dígitos).

Os testes usam um gerador sintético com os DVs implementados de forma
independente do código de produção.

## Configuração e estado (SQLite)

`recebiveis.config.carregar_config()` lê e valida o `config.yaml`. Os dias de
aviso (`avisos`) não têm valor padrão no código e não aceitam `0` (D0 não gera
aviso). Também são validados `confianca_minima` (0 a 1), `aprovacao_vencidos`,
`fase_validacao_concluida` (enquanto `false`, até o D-7 passa por aprovação),
`max_percentual_baixas`, `max_envios_por_execucao` e `feriados`. Em caminhos
com `\` use `/` ou aspas simples no YAML.

`recebiveis.estado.Estado` guarda boletos (`aberto`, `pago`, `em_revisao`, com
`data_baixa` e `motivo_baixa`) e avisos com chave única
`(boleto_id, tipo_aviso)`. O `boleto_id` é o código de barras, nunca o nome do
arquivo. A duplicidade é barrada pelo banco, e `confirmar_envio` é um `UPDATE`
condicional atômico: só uma execução consegue marcar o aviso como enviado.

O banco fica **fora do OneDrive** (`%LOCALAPPDATA%/recebiveis/estado.db`, em
`config.yaml`), porque o sincronizador pode travar ou corromper um SQLite
aberto. É a única escrita permitida fora do projeto. Dependência nova:
`ruamel.yaml` (o PyYAML não tem pacote para o Python 3.15 usado aqui).

## Cálculo de avisos

`recebiveis.avisos` decide, sem enviar nada:

- data prevista = vencimento + dias do `config.yaml`; se cair em sábado,
  domingo ou feriado, é empurrada para o próximo dia útil;
- D0 não gera aviso; aviso prévio (dias < 0) só vale antes do vencimento;
- no máximo um aviso por boleto por execução: o mais antigo ainda não tratado.
  Se ele for encaminhado à revisão, nada mais é enviado para o boleto nessa
  execução (o seguinte é avaliado na próxima);
- nunca dois avisos de vencido no mesmo dia, inclusive em execuções repetidas;
- atraso maior que 2 dias (contado a partir da data já ajustada) não envia:
  vai para revisão humana (`em_revisao` no estado);
- destino: vencidos vão para `fila_aprovacao` (`aprovacao_vencidos: true`); o
  D-7 também, até `fase_validacao_concluida: true`; fora disso, `enviar`;
- `max_envios_por_execucao`: os excedentes ficam `adiado`, sem mudar estado;
- boleto pago, em revisão ou sem vencimento não recebe aviso.

`planejar_execucao` cria a agenda de avisos no estado (idempotente) e encaminha
atrasos para revisão, mas não confirma envios. Enquanto a fila de aprovação não
existir, uma decisão `fila_aprovacao` se repete a cada execução até ser
resolvida; a fila (etapa futura) deve evitar entradas duplicadas.

## Reconciliação de baixa

`recebiveis.reconciliacao.reconciliar` roda antes dos avisos. Boleto pago =
arquivo que não está mais em `entrada/`, identificado pelo código de barras
(nunca pelo nome): renomear ou mover para subpasta não é pagamento.

- Lê `entrada/` recursivamente (PDF, PNG, JPG, JPEG; ignora `~$*`, ocultos e
  outros formatos), registra boletos novos e baixa os abertos que sumiram, com
  motivo "arquivo removido da pasta", cancelando os avisos pendentes.
- Travas: pasta inexistente, inacessível ou qualquer falha de leitura **abortam**
  a reconciliação sem baixar nada. Arquivo sem identificador confiável (linha
  digitável inválida) também **bloqueia as baixas**: sem identificá-lo, não dá
  para afirmar que um boleto saiu da pasta. Ele entra em revisão por hash do
  conteúdo (`doc:<sha256>`), que nunca recebe aviso nem baixa.
- Baixas acima de `max_percentual_baixas` (contra os boletos abertos) são
  bloqueadas e exigem confirmação (`confirmar_baixas_acima_do_limite`). Pasta
  vazia cai nessa trava.
- Boleto em revisão nunca é baixado. Boleto pago que reaparece volta a aberto,
  marcado para revisão humana, sem aviso automático.
- `boleto_ainda_na_entrada` confere de novo, imediatamente antes de um envio,
  se o boleto ainda está na pasta; na dúvida retorna `False` (não enviar).
- O resultado traz `eventos` prontos para o log, com o identificador mascarado.

## Clientes e e-mails (incremento 4a)

- `recebiveis.clientes` lê `clientes.csv` (`cnpj,nome,email_cobranca`) em modo
  somente leitura. CNPJ inválido, e-mail inválido ou CNPJ repetido são erros
  (a mensagem cita a linha, nunca o dado). Cliente sem e-mail é carregado com
  `email_cobranca = None`. O arquivo real tem dados pessoais e está no
  `.gitignore`; use `clientes.exemplo.csv` como modelo.
- `recebiveis.emailer` usa **somente** os templates Jinja de `templates/`
  (`aviso_previo.txt.j2`, `aviso_vencido.txt.j2`), em ambiente isolado
  (sandbox) e com `StrictUndefined`. Variáveis permitidas: `nome_cliente`,
  `valor`, `vencimento`, `numero_boleto` (a linha digitável, derivada do código
  de barras). O nome vem do cadastro, nunca do texto do boleto.
- Cada template é validado ao carregar: variável fora da lista, termo proibido
  (juros, multa, desconto, protesto, ameaça, urgência etc., sem distinção de
  acento ou caixa) ou ausência da frase "caso já tenha efetuado o pagamento,
  desconsidere esta mensagem." nos vencidos impedem o uso do template.
- Modo padrão é **dry-run**: `gravar_eml` grava o `.eml` em `saida/` e nada é
  enviado. `enviar_smtp` (STARTTLS, ou SSL na porta 465) só deve ser chamada com
  `--enviar` explícito; falha vira `ErroEnvio` sem dados pessoais, para o aviso
  voltar a pendente. Credenciais vêm do `.env`/ambiente (nunca versionados).
- `config.yaml` ganhou `saida`, `templates`, `clientes` e `bcc_financeiro`.

## Avaliação: golden set sintético e métricas

```powershell
python -m recebiveis.avaliacao gerar     # (re)gera golden/ (determinístico)
python -m recebiveis.avaliacao medir     # mede o agente e grava docs/avaliacao_golden.md
```

`golden/` tem 25 boletos **sintéticos** (semente fixa; nenhum dado real) em PDF
com texto, PDF escaneado, PNG e JPG, com 4 layouts aproximados, o CNPJ de 15
dígitos, o vencimento após o reinício do fator (2025), 7 casos que **devem** ir
para revisão (CNPJ inválido, vencimento e valor divergentes, linha digitável
inválida ou ausente, sem pagador, pagador pessoa física) e 2 de OCR ruim. O
gabarito está em `golden/gabarito.json`.

Métricas: acerto de CNPJ, nome, valor e vencimento; **falso aceite** (aceito sem
revisão, mas errado, ou que devia ir para revisão — a métrica de segurança);
revisão desnecessária; % enviado para revisão; tempo por boleto (com OCR e sem);
custo por boleto (não se aplica: nenhum LLM).

Resultados (conjunto: `golden/`, 25 boletos sintéticos; Tesseract 5.5):

| Métrica | Antes das correções | Depois |
|---|---|---|
| Falsos aceites | 4 de 25 (todos no layout "linha") | **0 de 25** |
| Revisão obrigatória capturada | 7 de 7 | 7 de 7 |
| Revisão desnecessária | 3 de 16 (layout "Sacado") | 0 de 16 |
| Acerto de nome | 15 de 24 | 22 de 24 |

Os dois erros de nome que restam e os demais campos errados são os 2 casos de OCR
ruim, que vão para revisão como esperado. Relatórios completos em
`docs/avaliacao_golden_antes.md` e `docs/avaliacao_golden.md`. **Cuidado:** o
conjunto foi montado pelo mesmo projeto que o mede, tem poucos layouts e 25
casos (cada caso vale 4 pontos percentuais). Não estima o acerto em boletos
reais; para isso é preciso medir em boletos reais com gabarito conferido.

## Modo oficial: tudo dentro do Docker (um banco só)

A tela web e o agente de terminal rodam no **mesmo container**, com o mesmo banco
(volume `recebiveis-dados`), a mesma pasta `entrada/`, fila, modelos e cadastro.
Assim o que você processa na tela aparece no terminal e vice-versa.

```powershell
docker compose up -d --build          # sobe a tela em http://127.0.0.1:8000
.\agente.ps1 run                      # ensaio (nada e enviado)
.\agente.ps1 run --enviar             # envio REAL
.\agente.ps1 aprovar                  # aprovar a fila (interativo)
.\agente.ps1 testar-email --para voce@exemplo.com
```

- `agente.ps1` chama `docker compose exec` e sobe o container se ele estiver parado.
  Sem terminal interativo (Agendador de Tarefas), ele adiciona o `-T` sozinho.
- O container usa o fuso `America/Sao_Paulo`: o "hoje" define o dia dos avisos.
- O `.env` é lido pelo Compose (`env_file`) e vira variáveis de ambiente do
  container; ele não entra na imagem. Depois de mudar o `.env`, rode
  `docker compose up -d` para o container ler os valores novos.
- Pastas montadas do projeto: `entrada/`, `saida/`, `fila_aprovacao/`, `logs/`
  (inclui `relatorio.md`), `templates/` e `docs/` (somente leitura; o cadastro é
  `docs/clientes.csv`). As variáveis `RECEBIVEIS_*` (entrada, banco, saída, fila,
  logs, relatório, modelos e clientes) sobrescrevem o `config.yaml`.
- **Agendar todo dia** (Agendador de Tarefas do Windows): programa
  `powershell.exe`, argumentos
  `-NoProfile -File "C:\caminho\do\projeto\agente.ps1" run` (acrescente
  `--enviar` só depois de validar o ensaio).
- Rodar o agente direto no Windows (fora do Docker) ainda funciona, mas usa outro
  banco (`%LOCALAPPDATA%`). Evite misturar os dois modos.

## Primeiro envio real: configurar e testar o SMTP

1. Preencha `.env` (já criado, fora do versionamento): `SMTP_HOST`, `SMTP_PORT`
   (587 = STARTTLS, 465 = SSL), `SMTP_USER`, `SMTP_PASS` (use senha de aplicativo
   quando o provedor exigir) e `EMAIL_REMETENTE`.
2. Valide só o SMTP, sem boletos nem clientes (**envio real de um e-mail**):

   ```powershell
   python -m recebiveis testar-email --para seu.email@exemplo.com
   ```

3. O cadastro de clientes é lido de `docs/clientes.csv` (`config.yaml`). CNPJ com
   15 dígitos e zero à esquerda é aceito (o zero é removido e os dígitos
   verificadores continuam valendo).
4. Rode `python -m recebiveis run` (dry-run) e confira os `.eml` em `saida/` e a
   fila em `fila_aprovacao/` antes de usar `--enviar`.

## Execução completa: `run` (incremento 4c)

```powershell
python -m recebiveis run              # DRY-RUN (padrão): nada é enviado
python -m recebiveis run --enviar     # envio REAL por SMTP
python -m recebiveis run --sem-llm    # sem LLM (o fallback ainda não existe)
```

Compatível com o Agendador de Tarefas do Windows e com o cron (código de saída
0 = ok, 1 = erro ou reconciliação abortada, 2 = configuração inválida).

Ordem da execução: **reconciliação de baixa → plano de avisos → fila de
aprovação / entrega**. Os avisos aprovados e ainda não enviados também saem
(`.eml` em dry-run, SMTP com `--enviar`). O limite `max_envios_por_execucao` vale
para as entregas; o excedente fica adiado.

**Trava de segurança:** se a reconciliação for abortada (pasta ausente,
inacessível, falha de leitura) ou tiver baixas bloqueadas (arquivo sem linha
digitável válida, ou baixa acima do limite), a fase de avisos **não roda**. Sem
saber o que já foi pago, o agente poderia cobrar quem pagou. Cadastro de
clientes ausente também impede os avisos (sem mandá-los para revisão em massa).

Saídas de cada execução:

- **Log JSON** em `logs/execucao-AAAA-MM-DD.jsonl` (uma linha por evento: entrada,
  extração, validação, decisão, aprovação, envio, baixa, reabertura, erro, fim).
  Todas as linhas trazem `execucao_id` (rastreio), `versao_codigo` (versão +
  hash do código-fonte) e `versao_prompt` (hash do arquivo `Prompt`). Um filtro
  de defesa mascara CNPJ, e-mail e código de barras; o identificador do boleto
  aparece como `***final#hash`, que distingue boletos com o mesmo final.
- **`relatorio.md`**: boletos processados, baixas (e bloqueadas), avisos
  cancelados por pagamento, revisões pendentes, erros e métricas (tempo total,
  tempo médio de leitura por boleto, custo de LLM). Declara que não há métrica
  de acerto enquanto o golden set não existir.

O identificador curto exibido nas telas e usado nos nomes de arquivo é um hash
(`#1d6a94e7`), não os últimos dígitos do código de barras: boletos do mesmo
beneficiário costumam terminar igual, o que faria os e-mails da fila se
sobrescreverem.

## Fila de aprovação e comando `aprovar` (incremento 4b)

Ciclo de um aviso: `pendente → aguardando_aprovacao → aprovado → enviado`.
Rejeitar cancela. Em dúvida (cliente sem e-mail, CNPJ fora do cadastro, valor
ou vencimento ausentes) o aviso vai para `em_revisao`.

- `recebiveis.fila.enfileirar` renderiza o e-mail (só pelos templates, com o
  nome do cadastro), grava o `.eml` em `fila_aprovacao/` e move o aviso para
  `aguardando_aprovacao`. A transição é atômica: não há entrada duplicada.
  Enquanto um aviso aguarda aprovação ou envio, os seguintes do mesmo boleto
  esperam (o mais antigo sai primeiro).
- `python -m recebiveis aprovar` mostra, para cada aviso: cliente, CNPJ e
  e-mail mascarados, valor, vencimento, dias na pasta e histórico de avisos.
  Opções: aprovar, rejeitar (com motivo), pular, sair. A decisão registra quem
  decidiu e quando (auditoria). `--listar` só mostra.
- **Sem `--enviar`, tudo é dry-run**: o aviso aprovado gera um `.eml` em
  `saida/` e continua `aprovado`. Com `--enviar` o envio é real (SMTP), mas só
  depois de conferir de novo que o boleto ainda está em `entrada/`, que ele
  segue aberto e que `EMAIL_REMETENTE` existe. A transição para `enviado` é
  atômica (execução repetida não reenvia). Se o SMTP falhar, o aviso volta a
  `aprovado` para a próxima tentativa.
- A baixa de um boleto cancela seus avisos pendentes e aprovações, e apaga o
  e-mail correspondente da fila.
- `python -m recebiveis reabrir <id>` (id curto `#...` da tela ou final do código,
  ≥ 6 caracteres, único) reabre um
  boleto baixado e o marca para revisão humana.
- **Migração do banco**: o esquema agora é versionado (`PRAGMA user_version`,
  atual 2). Bancos anteriores são migrados automaticamente na primeira abertura,
  preservando dados, chave única e integridade (há testes).

## Front-end e Docker

Interface web local para subir boletos em `entrada/` e acompanhar a leitura.

```powershell
docker compose up -d --build   # sobe em http://127.0.0.1:8000
docker compose logs -f         # acompanhar
docker compose down            # parar (o banco permanece no volume)
```

- A imagem inclui Tesseract com o idioma `por`; nada sai da máquina.
- Porta publicada **somente em 127.0.0.1**: a tela não tem login e não deve ser
  exposta na rede.
- `./entrada` é montada no container (os arquivos enviados aparecem na pasta do
  projeto). O banco SQLite fica no volume Docker `recebiveis-dados`, fora do
  OneDrive. Container sem root, sistema de arquivos somente leitura, sem
  capabilities.
- Envio valida extensão **e** bytes iniciais do arquivo, limita 20 MB por
  arquivo, sanitiza o nome, nunca sobrescreve e descarta conteúdo idêntico.
  POST exige token CSRF e Origin coerente; CSP restritiva; os arquivos de
  entrada nunca são servidos de volta. O CNPJ aparece mascarado.
- "Processar" roda a mesma reconciliação do agente (com as mesmas travas). A
  tela não oferece baixa manual nem confirmação de baixas em massa: isso
  continua exigindo decisão humana por outro canal. Nenhum e-mail é enviado.
- Sem Docker: `python -m recebiveis.web` (localhost:8000; precisa de
  Tesseract no PATH e `banco` no `config.yaml` com caminho válido).
- Variáveis: `RECEBIVEIS_CONFIG`, `RECEBIVEIS_ENTRADA`, `RECEBIVEIS_BANCO`,
  `RECEBIVEIS_SEGREDO` (chave de sessão; aleatória se ausente).

## Especificação e documentação de decisão

O arquivo `Prompt` é a fonte de verdade. Ele inclui, além das regras de
cobrança: fallback LLM com prompt de 5 componentes, temperature 0, grounding
e "não sei"; matriz de autonomia; métricas nas 5 dimensões; AgentOps/LLMOps
(alertas, rastreio, auditoria); ferramentas com schema; e orquestração
opcional em grafo (LangGraph) com memória de estado; e servidor MCP local
opcional, somente leitura/simulação (nunca envia, baixa ou aprova).

`docs/registro_decisao.md` reúne discovery, mapa de tarefas (critério dos
40%), abordagem, riscos e o mapa de cobertura da disciplina. Resultados de
desempenho ainda não foram medidos e não devem ser afirmados sem conjunto e
métrica.

## Limites deste incremento

Ainda nao existem: o fallback com LLM (`--sem-llm` apenas documenta isso), o
golden set de 20 boletos com as metricas de acerto, o grafo (LangGraph) e o
servidor MCP. O envio real (`--enviar`) so foi exercitado com SMTP simulado.
`clientes.exemplo.csv` contem apenas o cabecalho para evitar dados pessoais de
exemplo; crie `clientes.csv` (fora do versionamento) com `cnpj,nome,email_cobranca`.

Para envio real, copie `.env.exemplo` para `.env` e preencha `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` e `EMAIL_REMETENTE`; nunca adicione `.env`
ao controle de versao.

