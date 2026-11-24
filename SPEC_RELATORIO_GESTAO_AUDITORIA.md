# Especificação: Relatório de Gestão / Auditoria Completa

## Objetivo
Criar um relatório que sirva como **espelho fiel da loja** para auditoria — o auditor deve poder basear-se nele sem precisar fazer retrabalho. Deve cobrir **tudo** o que aconteceu no período: quem não pagou, desfalques, diferenças de estoque, vendas abaixo/acima do preço, anomalias, responsáveis, etc.

---

## 1. Período e Cabeçalho Obrigatório

### 1.1 Identificação do Período
- **Exibir sempre**: "Período: DD/MM/YYYY a DD/MM/YYYY"
- **Indicar modo**: "Mês atual" / "Mês anterior" / "Semestre" / "Personalizado"
- **Data/hora geração**: "Gerado em DD/MM/YYYY HH:MM por [Usuário]"
- **Status do período**: ABERTO / FECHADO (se há fecho mensal)

### 1.2 Dados da Empresa
- Nome, NUIT, Morada, Contacto, Email, Moeda
- Logo da empresa

---

## 2. Veredito Executivo (Resumo de Alto Nível)

### 2.1 Classificação do Período
| Nível | Critério | Ação |
|-------|----------|------|
| **CRÍTICO** | Diferença de caixa < 0 (faltam valores) OU desfalque confirmado OU estoque negativo | Investigação imediata |
| **ALERTA** | Diferenças não justificadas > tolerância OU vendas abaixo custo OU contagens sem justificativa | Verificação prioritária |
| **ATENÇÃO** | Conferências pendentes OU ajustes sem motivo OU produtos com custo zero | Acompanhar |
| **OK** | Tudo conferido, sem diferenças, sem anomalias | Arquivar |

### 2.2 KPIs Resumo (Cards)
- Vendas totais / Custo / Lucro / Margem %
- Compras totais
- Recebido (vista + crédito cobrado) vs Esperado
- Diferença total de recebimentos (MT)
- A receber (valor + nº faturas) / Em atraso
- Estoque: Inicial + Compras - Custo Vendas - Perdas/Ajustes = Final (Fecha? Sim/Não)
- Nº de anomalias detectadas por categoria

---

## 3. Secção 1: Vendas — Análise Completa

### 3.1 Resumo Geral
- Total vendido, Custo, Lucro bruto, Margem %
- Nº vendas / Nº itens vendidos
- Ticket médio

### 3.2 Por Forma de Pagamento
| Forma | Vendas (MT) | Qtd Vendas | % do Total |
|-------|-------------|------------|------------|
| Dinheiro | | | |
| Cartão | | | |
| M-Pesa / e-Mola | | | |
| Cheque | | | |
| Crédito (fatura) | | | |
| **Sem forma indicada** | | | |

### 3.3 Vendas a Crédito (Faturas)
- Lista de faturas emitidas no período
- Para cada: nº fatura, cliente, data emissão, vencimento, valor, status (Aberta/Parcial/Liquidada/Cancelada), dias em atraso
- **Faturas em atraso**: destaque vermelho com dias e valor

### 3.4 Vendas Anuladas
- Código, data, cliente, valor, motivo, **quem vendeu**, **quem anulou**, data anulação
- Itens da venda anulada (produto, qtd, preço, valor)
- **Regra**: só anula se não tem recebimentos

### 3.5 Vendas Abaixo do Custo (Prejuízo)
- Código venda, data, vendedor, produtos, valor venda, custo, **prejuízo (MT)**, margem %
- **Alerta** se margem < 0

### 3.6 Vendas Acima do Preço de Tabela (Possível Desvio)
- Vendas onde preço_unitario > preco_venda do produto + tolerância (ex: 5%)
- Código, produto, preço tabela, preço praticado, diferença %, vendedor

### 3.7 Vendas Sem Forma de Pagamento
- Código, data, valor, vendedor — **Risco: dinheiro não rastreado**

### 3.8 Top Vendedores
- Por valor, por lucro, por nº vendas

### 3.9 Top Produtos Vendidos
- Por quantidade, por receita, por lucro

---

## 4. Secção 2: Compras / Entradas — Análise Completa

### 4.1 Resumo Geral
- Total comprado, Nº entradas, Nº fornecedores

### 4.2 Por Fornecedor
| Fornecedor | Nº Entradas | Valor Total (MT) | % | Última Entrada |
|------------|-------------|------------------|---|----------------|

### 4.3 Entradas Sem Nota Fiscal
- Data, produto, qtd, valor, usuário — **Risco: entrada não documentada**

### 4.4 Entradas Com Preço Acima do Último Custo
- Produto, último custo, custo praticado, diferença %, fornecedor, usuário

### 4.5 Entradas de Ajuste de Inventário (Positivas)
- Data, produto, qtd, valor, motivo, usuário aprovador

---

## 5. Secção 3: Recebimentos — Conferência Caixa vs Sistema (O CORE DA AUDITORIA)

### 5.1 Por Canal de Recebimento
| Canal | Vendas à Vista (MT) | Dívidas Cobradas (MT) | **Esperado Sistema** | **Conferido (Caixa/Banco)** | **Diferença** | Status | Justificativa |
|-------|---------------------|----------------------|---------------------|----------------------------|---------------|--------|---------------|
| Dinheiro | | | | [INPUT] | AUTO | ✅/⚠️/❌ | |
| Cartão | | | | [INPUT] | AUTO | | |
| M-Pesa | | | | [INPUT] | AUTO | | |
| e-Mola | | | | [INPUT] | AUTO | | |
| Cheque | | | | [INPUT] | AUTO | | |
| Transferência | | | | [INPUT] | AUTO | | |
| **Sem forma** | | | | — | — | ⚠️ | Não rastreável |

### 5.2 Regras de Interpretação da Diferença
- **Diferença < 0 (FALTAM)**: Sistema regista MAIS que o contado → **Possível desfalque, venda não cobrada, erro de lançamento**
- **Diferença > 0 (SOBRAM)**: Contado MAIS que sistema → **Venda não registada, erro de lançamento, troco não conferido**
- **Diferença = 0**: Conferido OK

### 5.3 Recebimentos de Crédito (Recibos)
- Lista detalhada: nº recibo, fatura, cliente, data, valor, forma, usuário
- Total recibos vs Total faturas a receber no período

### 5.4 Contas a Receber — Envelhecimento
| Faixa | Nº Faturas | Valor (MT) | % |
|-------|------------|------------|---|
| A vencer (0-30d) | | | |
| 31-60 dias | | | |
| 61-90 dias | | | |
| 90+ dias | | | |
| **Total em atraso** | | | |

### 5.5 Clientes Devedores (Top 20 por valor em atraso)
- Cliente, NUIT, telefone, email, total em atraso, nº faturas, fatura mais antiga

---

## 6. Secção 4: Estoque — Reconciliação Completa

### 6.1 Equação do Estoque (Deve Fechar)
```
Valor Inicial (MT)
+ Compras (MT)
- Custo das Vendas (MT)
- Perdas e Ajustes Negativos (MT)
+ Ajustes Positivos (MT)
= Valor Final Calculado (MT)

Valor Final Sistema (saldo_atual × preco_custo) = ?
DIFERENÇA = Calculado - Sistema
```

### 6.2 Produtos com Diferença de Quantidade (Sistema vs Movimentos)
| Produto | Categoria | Saldo Movimentos | Saldo Sistema (estoque_atual) | Diferença Qtd | Diferença Valor (MT) | Status |
|---------|-----------|------------------|------------------------------|---------------|----------------------|--------|

### 6.3 Produtos com Saldo Negativo
- Produto, saldo atual, categoria, **responsável último movimento**, data último movimento

### 6.4 Produtos Abaixo do Mínimo / Acima do Máximo
- Lista com qtd atual, mínimo, máximo, dias sem reposição (se aplicável)

### 6.5 Produtos Sem Movimento no Período
- Parados há X dias, valor em estoque, categoria

### 6.6 Valorização por Categoria
| Categoria | Valor Inicial | Compras | Custo Vendas | Perdas/Ajustes | Valor Final | % Total |
|-----------|---------------|---------|--------------|----------------|-------------|---------|

---

## 7. Secção 5: Perdas — Detalhamento Completo

### 7.1 Resumo por Motivo
| Motivo | Nº Ocorrências | Qtd Total | Valor Total (MT) | % Total Perdas |
|--------|----------------|-----------|------------------|----------------|
| Quebra | | | | |
| Vencimento | | | | |
| Deterioração | | | | |
| Furto | | | | |
| Outros | | | | |

### 7.2 Lista Detalhada de Perdas
- Data, produto, código, qtd, unidade, motivo, **justificativa**, valor, **usuário que registou**, ID movimento

### 7.3 Perdas Sem Justificativa
- Filtro: justificativa vazia ou "—" → **Alto risco**

### 7.4 Perdas por Usuário
- Usuário, nº perdas, qtd total, valor total — detectar padrões

---

## 8. Secção 6: Ajustes de Inventário

### 8.1 Ajustes Positivos (Entradas)
- Data, produto, qtd, valor, motivo, **usuário aprovador**, data aprovação

### 8.2 Ajustes Negativos (Saídas)
- Mesmo detalhe

### 8.3 Ajustes Sem Justificativa / Sem Aprovador
- **Alerta crítico**

---

## 9. Secção 7: Contagens Físicas / Auditorias de Estoque no Período

### 9.1 Resumo das Auditorias
| Inventário # | Data | Tipo | Estado | Produtos Contados | Com Diferença | Perdas (MT) | Sobras (MT) | Contador | Responsável |
|--------------|------|------|--------|-------------------|---------------|-------------|-------------|----------|-------------|

### 9.2 Diferenças por Inventário
Para cada inventário com diferenças:
| Produto | Sistema (Qtd) | Contado (Qtd) | Diferença Qtd | Diferença Valor (MT) | Custo Unit. | Motivo | Justificada? | Observação |
|---------|---------------|---------------|---------------|----------------------|-------------|--------|--------------|------------|

### 9.3 Diferenças NÃO Justificadas (Todas as Auditorias)
- **Crítico**: lista consolidada de todas as diferenças sem justificativa no período

### 9.4 Recontagens Solicitadas
- Inventário, produto, contador original, contador recontagem, data, resultado

---

## 10. Secção 8: Anomalias e Alertas Automáticos (Motor de Detecção)

### 10.1 Estoque Negativo
- Produtos com saldo_atual < 0

### 10.2 Custo Zerado / Nulo
- Produtos ativos com preco_custo = 0 ou NULL

### 10.3 Movimentos de Entrada Sem Documento
- Entradas sem nota_fiscal (exceto Ajuste_Inventario)

### 10.4 Ajustes de Auditoria Sem Justificativa
- auditoria_ajustes com motivo vazio

### 10.5 Vendas Com Preço Fora da Faixa
- preco_unitario < preco_custo (prejuízo)
- preco_unitario > preco_venda * 1.05 (acima da tabela +5%)

### 10.6 Usuários Com Muitas Anulações
- Top 5 usuários por nº de anulações no período

### 10.7 Usuários Com Muitas Perdas Registadas
- Top 5 usuários por nº de perdas

### 10.8 Diferenças de Caixa Não Conferidas
- Canais com conferência pendente e valor esperado > 0

### 10.9 Faturas Vencidas Há Mais de 90 Dias Sem Cobrança
- Clientes com faturas antigas sem recibos recentes

### 10.10 Produtos Com Validade Vencida / A Vencer (30/60/90 dias)
- Já existe em perdas_e_validade()

### 10.11 Movimentos Fora de Horário Comercial
- Opcional: movimentos registados 22h-6h ou fins de semana

### 10.12 Sequência de Códigos de Venda Com Falhas
- Gaps em codigo_venda (V-0001, V-0003 → falta V-0002)

---

## 11. Secção 9: Evolução Temporal (Série Histórica)

### 11.1 Mensal (se período > 1 mês)
| Mês | Vendas | Compras | Lucro | Recebido | Perdas | Margem % | Diferença Caixa |
|-----|--------|---------|-------|----------|--------|----------|-----------------|

### 11.2 Semanal (se período > 1 semana)
| Semana | Vendas | Recebido | Diferença Caixa |

---

## 12. Secção 10: Rastreabilidade por Usuário (Quem Fez O Quê)

### 12.1 Resumo por Usuário
| Usuário | Perfil | Vendas (MT) | Compras (MT) | Perdas (MT) | Ajustes (MT) | Anulações | Recibos Emitidos | Conferências Feitas |
|---------|--------|-------------|--------------|-------------|--------------|-----------|------------------|---------------------|

### 12.2 Ações Críticas por Usuário
- Anulações: lista
- Perdas registadas: lista
- Ajustes aprovados: lista
- Diferenças de caixa justificadas: lista

---

## 13. Secção 11: Exportação e Formatos

### 13.1 HTML (Visualização no Navegador)
- Tabelas expansíveis/colapsáveis
- Filtros por secção
- Cores semânticas: 🔴 Crítico / 🟠 Alerta / 🟡 Atenção / 🟢 OK
- Botão "Imprimir" (CSS @media print)
- Botão "Exportar Excel"
- Botão "Exportar PDF"

### 13.2 Excel (Múltiplas Abas)
| Aba | Conteúdo |
|-----|----------|
| Resumo | Veredito, KPIs, Período |
| Vendas | Detalhe completo |
| Vendas_Anuladas | |
| Vendas_Abaixo_Custo | |
| Vendas_Acima_Tabela | |
| Compras | Por fornecedor, sem NF |
| Recebimentos_Caixa | Conferência por canal |
| Recibos_Credito | Detalhe |
| Contas_Receber | Envelhecimento, devedores |
| Estoque_Reconciliacao | Equação + diferenças |
| Estoque_Negativo | |
| Estoque_Parado | |
| Perdas | Por motivo + detalhe |
| Perdas_Sem_Justificativa | |
| Ajustes | Positivos + negativos |
| Auditorias_Contagem | Resumo + diferenças |
| Diferenças_Nao_Justificadas | Consolidado |
| Anomalias | Todas as 10+ categorias |
| Evolucao_Mensal | |
| Por_Usuario | Resumo + ações críticas |

### 13.3 PDF (Relatório Formal)
- Cabeçalho com logo, empresa, período
- Sumário executivo (veredito + KPIs)
- Todas as secções com quebras de página
- Rodapé: "Documento gerado pelo SGE — Página X de Y — Confidencial"
- Assinatura do auditor / responsável

---

## 14. Requisitos Técnicos

### 14.1 Performance
- Consulta única otimizada (CTEs) para buscar tudo
- Cache de 5 min para relatórios idênticos
- Paginação no HTML para tabelas > 500 linhas

### 14.2 Filtros de Período
- Presets: Hoje, Ontem, Esta semana, Semana passada, Este mês, Mês passado, Este semestre, Semestre passado, Ano atual, Personalizado
- Inputs: data_ini, data_fim (obrigatórios se personalizado)

### 14.3 Permissões
- Admin / Gerente: acesso total
- Operador: apenas visualização (sem exportação de dados sensíveis?)

### 14.4 Auditoria do Relatório
- Registar em tabela `auditoria` quem gerou/exportou o relatório e quando

---

## 15. Próximos Passos de Implementação

### Fase 1: Backend (relatorios_dados.py)
1. Criar função `gestao_completa(conn, ini, fim)` que retorna estrutura completa
2. Reutilizar funções existentes: `vendas`, `balancete`, `faturas_anuladas`, `perdas_e_validade`, `excecoes`, `curva_abc`, `divergencias_inventario`
3. Adicionar novas queries para: vendas acima tabela, anomalias por usuário, gaps de código, movimentos fora horário

### Fase 2: Rota (app.py)
1. Substituir `rel_gestao()` por nova versão
2. Manter compatibilidade com `exportar=excel|pdf`
3. Adicionar parâmetros de filtro avançados

### Fase 3: Template HTML (templates/rel_gestao.html)
1. Reestruturar com secções colapsáveis
2. Adicionar cards KPI coloridos
3. Tabelas com ordenação/filtro client-side (DataTables ou Alpine.js)
4. Formulário de conferência integrado

### Fase 4: Exportação Excel/PDF (reports.py)
1. Estender `gerar_excel` para múltiplas abas
2. Estender `gerar_pdf` para layout formal
3. Adicionar assinatura/rodapé

---

## 16. Perguntas para Decisão

1. **Tolerância para "Venda acima da tabela"**: % fixo (5%) ou configurável?
2. **Horário comercial** para detectar movimentos fora de horas: fixo (8h-20h) ou configurável?
3. **Período padrão** ao abrir relatório: mês atual ou último mês fechado?
4. **Incluir custo de oportunidade** (estoque parado × custo × taxa)?
5. **Assinatura digital** no PDF (hash do conteúdo + usuário + timestamp)?

---

*Documento vivo — atualizar conforme implementação e feedback do auditor.*