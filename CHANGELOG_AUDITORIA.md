# Correcções da auditoria (outubro 2026)

## Estrutura
- O zip enviado estava "achatado". A estrutura correcta é: `templates/`, `static/{css,js,img}/`,
  `dados/` (aqui fica o `estoque.db`), `docs/`, `tests/`, `scripts/`.
- Copiar o teu `estoque.db` para `dados/` (senão a app cria uma base vazia).
- Adicionado `.gitignore` (dados, backups, logs, `.secret_key` e `.7z` não vão para o git).

## Críticos
| # | Correcção |
|---|---|
| C1 | `app.py`: `nuit_cliente`/`cliente_nuit` → `nif_cliente`/`cliente_nif`. A emissão de facturas voltou e o NIF chega à factura. |
| C2 | `registrar_venda_credito` é atómica (uma só transacção: saídas de stock, factura, itens, nº de factura e auditoria). `registrar_saida` ganhou `commit=False` e devolve `id_movimento` (já não se procura "o último movimento do produto"). |
| C3 | Cotações: usam a unidade do produto e o desconto; `faturar_cotacao` e `converter_cotacao_em_venda` são atómicas (a conversão Aprovada→Convertida estava bloqueada por `atualizar_status_cotacao`). Quantidades em kg já não são truncadas (2,5 kg ≠ 2). |
| C4 | `cancelar_conta_receber`: ordem correcta (itens e factura antes dos movimentos), repõe stock, numa transacção. **Muda o comportamento:** facturas com recibos deixam de poder ser eliminadas. |

## Altos
- Handler global deixa passar `HTTPException` (404 deixa de ser 500 e de poluir o log).
- Auditoria passa a ser gravada (`commit` depois de `registrar_auditoria` em 12 funções).
- IVA vazio usa a taxa padrão (16%), já não 0%.
- Reset: ordem correcta das tabelas (inclui facturas, recibos, fechos, auditorias), aborta se o backup falhar, exige a senha do administrador.
- Numeração: o contador de facturas usa a mesma transacção (sem buracos nem duplicados); recibos e cotações usam o maior número existente em vez de `COUNT(*)+1`.

## Do segundo relatório (confirmados)
- Upload do logo: campo `logo_arquivo` (o template enviava `logo_file`).
- Chart.js 4.4.1 agora local (`static/js/chart.umd.min.js`), o dashboard funciona offline.
- Botão "Encerrar" só para Admin/Gerente.
- `favicon.ico` incluído; rota tolerante se faltar; logo só é apresentado se o ficheiro existir.

## Outros
- `next` do login só aceita caminhos internos.
- Backups e cópia pré-reset usam a API de backup do SQLite; limite de upload 256 MB.
- Inventário: chaves de formulário inválidas já não dão 500.
- `SGE_DATA_DIR` permite apontar a base de dados para outra pasta (usado pelos testes).

## Edição de cotações (corrigido depois)
- O formulário de edição era o de "Nova Cotação" sem dados e sem `action`: ao gravar, o POST ia para `/editar` (só GET) e nunca chegava a `/atualizar`. Agora abre preenchido (cliente, validade, observações e todos os itens), permite adicionar e remover itens (incluindo os já existentes) e grava na cotação certa.
- Quantidades decimais (kg) aceites nas cotações; erros de preenchimento mostram mensagem em vez de 500.
- Produtos desativados que já constam na cotação continuam visíveis ao editar.
- **PDF do recibo:** `reports.gerar_pdf_recibo` não existia (o botão PDF do recibo dava erro). Criado.
- Avisos do Pylance: corrigidos os de `static_folder`, `secure_filename`, `safe_parse_float`, Excel e tolerâncias da auditoria.

## Testes
`python -m unittest discover -s tests -v` (19 testes, usam base temporária).

## Script de reparação
`python scripts/reparar_orfaos.py` lista (e com `--aplicar` repõe) as saídas a crédito sem fatura.

## Não alterado (decisões tuas)
- Recuperação de senha sem autenticação (o token é mostrado a quem souber o login).
- CSRF e logout por GET.
- Bloqueio de login por conta (permite bloquear o admin de propósito).
- Anulação fiscal com nota de crédito (hoje a factura sem pagamentos é eliminada).
- Logo em executável PyInstaller (`static/` é só de leitura) — guardar em `dados/`.
- Série de facturas não reinicia por ano; arredondamento do preço unitário a 2 casas nas entradas.

## Retificações — fases 0 e 1 (novembro 2026)
- Leitura de números: `1.500` em dinheiro passa a ser 1500; em quantidades e taxas um ponto único continua decimal (`2.500` = 2,5). Regra única em `services.parse_numero_pt`, usada também pelo comparador de estoque.
- Quantidades em kg deixam de ser truncadas nos relatórios, exports (Excel/PDF), inventário e cotações (`services.qtd_relatorio`).
- `services.py`: removido o BOM e corrigido o texto com codificação duplicada (217 ocorrências).
- Extrato por categoria: o saldo acumulado é por produto e parte do saldo anterior ao período; o saldo em valor passa a ser entradas − saídas; quantidades de unidades diferentes já não se somam; a linha de totais tem o mesmo número de colunas que o cabeçalho.
- `build_exe.bat` e `installer.iss` passam a copiar `atualizador.py` e `comparador_estoque.py` para a pasta `_codigo` (e raiz).
- Novos testes em `tests/test_retificacoes.py` (33 testes no total).
- Pendente: nenhum script do repositório compila o `bootstrap.py`; o executável é gerado a partir de `app.py`, por isso o mecanismo de atualização por `_codigo` não está ligado ao build.

## Retificações — fase 2: registo de vendas (novembro 2026)
- Vendas à vista e a crédito passam a ter validação prévia, sem gravar (`services.validar_itens_venda`): todas as linhas são verificadas de uma vez e os erros indicam a linha, o produto e o problema.
- Estoque insuficiente nomeia o produto e usa a unidade do utilizador (ex.: "Disponível: 2 cx (48 un); pedido: 5 cx (120 un)"). Linhas do mesmo produto somam-se.
- Popup (`AppModal.abrir`) com a lista de problemas, "Corrigir" e "Retirar linha" por linha; o formulário não é apagado e as linhas com erro ficam marcadas. Nova `static/js/venda_validacao.js`.
- Gravação numa só transacção (`services.processar_venda_vista`); se uma linha falhar nesse momento (outro utilizador vendeu entretanto), a transacção é desfeita e a linha volta indicada.
- Margem negativa e preço 0,00 são avisados antes de gravar, com confirmação.
- Envio único: cada formulário traz um token e um envio repetido (duplo clique, F5) não regista duas vezes. O token vive em memória: reiniciar o servidor invalida os formulários abertos.
- Tentativas falhadas ficam na auditoria (`tabela = 'venda_falhada'`; a coluna `acao` só aceita INSERT/UPDATE/DELETE/LOGIN/LOGOUT).
- Sem JavaScript o comportamento antigo mantém-se (mensagens e redirecionamento), mas as mensagens já nomeiam o produto.
- Testes: `tests/test_vendas_fase2.py` (16). Total: 49.
