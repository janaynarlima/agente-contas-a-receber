# Registro de decisão — Agente de contas a receber

Rascunho inicial. Itens marcados como **a definir** dependem de medição ou do stakeholder; não preencha com números inventados.

## 1. Discovery

- **Problema:** reduzir o esforço manual e o esquecimento no acompanhamento de boletos a vencer, sem aumentar o risco de cobrança indevida.
- **Número-meta:** a definir (ex.: tempo gasto por semana hoje vs. com o agente). Só registrar após medir.
- **Decisão apoiada:** quando avisar, quem avisar, se o envio é automático ou exige aprovação humana.
- **Stakeholder:** responsável financeiro. Define `confianca_minima`, `max_percentual_baixas` e o limite de envios por execução.

## 2. Mapa de tarefas e critério dos 40%

| Tarefa | Classificação |
|---|---|
| Ler boleto (PDF/imagem) e extrair campos | Automatizável |
| Validar CNPJ, linha digitável, código de barras | Automatizável |
| Calcular datas de aviso | Automatizável |
| Gerar e-mail por template | Automatizável |
| Reconciliar baixa (arquivo removido de `entrada/`) | Automatizável |
| Enviar aviso de vencido | Parcial (humano aprova) |
| Tratar boleto em revisão / reabertura | Parcial (humano revisa) |
| Negociar dívida, conceder prazo ou desconto | Não automatizável |
| Tratar disputa ou cliente sensível | Não automatizável |
| Alterar cadastro de cliente | Não automatizável |

Expostas à automação (automatizável + parcial): 7 de 10 (70%), acima de 40%. Classificação ilustrativa, a ser validada com o stakeholder. Cenário desejado: **augmentation** — o agente assume a triagem e o analista foca em exceções e negociação.

## 3. Abordagem e critério decisivo

- Determinístico primeiro (linha digitável/código de barras, regra FEBRABAN, dígitos verificadores): barato, auditável e verificável.
- LLM apenas como fallback (layout incomum), com temperature 0, JSON validado e grounding no texto. Critério: o determinístico falhou.
- O LLM nunca escreve e-mail, decide envio, aprova ou baixa. Critério: custo do erro alto e e-mail regulado por template.

- **MCP (Could):** servidor local somente leitura/simulação sobre o registro de ferramentas. Critério: o registro com schema já existe e o MCP só acrescenta uma interface. Ferramentas de ação (enviar, baixar, aprovar) ficam fora, porque um cliente LLM contornaria a aprovação humana. Só após o núcleo testado.

## 4. Dados

Boletos sintéticos (golden set de pelo menos 20), `clientes.exemplo.csv` sem dados reais. Nenhum dado real em testes ou repositório.

## 5. Riscos e controles

| Risco | Controle |
|---|---|
| Cobrar quem já pagou (falso positivo em ação) | Reconciliação antes dos avisos; aprovação de vencidos; verificação do arquivo antes do envio |
| Baixa em massa por falha de pasta | Aborto da reconciliação; `max_percentual_baixas` |
| CNPJ do beneficiário confundido com o do pagador | Extração restrita ao trecho do pagador; teste específico |
| Injeção de prompt via conteúdo do boleto | Conteúdo tratado como dado; saída validada por schema e grounding |
| Vazamento de dados (LGPD) | OCR local; mascaramento em logs; anonimização antes do LLM; privilégio mínimo |
| Cliente MCP (LLM) acionar envio ou aprovação, ou tratar dado do boleto como instrução | Servidor expõe só leitura/simulação; retornos mascarados e estruturados; ações só pela CLI |
| Aviso duplicado | Chave única `(boleto_id, tipo_aviso)` no SQLite |

## 6. Métricas (a medir no golden set)

Qualidade (acerto por campo, % em revisão, falso positivo de ação), latência por boleto, custo por boleto (se houver LLM), alucinação (% de campos sem lastro) e concordância agente x revisor. **Resultados atuais: não medidos.**

## 7. Ciclo, tipos de agente e autonomia

Ver "CICLO DO AGENTE, TIPOS E AUTONOMIA" no arquivo `Prompt`.

## 8. Mapa de cobertura da disciplina

| Aula | Onde aparece |
|---|---|
| 1 | Decisão apoiada, pipeline até operação, golden set e métricas |
| 2 | Abordagem híbrida, temperature 0, prompt injection, LGPD |
| 3 | Mapa de tarefas, critério dos 40%, augmentation, humano no circuito |
| 4 | Ciclo percepção-ação, tipos de agente, limites de autonomia |
| 5 | Prompt de 5 componentes, 5 dimensões de output, limiar 0,8, registro de decisão |
| 6 | Gatilho, ferramentas com schema, memória (estado e SQLite), grafo opcional, MCP opcional (Tool Registry/Schema) |
| 7 | Discovery, MoSCoW, AgentOps/LLMOps, quem aprova o quê |
