# Documento de Requisitos — Double-Entry Ledger (Subledger)

## Introdução

Sistema de subledger distribuído baseado em partidas dobradas (Double-Entry Bookkeeping), construído sobre AWS com Python e DynamoDB. O sistema atua como Single Source of Truth (SSOT) para todos os movimentos financeiros, aplicando separação estrita entre caminho de escrita (Write Path com consistência forte) e caminho de leitura (Read Path via CQRS). A arquitetura segue DDD tático, padrões GoF adaptados para Python, e infraestrutura como código com Terraform.

## Glossário

- **Ledger_Engine**: Motor central de partidas dobradas responsável por criar, validar e persistir lançamentos contábeis (journal entries) e suas postagens (postings).
- **Journal_Entry**: Registro contábil imutável que agrupa um conjunto de postings. Cada journal entry possui um identificador único (entry_id) e uma chave de idempotência (external_id).
- **Posting**: Linha individual de débito ou crédito vinculada a uma conta e a um journal entry. Valores expressos em minor units (centavos).
- **Account**: Entidade que representa uma conta no subledger. Tipos: Available, Hold, Fees, Clearing. Cada conta pertence a um tenant.
- **Balance**: Projeção materializada do saldo corrente de uma conta em uma moeda específica. Protegida por Optimistic Concurrency Control (OCC) via campo version.
- **Outbox_Event**: Evento transacional gravado atomicamente junto com o journal entry. Capturado via DynamoDB Streams e publicado para barramento de eventos.
- **Write_Path**: Caminho de escrita com consistência forte via DynamoDB TransactWriteItems.
- **Read_Path**: Caminho de leitura via projeções materializadas (CQRS), com consistência eventual.
- **Minor_Units**: Representação monetária em inteiros (centavos). Exemplo: R$ 10,50 = 1050.
- **OCC**: Optimistic Concurrency Control — controle de concorrência otimista usando campo version nas atualizações de saldo.
- **Outbox_Pattern**: Padrão onde eventos são gravados na mesma transação que os dados de negócio, garantindo entrega confiável.
- **CQRS**: Command Query Responsibility Segregation — separação entre modelos de escrita e leitura.
- **WORM**: Write Once Read Many — política de imutabilidade para armazenamento de auditoria.
- **TransactWriteItems**: Operação atômica do DynamoDB que agrupa múltiplas escritas em uma única transação.
- **DLQ**: Dead Letter Queue — fila para mensagens que falharam no processamento.
- **Lambda_Publisher**: Função AWS Lambda que consome DynamoDB Streams e publica eventos no barramento.
- **Reversal**: Lançamento contábil que anula um journal entry anterior, criando postings inversos. Única forma de correção permitida.

## Requisitos

### Requisito 1: Motor de Partidas Dobradas (Zero-Sum)

**User Story:** Como engenheiro de plataforma financeira, eu quero que todo lançamento contábil obedeça à regra de partidas dobradas, para que o subledger mantenha integridade contábil absoluta.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL garantir que a soma algébrica de todos os postings de um Journal_Entry seja igual a zero para cada moeda envolvida.
2. WHEN um Journal_Entry é submetido com postings cuja soma não é zero para qualquer moeda, THE Ledger_Engine SHALL rejeitar o lançamento e retornar erro estruturado com código ZERO_SUM_VIOLATION e status HTTP 400.
3. THE Ledger_Engine SHALL representar débitos como valores positivos e créditos como valores negativos nos postings.
4. WHEN um Journal_Entry válido é submetido, THE Ledger_Engine SHALL criar o Journal_Entry com no mínimo dois postings.
5. FOR ALL Journal_Entries válidos, criar o journal entry e depois consultar seus postings SHALL produzir postings cuja soma é zero por moeda (propriedade de invariante round-trip).

### Requisito 2: Precisão Monetária em Minor Units

**User Story:** Como engenheiro de plataforma financeira, eu quero que todos os valores monetários sejam representados em minor units (inteiros), para que não haja erros de arredondamento em operações financeiras.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL representar todos os valores monetários como inteiros em minor units (centavos).
2. WHEN um valor monetário do tipo float ou decimal é recebido na API, THE Ledger_Engine SHALL rejeitar a requisição com erro estruturado com código INVALID_AMOUNT_TYPE e status HTTP 400.
3. THE Ledger_Engine SHALL validar que todos os valores de posting são inteiros maiores que zero antes de processar o Journal_Entry.

### Requisito 3: Atomicidade do Write Path

**User Story:** Como engenheiro de plataforma financeira, eu quero que a gravação de lançamentos contábeis seja atômica, para que não existam estados parciais no subledger.

#### Critérios de Aceitação

1. THE Write_Path SHALL gravar Journal_Entry, Postings, atualizações de Balance e Outbox_Event em uma única operação TransactWriteItems do DynamoDB.
2. IF a operação TransactWriteItems falhar por qualquer motivo, THEN THE Write_Path SHALL garantir que nenhum dos itens da transação seja persistido.
3. THE Write_Path SHALL incluir todos os postings, todas as atualizações de saldo e o evento de outbox na mesma TransactWriteItems.

### Requisito 4: Idempotência End-to-End

**User Story:** Como consumidor da API do ledger, eu quero que requisições duplicadas com o mesmo external_id sejam tratadas de forma idempotente, para que retentativas não criem lançamentos duplicados.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL usar o campo external_id como chave de idempotência para Journal_Entries.
2. WHEN um Journal_Entry é submetido com um external_id já existente, THE Ledger_Engine SHALL retornar HTTP 200 com o entry_id do Journal_Entry original sem criar um novo lançamento.
3. THE Write_Path SHALL usar ConditionExpression attribute_not_exists na chave primária para garantir unicidade do external_id na operação TransactWriteItems.
4. FOR ALL submissões duplicadas com o mesmo external_id, a resposta SHALL conter o mesmo entry_id da primeira submissão (propriedade de idempotência).

### Requisito 5: Controle de Concorrência Otimista (OCC)

**User Story:** Como engenheiro de plataforma financeira, eu quero que atualizações de saldo usem controle de concorrência otimista, para que escritas concorrentes não corrompam saldos.

#### Critérios de Aceitação

1. THE Write_Path SHALL incluir uma ConditionExpression verificando o campo version do Balance em cada atualização de saldo dentro da TransactWriteItems.
2. WHEN uma atualização de saldo falha por conflito de versão (ConditionalCheckFailedException), THE Write_Path SHALL retornar erro estruturado com código OPTIMISTIC_LOCK_CONFLICT e status HTTP 409.
3. THE Write_Path SHALL incrementar o campo version do Balance em exatamente 1 a cada atualização bem-sucedida.
4. FOR ALL atualizações concorrentes ao mesmo Balance, apenas uma SHALL ser bem-sucedida e as demais SHALL receber erro de conflito (propriedade de serialização).

### Requisito 6: Modelagem de Contas e Holds

**User Story:** Como engenheiro de plataforma financeira, eu quero que bloqueios de saldo sejam modelados como postings entre contas Available e Hold, para que toda movimentação financeira seja rastreável via partidas dobradas.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL modelar cada usuário com no mínimo duas contas: uma do tipo Available e uma do tipo Hold.
2. WHEN uma operação de bloqueio de saldo é solicitada, THE Ledger_Engine SHALL criar um Journal_Entry com posting de débito na conta Available e posting de crédito na conta Hold do mesmo usuário.
3. WHEN uma operação de liberação de saldo é solicitada, THE Ledger_Engine SHALL criar um Journal_Entry com posting de débito na conta Hold e posting de crédito na conta Available do mesmo usuário.
4. THE Ledger_Engine SHALL suportar contas de plataforma dos tipos Fees e Clearing além das contas de usuário.
5. THE Account SHALL possuir os campos account_id, tenant_id, type, status e created_at.

### Requisito 7: Transactional Outbox Pattern

**User Story:** Como engenheiro de plataforma, eu quero que eventos de domínio sejam gravados atomicamente com os lançamentos contábeis, para que a publicação de eventos seja confiável e sem perda.

#### Critérios de Aceitação

1. THE Write_Path SHALL gravar um Outbox_Event na mesma TransactWriteItems que o Journal_Entry e seus Postings.
2. THE Outbox_Event SHALL conter os campos event_id, entry_id, event_type, payload e expires_at (TTL).
3. WHEN um novo Outbox_Event aparece no DynamoDB Stream com imagem NEW_IMAGE, THE Lambda_Publisher SHALL publicar o evento no barramento de eventos (EventBridge, SNS ou SQS).
4. IF a Lambda_Publisher falhar ao processar um evento do Stream, THEN THE Lambda_Publisher SHALL encaminhar o registro para a DLQ.
5. THE Outbox_Event SHALL usar event_id com prefixo "OUTBOX#" seguido do entry_id para facilitar identificação no DynamoDB Stream.

### Requisito 8: Read Path e CQRS

**User Story:** Como consumidor da API do ledger, eu quero consultar saldos e extratos de forma rápida e paginada, para que operações de leitura não impactem a performance do caminho de escrita.

#### Critérios de Aceitação

1. THE Read_Path SHALL fornecer consulta de saldo (Balance) com complexidade O(1) via projeção materializada.
2. THE Read_Path SHALL fornecer consulta de extrato (Statement) com paginação e ordenação cronológica usando o posting_sort_key.
3. THE Read_Path SHALL operar com consistência eventual, aceitando defasagem de até 1 segundo em relação ao Write_Path.
4. THE Write_Path SHALL ser o único caminho que valida saldos com consistência forte para decisões de negócio.
5. WHEN uma consulta de extrato é solicitada, THE Read_Path SHALL retornar os postings paginados com cursor baseado no posting_sort_key.

### Requisito 9: Imutabilidade e Reversões

**User Story:** Como auditor financeiro, eu quero que lançamentos contábeis sejam imutáveis e que correções sejam feitas exclusivamente via reversões, para que exista trilha de auditoria completa.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL proibir operações de UPDATE e DELETE em Journal_Entries e Postings.
2. WHEN uma correção de lançamento é necessária, THE Ledger_Engine SHALL criar um novo Journal_Entry do tipo REVERSAL com postings inversos ao lançamento original.
3. THE Reversal SHALL referenciar o entry_id do Journal_Entry original no campo metadata.
4. FOR ALL Reversals, a soma dos postings do lançamento original com os postings do reversal SHALL ser zero por moeda (propriedade de anulação).

### Requisito 10: Auditoria e WORM

**User Story:** Como auditor financeiro, eu quero que todas as transações sejam exportadas para armazenamento imutável, para que exista registro permanente para compliance e analytics.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL exportar todos os Journal_Entries e Postings para S3 com Object Lock (WORM).
2. THE Ledger_Engine SHALL armazenar os dados de auditoria em formato Parquet ou Iceberg para consultas analíticas.
3. WHEN um Journal_Entry é criado, THE Ledger_Engine SHALL garantir que o registro de auditoria correspondente seja exportado para S3.

### Requisito 11: Modelo de Dados DynamoDB

**User Story:** Como engenheiro de plataforma, eu quero um modelo de dados DynamoDB otimizado com single-table design, para que o sistema seja escalável e eficiente em custo.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL usar single-table design com chaves compostas no DynamoDB.
2. THE Posting SHALL usar account_id como partition key e posting_sort_key no formato "POSTING#timestamp#entry_id#index" como sort key.
3. THE Balance SHALL usar account_id como partition key e currency como sort key.
4. THE Journal_Entry SHALL usar entry_id (UUID) como partition key.
5. THE Ledger_Engine SHALL habilitar Point-in-Time Recovery (PITR) na tabela DynamoDB.

### Requisito 12: Infraestrutura como Código (Terraform)

**User Story:** Como engenheiro de plataforma, eu quero que toda a infraestrutura seja provisionada via Terraform seguindo boas práticas AWS, para que o ambiente seja reproduzível e auditável.

#### Critérios de Aceitação

1. THE Terraform SHALL usar backend remoto S3 com use_lockfile habilitado para gerenciamento de state.
2. THE Terraform SHALL separar states por componente (networking, storage, compute) e por ambiente (dev, staging, prod).
3. THE Terraform SHALL declarar versões em required_providers e fazer commit do .terraform.lock.hcl.
4. THE Terraform SHALL provisionar a tabela DynamoDB com PITR, DynamoDB Streams (NEW_IMAGE) e TTL habilitado para Outbox_Events.
5. THE Terraform SHALL provisionar Lambda_Publisher com Event Source Mapping, filtro de eventos para prefixo "OUTBOX#" e DLQ configurada.
6. THE Terraform SHALL provisionar bucket S3 com Object Lock para armazenamento WORM de auditoria.
7. THE Terraform SHALL usar autenticação efêmera (OIDC ou SSO) e proibir credenciais estáticas no código.

### Requisito 13: Testes de Integração Local com Finch

**User Story:** Como desenvolvedor, eu quero executar testes de integração localmente usando Finch com DynamoDB Local, para que eu possa validar o sistema sem depender de infraestrutura AWS.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL fornecer configuração Finch (container) com DynamoDB Local para testes de integração.
2. WHEN os testes de integração são executados, THE Ledger_Engine SHALL criar as tabelas DynamoDB automaticamente no ambiente local.
3. THE Ledger_Engine SHALL incluir testes de integração que validem atomicidade, idempotência, OCC e zero-sum no ambiente local.

### Requisito 14: Tratamento de Limites do DynamoDB

**User Story:** Como engenheiro de plataforma, eu quero que o sistema respeite os limites do TransactWriteItems, para que operações grandes não falhem silenciosamente.

#### Critérios de Aceitação

1. WHEN um Journal_Entry resulta em mais de 100 itens na TransactWriteItems, THE Ledger_Engine SHALL rejeitar a operação com erro estruturado com código TRANSACTION_LIMIT_EXCEEDED e status HTTP 400.
2. WHEN um Journal_Entry resulta em payload maior que 4MB na TransactWriteItems, THE Ledger_Engine SHALL rejeitar a operação com erro estruturado com código TRANSACTION_SIZE_EXCEEDED e status HTTP 400.

### Requisito 15: Observabilidade e Resiliência

**User Story:** Como engenheiro de plataforma, eu quero que o sistema tenha logging estruturado e métricas, para que problemas sejam detectados e diagnosticados rapidamente.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL emitir logs estruturados (JSON) com campos entry_id, operation e result para cada operação de escrita.
2. THE Ledger_Engine SHALL emitir métricas de latência do Write_Path e do Read_Path.
3. IF uma operação TransactWriteItems falhar, THEN THE Ledger_Engine SHALL registrar log com detalhes do erro incluindo entry_id e tipo de falha.
4. THE Lambda_Publisher SHALL emitir métricas de eventos processados, eventos com falha e tamanho da DLQ.

### Requisito 16: Contratos de API

**User Story:** Como consumidor da API do ledger, eu quero contratos de API claros com erros estruturados, para que a integração seja previsível e fácil de depurar.

#### Critérios de Aceitação

1. THE Ledger_Engine SHALL retornar respostas de sucesso no formato {"status": "success", "data": {...}, "metadata": {...}}.
2. THE Ledger_Engine SHALL retornar respostas de erro no formato {"error": {"code": "<ERROR_CODE>", "message": "<descrição>"}}.
3. THE Ledger_Engine SHALL validar o schema de entrada na camada de API antes de invocar a lógica de domínio.
4. THE Ledger_Engine SHALL suportar paginação baseada em cursor para consultas de extrato.
