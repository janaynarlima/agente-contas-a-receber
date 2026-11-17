# Avaliação do agente no golden set sintético

## Conjunto medido
- **Conjunto:** `golden/` — 25 boletos **sintéticos** gerados com semente fixa (20261117). Nenhum dado real.
- **Composição por formato:** jpg: 2, pdf_escaneado: 4, pdf_texto: 13, png: 6
- **Layouts:** 4 aproximações sintéticas (rótulo acima do valor, tudo na mesma linha, blocos rotulados, terminologia antiga Sacado/Cedente). Não reproduzem o leiaute de nenhum banco real.
- **Medido em:** 17/11/2026 · código `0.4.0+6e1a2c4d` · Tesseract 5.5.3.20260724

## Resultados

| Métrica | Resultado |
|---|---|
| Acerto de CNPJ do pagador | 95% (21/22) |
| Acerto de nome do pagador | 92% (22/24) |
| Acerto de valor | 92% (22/24) |
| Acerto de vencimento | 92% (22/24) |
| **Falso aceite** (aceito sem revisão, mas errado ou obrigatório em revisão) | **0% (0/25)** |
| Casos obrigatórios em revisão que foram capturados | 100% (7/7) |
| Enviados para revisão (total) | 36% (9/25) |
| Revisão desnecessária (podia ser aceito) | 0% (0/16) |
| Falhas de leitura/OCR | 0 |
| Custo por boleto (LLM) | não se aplica: nenhum LLM é usado |
| Tempo por boleto — todos | média 0.28 s · mediana 0.02 s · p95 0.65 s · máx 0.81 s |
| Tempo por boleto — pdf_texto | média 0.01 s · mediana 0.00 s · p95 0.02 s · máx 0.02 s |
| Tempo por boleto — ocr (imagem e pdf escaneado) | média 0.57 s · mediana 0.57 s · p95 0.81 s · máx 0.81 s |

## Por caso

| Caso | Formato | Esperado | CNPJ | Nome | Valor | Venc. | Revisão | Falso aceite |
|---|---|---|---|---|---|---|---|---|
| pdf_texto_coluna | pdf_texto | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_escaneado_coluna | pdf_escaneado | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| png_coluna | png | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_texto_linha | pdf_texto | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_escaneado_linha | pdf_escaneado | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| png_linha | png | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_texto_blocos | pdf_texto | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_escaneado_blocos | pdf_escaneado | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| png_blocos | png | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_texto_sacado | pdf_texto | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_escaneado_sacado | pdf_escaneado | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| png_sacado | png | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| jpg_coluna | jpg | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| jpg_linha | jpg | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_texto_cnpj15 | pdf_texto | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| pdf_texto_pos2025 | pdf_texto | aceitar | ✓ | ✓ | ✓ | ✓ | não | não |
| neg_cnpj_invalido | pdf_texto | revisao | — | ✓ | ✓ | ✓ | sim | não |
| neg_vencimento_divergente | pdf_texto | revisao | ✓ | ✓ | ✓ | — | sim | não |
| neg_valor_divergente | pdf_texto | revisao | ✓ | ✓ | — | ✓ | sim | não |
| neg_linha_dv_invalido | pdf_texto | revisao | ✓ | ✓ | ✓ | ✓ | sim | não |
| neg_sem_linha_digitavel | pdf_texto | revisao | ✓ | ✓ | ✓ | ✓ | sim | não |
| neg_sem_pagador | pdf_texto | revisao | — | — | ✓ | ✓ | sim | não |
| neg_pagador_cpf | pdf_texto | revisao | — | ✓ | ✓ | ✓ | sim | não |
| ocr_ruim_ruido | png | indiferente | ✓ | ✗ | ✗ | ✗ | sim | não |
| ocr_ruim_baixa_resolucao | png | indiferente | ✗ | ✗ | ✗ | ✗ | sim | não |

## Como ler

- ✓ campo correto · ✗ campo errado ou ausente · — campo sem gabarito (não avaliado).
- *Esperado*: `aceitar` = tudo correto e sem revisão; `revisao` = o agente deve revisar; `indiferente` = correto ou revisão servem (OCR ruim).
- Quando o gabarito diz que o CNPJ não deve ser aceito (por exemplo, só existe o CNPJ do beneficiário), qualquer CNPJ extraído conta como erro.

## Limites desta medição

- São boletos sintéticos de poucos layouts; o resultado **não** estima o acerto em boletos reais.
- 25 casos é um conjunto pequeno: um caso muda a taxa em 4 ponto(s) percentual(is).
- O conjunto foi montado pelo mesmo projeto que o mede; quando uma falha é corrigida, o conjunto deixa de mostrá-la. Compare sempre com o relatório anterior.
