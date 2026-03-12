# Plano de Implementação: Double-Entry Ledger (Subledger)

## Visão Geral

Implementação incremental do subledger de partidas dobradas, começando pelo núcleo do domínio (value objects, erros) e expandindo para infraestrutura, API e Terraform. Cada tarefa constrói sobre as anteriores, garantindo que não haja código órfão. Testes de propriedade (Hypothesis) e unitários acompanham cada componente.

## Tarefas

- [x] 1. Configurar estrutura do projeto e dependências
  - Criar a árvore de diretórios conforme design (ledger/domain, infrastructure, application, api, tests/unit, tests/property, tests/integration, infra)
  - Criar `pyproject.toml` com dependências: boto3, hypothesis, pytest, pydantic (schema validation)
  - Criar `__init__.py` em todos os pacotes
  - Criar `conftest.py` raiz com configuração base do pytest
  - _Requirements: 13.1_

- [x] 2. Implementar Value Objects e hierarquia de erros do domínio
  - [x] 2.1 Implementar Value Objects (value_objects.py)
    - Implementar `Money` (frozen dataclass, amount int > 0, currency ISO 4217 3 chars)
    - Implementar `Direction` (Enum: DEBIT, CREDIT)
    - Implementar `Posting` (frozen dataclass com `signed_amount` property: DEBIT=+, CREDIT=-)
    - Implementar `AccountType` (Enum: AVAILABLE, HOLD, FEES, CLEARING)
    - Implementar `EntryType` (Enum: STANDARD, REVERSAL)
    - Implementar `Balance` (dataclass com version OCC, balance_amount int, currency, last_update)
    - Implementar `OutboxEvent` (frozen dataclass com event_id prefixo "OUTBOX#", expires_at TTL)
    - _Requirements: 1.1, 1.3, 2.1, 6.1, 6.4, 7.2, 7.5_

  - [x] 2.2 Implementar hierarquia de erros (errors.py)
    - Implementar `DomainError` base com code, message, http_status
    - Implementar `ZeroSumViolation`, `InvalidAmountType`, `OptimisticLockConflict`, `IdempotencyConflict`, `TransactionLimitExceeded`, `TransactionSizeExceeded`, `JournalEntryNotFound`
    - _Requirements: 1.2, 2.2, 5.2, 4.2, 14.1, 14.2_

  - [x] 2.3 Escrever teste de propriedade para convenção de sinais
    - **Property 4: Convenção de sinais (débito positivo, crédito negativo)**
    - **Validates: Requirements 1.3**

  - [x] 2.4 Escrever teste de propriedade para validação de minor units
    - **Property 5: Validação de minor units**
    - **Validates: Requirements 2.1, 2.2, 2.3**

  - [x] 2.5 Escrever teste de propriedade para prefixo OUTBOX#
    - **Property 13: Prefixo OUTBOX# no event_id**
    - **Validates: Requirements 7.5**

  - [x] 2.6 Escrever testes unitários para Value Objects e erros
    - Testar criação válida e inválida de Money (float, zero, negativo, currency inválida)
    - Testar signed_amount de Posting para DEBIT e CREDIT
    - Testar instanciação de cada erro com campos corretos
    - _Requirements: 1.3, 2.1, 2.2, 2.3_

- [x] 3. Implementar Entity Account e Aggregate JournalEntry
  - [x] 3.1 Implementar Entity Account (entities.py)
    - Implementar `Account` dataclass com account_id, tenant_id, account_type, status, created_at
    - _Requirements: 6.1, 6.4, 6.5_

  - [x] 3.2 Implementar Aggregate Root JournalEntry (aggregates.py)
    - Implementar `JournalEntry` frozen dataclass com entry_id, external_id, entry_type, postings (tuple), metadata, timestamp, outbox_event
    - Implementar método `validate_zero_sum()` que agrupa postings por moeda e verifica soma == 0
    - _Requirements: 1.1, 1.4, 1.5, 9.1_

  - [x] 3.3 Escrever teste de propriedade para zero-sum round-trip
    - **Property 1: Zero-sum round-trip**
    - Criar Hypothesis strategy `balanced_postings_strategy` que gera pares DEBIT/CREDIT balanceados
    - **Validates: Requirements 1.1, 1.5**

  - [x] 3.4 Escrever teste de propriedade para mínimo dois postings
    - **Property 2: Mínimo dois postings**
    - **Validates: Requirements 1.4**

  - [x] 3.5 Escrever teste de propriedade para rejeição de entradas desbalanceadas
    - **Property 3: Rejeição de entradas desbalanceadas**
    - **Validates: Requirements 1.2**

- [x] 4. Implementar ValidationChain, JournalEntryFactory e LedgerEngine
  - [x] 4.1 Implementar ValidationChain (validators.py) — GoF Chain of Responsibility
    - Implementar `ValidationStrategy` Protocol
    - Implementar `ValidationResult` (success/failure com lista de erros)
    - Implementar `ZeroSumValidator`: soma algébrica == 0 por moeda
    - Implementar `MinorUnitsValidator`: todos amounts são int > 0
    - Implementar `TransactionLimitValidator`: ≤ 100 itens e ≤ 4MB
    - Implementar `ValidationChain` que encadeia validadores e interrompe no primeiro erro
    - _Requirements: 1.1, 1.2, 2.1, 2.3, 14.1, 14.2_

  - [x] 4.2 Implementar JournalEntryFactory (factories.py) — GoF Factory Method
    - Implementar `create_standard()`: gera entry_id UUID v4, timestamp ISO 8601, OutboxEvent com prefixo OUTBOX#
    - Implementar `create_reversal()`: gera postings inversos ao original, entry_type REVERSAL, metadata com original entry_id
    - _Requirements: 1.4, 7.1, 7.2, 9.2, 9.3_

  - [x] 4.3 Implementar Port LedgerRepository (ports.py)
    - Implementar `LedgerRepository` Protocol com métodos: save_journal_entry, find_journal_entry_by_id, find_journal_entry_by_external_id, get_balance, get_statement
    - Implementar `StatementPage` dataclass para paginação
    - _Requirements: 3.1, 4.1, 8.1, 8.2_

  - [x] 4.4 Implementar LedgerEngine (services.py) — GoF Facade
    - Implementar `create_journal_entry()`: verificar idempotência → validar via chain → criar via factory → persistir via repository
    - Implementar `create_reversal()`: buscar original → gerar postings inversos → persistir
    - Implementar logging estruturado (JSON) com entry_id, operation, result
    - _Requirements: 1.1, 1.2, 4.1, 4.2, 9.2, 15.1, 15.3_

  - [x] 4.5 Escrever teste de propriedade para limites do DynamoDB
    - **Property 18: Validação de limites do DynamoDB**
    - **Validates: Requirements 14.1, 14.2**

  - [x] 4.6 Escrever teste de propriedade para reversal annulment
    - **Property 16: Anulação por reversão (reversal annulment)**
    - **Validates: Requirements 9.2, 9.3, 9.4**

  - [x] 4.7 Escrever teste de propriedade para logging estruturado
    - **Property 19: Logging estruturado**
    - **Validates: Requirements 15.1, 15.3**

  - [x] 4.8 Escrever testes unitários para ValidationChain, Factory e Engine
    - Testar cada validador isoladamente com exemplos concretos
    - Testar factory com criação standard e reversal
    - Testar engine com fluxo completo usando repository in-memory
    - Testar imutabilidade (Property 15: tentativa de update/delete rejeitada)
    - _Requirements: 1.1, 1.2, 1.4, 9.1, 9.2, 14.1, 14.2_

- [x] 5. Checkpoint — Validar domínio
  - make sure the project is on this format
    src/
      ledger/
    tests/
      ledger/
    instead of everything inside of ledger/
  - Ensure all tests pass, ask the user if questions arise.

- [-] 6. Implementar camada Application (Commands, Queries, Handlers, DTOs)
  - [x] 6.1 Implementar Commands e Queries (commands.py, queries.py)
    - Implementar `CreateJournalEntryCommand` (external_id, postings list, metadata)
    - Implementar `CreateReversalCommand` (original_entry_id, external_id, metadata)
    - Implementar `GetBalanceQuery` (account_id, currency)
    - Implementar `GetStatementQuery` (account_id, cursor, page_size)
    - _Requirements: 1.4, 4.1, 8.1, 8.2, 9.2_

  - [x] 6.2 Implementar Handlers (handlers.py)
    - Implementar `CommandHandler` que delega para LedgerEngine
    - Implementar `QueryHandler` que delega para LedgerRepository (read path)
    - _Requirements: 3.1, 8.1, 8.2_

  - [x] 6.3 Implementar DTOs (dtos.py) — Anti-Corruption Layer
    - Implementar DTOs de request/response para API
    - Implementar conversão DTO → Command/Query e Domain → DTO
    - _Requirements: 16.1, 16.2_

  - [x] 6.4 Escrever teste de propriedade para modelagem de contas
    - **Property 11: Modelagem de contas por usuário (Available + Hold)**
    - **Validates: Requirements 6.1**

  - [x] 6.5 Escrever testes unitários para Application layer
    - Testar handlers com mocks do repository e engine
    - Testar conversão DTO ↔ Domain
    - _Requirements: 6.1, 8.1, 16.1, 16.2_

- [x] 7. Implementar camada de Infraestrutura (DynamoDB Repository, Mapper, Publisher)
  - [x] 7.1 Implementar DynamoDB Mapper (dynamodb_mapper.py)
    - Implementar mapeamento domínio → DynamoDB items (PK/SK conforme single-table design)
    - Implementar mapeamento DynamoDB items → domínio
    - Implementar geração de posting_sort_key no formato "POSTING#{timestamp}#{entry_id}#{index}"
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

  - [x] 7.2 Implementar DynamoDBLedgerRepository (dynamodb_repository.py) — GoF Adapter
    - Implementar `save_journal_entry()` via TransactWriteItems: JournalEntry + Postings + Balance updates (OCC) + OutboxEvent + Idempotency record
    - Implementar ConditionExpression `attribute_not_exists` para idempotência
    - Implementar ConditionExpression `version = :expected_version` para OCC
    - Implementar `find_journal_entry_by_id()`, `find_journal_entry_by_external_id()`
    - Implementar `get_balance()` via GetItem (O(1))
    - Implementar `get_statement()` via Query com paginação por cursor (posting_sort_key)
    - _Requirements: 3.1, 3.2, 3.3, 4.3, 5.1, 5.3, 8.1, 8.2, 8.5_

  - [x] 7.3 Implementar Lambda Publisher (publisher.py) — GoF Observer
    - Implementar handler que consome DynamoDB Streams (filtro OUTBOX#)
    - Implementar deserialização de OutboxEvent
    - Implementar publicação no EventBridge via PutEvents
    - Implementar fallback para DLQ (SQS) em caso de falha
    - _Requirements: 7.3, 7.4, 7.5_

  - [x] 7.4 Implementar Audit Pipeline — Transform Lambda + Firehose (audit_exporter.py, audit_handler.py)
    - Implementar `AuditRecord` frozen dataclass com schema flat (record_type, entry_id, external_id, entry_type, account_id, amount, direction, currency, posting_index, tenant_id, timestamp, metadata, year, month, day)
    - Implementar `AuditTransformer` que filtra registros do DynamoDB Stream (descarta BALANCE#, mantém JOURNAL# e POSTING#)
    - Implementar deserialização DynamoDB JSON → AuditRecord flat com campos de particionamento (year, month, day, tenant_id)
    - Implementar Lambda handler (audit_handler.py) que consome DynamoDB Streams e chama `AuditTransformer.process_stream_records()`
    - Implementar envio para Kinesis Data Firehose via `PutRecordBatch` (JSON records)
    - Implementar fallback para Audit DLQ (SQS separada) em caso de falha na Transform Lambda
    - Implementar logging estruturado: entry_ids processados, registros enviados ao Firehose, registros filtrados
    - Nota: batching, conversão Parquet, particionamento S3 e entrega WORM são responsabilidade do Firehose (configurado via Terraform)
    - _Requirements: 10.1, 10.2, 10.3_

  - [x] 7.5 Escrever teste de propriedade para formato do posting_sort_key
    - **Property 17: Formato do posting_sort_key**
    - **Validates: Requirements 11.2**

  - [x] 7.6 Escrever testes unitários para Mapper, Publisher e Audit Transformer
    - Testar round-trip domínio → DynamoDB → domínio
    - Testar publisher com eventos válidos e falhas
    - Testar audit transformer: filtragem de registros (JOURNAL# e POSTING# aceitos, BALANCE# descartado)
    - Testar audit transformer: deserialização DynamoDB JSON → AuditRecord flat com campos corretos
    - Testar audit transformer: enriquecimento com campos de particionamento (year, month, day extraídos do timestamp)
    - Testar audit transformer: chamada PutRecordBatch no Firehose com mock
    - _Requirements: 11.1, 11.2, 7.3, 7.4, 10.1, 10.3_

- [ ] 8. Checkpoint — Validar infraestrutura
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implementar camada de API (Lambda Handlers, Schema Validation)
  - [x] 9.1 Implementar Schema Validator (schema_validator.py)
    - Implementar validação JSON Schema para POST /entries e POST /reversals
    - Implementar validação de tipos (rejeitar float/decimal em amount)
    - _Requirements: 2.2, 16.3_

  - [x] 9.2 Implementar Write Handler (write_handler.py)
    - Implementar handler Lambda para POST /entries: schema validation → DTO → CommandHandler → response
    - Implementar handler Lambda para POST /reversals: schema validation → DTO → CommandHandler → response
    - Implementar tradução DomainError → HTTP response estruturada
    - Implementar logging estruturado por operação
    - _Requirements: 1.2, 2.2, 4.2, 5.2, 15.1, 16.1, 16.2_

  - [x] 9.3 Implementar Read Handler (read_handler.py)
    - Implementar handler Lambda para GET /balances/{account_id}: QueryHandler → response
    - Implementar handler Lambda para GET /statements/{account_id}: QueryHandler com paginação cursor → response
    - _Requirements: 8.1, 8.2, 8.5, 16.1, 16.4_

  - [x] 9.4 Escrever teste de propriedade para formato de resposta da API
    - **Property 20: Formato de resposta da API**
    - **Validates: Requirements 16.1, 16.2**

  - [x] 9.5 Escrever testes unitários para API handlers
    - Testar write handler com payloads válidos e inválidos
    - Testar read handler com paginação
    - Testar tradução de cada DomainError para HTTP response
    - _Requirements: 16.1, 16.2, 16.3_

- [ ] 10. Checkpoint — Validar API
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Configurar testes de integração com Finch + DynamoDB Local
  - [x] 11.1 Criar docker-compose.yml e fixtures pytest
    - Criar `docker-compose.yml` com DynamoDB Local (amazon/dynamodb-local, porta 8000, -sharedDb -inMemory)
    - Criar fixture pytest que inicia container, cria tabelas (single-table + GSI), e limpa entre testes
    - Criar conftest.py de integração com client DynamoDB apontando para localhost:8000
    - _Requirements: 13.1, 13.2_

  - [x] 11.2 Implementar testes de integração
    - Testar atomicidade do write path (Property 6): criar entry, verificar todos os itens existem
    - Testar atomicidade em falha (Property 7): forçar falha OCC, verificar nenhum item persistido
    - Testar idempotência (Property 8): submeter mesmo external_id N vezes, verificar mesmo entry_id
    - Testar OCC version increment (Property 9): atualizar balance, verificar version + 1
    - Testar serialização OCC (Property 10): escritas concorrentes, verificar exatamente 1 sucesso
    - Testar hold/release round-trip (Property 12): bloquear e liberar, verificar saldo restaurado
    - Testar ordenação de extrato (Property 14): criar N postings, consultar, verificar ordem cronológica
    - _Requirements: 3.1, 3.2, 4.2, 4.4, 5.3, 5.4, 6.2, 6.3, 8.2, 13.3_

- [ ] 12. Checkpoint — Validar integração
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Implementar infraestrutura Terraform
  - [x] 13.1 Criar módulo DynamoDB (infra/modules/dynamodb)
    - Criar tabela single-table com PK (string), SK (string)
    - Habilitar PITR, DynamoDB Streams (NEW_IMAGE), TTL no campo expires_at
    - Criar GSI-EntryPostings (PK: JOURNAL#{entry_id}, SK: POSTING#{index})
    - Variáveis tipadas com validação, outputs mínimos (table_name, table_arn, stream_arn)
    - _Requirements: 11.1, 11.5, 12.4_

  - [x] 13.2 Criar módulo Lambda (infra/modules/lambda)
    - Módulo para Write Lambda, Read Lambda, Publisher Lambda e Audit Transform Lambda
    - IAM roles com least privilege (DynamoDB, EventBridge, SQS, S3, Firehose)
    - Event Source Mapping para Publisher com filtro "OUTBOX#" e DLQ
    - Event Source Mapping para Audit Transform Lambda com filtro "JOURNAL#" e "ACCOUNT#", batch_size=100, maximum_batching_window_in_seconds=30, e Audit DLQ separada
    - Variáveis tipadas, outputs mínimos (function_arn, role_arn)
    - _Requirements: 12.5, 7.3, 7.4, 10.1, 10.3_

  - [x] 13.3 Criar módulo S3 Audit (infra/modules/s3-audit)
    - Bucket S3 de destino com Object Lock (WORM GOVERNANCE mode), versionamento, criptografia server-side
    - Bucket S3 de erros para registros que falharam na conversão Parquet do Firehose
    - Lifecycle rules para transição de storage class
    - _Requirements: 10.1, 12.6_

  - [x] 13.4 Criar módulo Firehose + Glue (infra/modules/firehose-audit)
    - Kinesis Data Firehose delivery stream com destino extended_s3
    - Configuração de buffer (128MB / 60s), compressão Snappy via Parquet
    - Data format conversion: JSON → Parquet via Glue Table schema
    - Dynamic Partitioning habilitado com JQ metadata extraction (tenant_id)
    - Prefixo S3: `audit/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/tenant=!{partitionKeyFromQuery:tenant_id}/`
    - Error output prefix para bucket de erros
    - Glue Catalog Database e Table com schema do AuditRecord (record_type, entry_id, external_id, entry_type, account_id, amount, direction, currency, posting_index, tenant_id, timestamp, metadata)
    - IAM role para Firehose com acesso a S3 destino, S3 erros e Glue Catalog
    - _Requirements: 10.1, 10.2, 10.3_

  - [x] 13.5 Criar módulo EventBridge (infra/modules/eventbridge)
    - Event bus para eventos do ledger
    - Rules para roteamento de TransactionCreated e TransactionReversed
    - _Requirements: 7.3_

  - [x] 13.5 Criar root module do ambiente dev (infra/environments/dev)
    - Backend remoto S3 com use_lockfile = true
    - required_version e required_providers com versões pinadas
    - Composição dos módulos (dynamodb, lambda, s3-audit, firehose-audit, eventbridge)
    - Variáveis de ambiente, providers explícitos, outputs mínimos
    - Criar versions.tf, providers.tf, variables.tf, outputs.tf, main.tf
    - _Requirements: 12.1, 12.2, 12.3, 12.7_

- [ ] 14. Checkpoint — Validar Terraform
  - Executar `terraform fmt`, `terraform validate` nos módulos e environment dev
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Wiring final e Hypothesis generators compartilhados
  - [x] 15.1 Criar módulo de Hypothesis strategies compartilhadas (tests/property/strategies.py)
    - Implementar `currencies` strategy (BRL, USD, EUR, GBP)
    - Implementar `money_strategy` (Money válido)
    - Implementar `posting_strategy` (Posting válido)
    - Implementar `balanced_postings_strategy` (conjuntos zero-sum)
    - Implementar `journal_entry_strategy` (JournalEntry válido)
    - _Requirements: 1.1, 1.3, 2.1_

  - [x] 15.2 Criar InMemoryLedgerRepository para testes unitários
    - Implementar repositório in-memory que satisfaz LedgerRepository Protocol
    - Simular TransactWriteItems (atomicidade), OCC (version check), idempotência (external_id)
    - _Requirements: 3.1, 4.1, 5.1_

  - [x] 15.3 Integrar todos os componentes no LedgerEngine
    - Verificar que o fluxo completo funciona: API handler → DTO → Command → Engine → Validation → Factory → Repository
    - Verificar que reversões funcionam end-to-end
    - _Requirements: 1.1, 4.1, 9.2_

- [x] 16. Checkpoint final — Validar sistema completo
  - Ensure all tests pass, ask the user if questions arise.

## Notas

- Tarefas marcadas com `*` são opcionais e podem ser puladas para um MVP mais rápido
- Cada tarefa referencia requisitos específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Testes de propriedade validam propriedades universais de corretude (Hypothesis)
- Testes unitários validam exemplos específicos e edge cases
- Testes de integração usam Finch + DynamoDB Local (sem dependência de AWS)
- Linguagem do código: Python 3.11+ | Documentação: Português (BR)
