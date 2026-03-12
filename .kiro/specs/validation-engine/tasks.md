# Plano de Implementação: Validation Engine — Revisado para Infra e Integração Incrementais

## Visão Geral

Implementação progressiva do Motor de Validação Customizável como bounded context separado (`src/validation_engine/`), seguindo **vertical slices incrementais**.

Cada slice entrega:

* código de domínio;
* adaptadores Python mínimos;
* infraestrutura mínima necessária;
* testes unitários;
* property tests relevantes;
* testes de integração local;
* testes de integração em AWS dev.

A estratégia evita que infraestrutura, wiring real, compatibilidade com AWS e testes end-to-end sejam empurrados para o final.

## Princípios do plano

* **Infra first enough**: a infraestrutura mínima de cada capability entra junto com a capability, não depois.
* **Integração dupla contínua**: cada capability relevante deve ser validada localmente e em AWS dev assim que ficar implementável.
* **Sem mocks eternos**: mocks continuam existindo para testes unitários, mas não substituem integração real.
* **Sem gaps entre design e tasks**: tasks cobrem domínio, contratos, runtime, observabilidade, rollout, segurança, compatibilidade, replay e operação.
* **Backwards compatibility monitorada**: o ledger existente continua sendo validado ao longo do plano.

---

# Tasks

* [x] 1. Bootstrap do bounded context, testes e baseline de infraestrutura

  * [x] 1.1 Criar estrutura de diretórios do bounded context `src/validation_engine/`

    * Criar `src/validation_engine/__init__.py`
    * Criar `src/validation_engine/domain/__init__.py`
    * Criar `src/validation_engine/application/__init__.py`
    * Criar `src/validation_engine/infrastructure/__init__.py`
    * Criar `tests/validation_engine/unit/`, `tests/validation_engine/property/`, `tests/validation_engine/integration/local/`, `tests/validation_engine/integration/aws_dev/`
    * Criar `__init__.py` em todos os diretórios necessários
    * *Requisitos: 2.1, 2.2*

  * [x] 1.2 Configurar baseline de testes e markers

    * Configurar `pytest` com markers: `unit`, `property`, `integration_local`, `integration_aws_dev`, `slow`
    * Configurar `Hypothesis` com perfil local e CI
    * Configurar fixtures base para:

      * ambiente local
      * ambiente AWS dev
      * geração de ASTs
      * geração de contextos canônicos
      * geração de bundles/snapshots
    * *Requisitos: 16.1, 16.4, 19.5*

  * [x] 1.3 Configurar baseline de integração local

    * Definir stack local para testes:

      * moto ou localstack para S3/AppConfig quando aplicável
      * DynamoDB Local para integração com ledger
      * diretório temporário para `LKGStore`
    * Criar helpers de bootstrap local
    * *Requisitos: 3.1, 3.2, 17.2, 22.1*

  * [x] 1.4 Configurar baseline de integração AWS dev

    * Definir convenção de naming por ambiente dev
    * Definir variáveis de ambiente para testes AWS dev
    * Definir política de cleanup de artefatos de teste
    * Definir prefixos dedicados de teste para bundles, snapshots e decision trails
    * *Requisitos: 22.1, 22.2, 22.3, 22.6*

  * [x] 1.5 Implementar hierarquia de erros em `src/validation_engine/domain/errors.py`

    * Criar `ValidationEngineError(DomainError)` como base
    * Criar subclasses:

      * `PolicySyntaxError`
      * `PolicySemanticError`
      * `PolicyCostBudgetExceeded`
      * `PolicyBundleUnavailable`
      * `PolicySnapshotUnavailable`
      * `PolicyBundleIntegrityFailure`
      * `PolicyEngineNotReady`
      * `PolicyEvaluationError`
      * `PolicyRejected`
      * `InvalidPolicyBundle`
    * Garantir `code`, `message`, `http_status`
    * *Requisitos: 17.1, 17.3, 17.4, 17.5*

  * [x] 1.6 Escrever testes unitários para erros em `tests/validation_engine/unit/test_errors.py`

    * Testar herança, códigos, mensagens e HTTP status
    * *Requisitos: 17.1, 17.3, 17.4, 17.5*

---

* [-] 2. DSL, AST e modelos do domínio

  * [x] 2.1 Implementar enums e nós do AST em `src/validation_engine/domain/policy_ast.py`

    * `PolicyEffect`
    * `FinalVerdict`
    * `CompositionMode`
    * nós:

      * `LiteralNode`
      * `FieldAccessNode`
      * `RefAccessNode`
      * `CollectionRefNode`
      * `PredicateNode`
      * `AggregateNode`
      * `ComparisonNode`
      * `LogicalOpNode`
      * `NotOpNode`
      * `PolicyRuleNode`
    * type alias `ASTNode`
    * `RuleAST`
    * *Requisitos: 23.1, 23.2, 23.3, 23.4, 23.5, 23.7, 23.8, 23.9*

  * [x] 2.2 Implementar contexto canônico em `src/validation_engine/domain/context.py`

    * `CanonicalPosting`
    * `DerivedFacts`
    * `CanonicalValidationContext`
    * *Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6*

  * [x] 2.3 Implementar modelos em `src/validation_engine/domain/models.py`

    * `PolicyScope`
    * `PolicyActivationManifest`
    * `BundleCompatibility`
    * `CompilationMetadata`
    * `RuleBundle`
    * `ReferenceSnapshot`
    * `ActivePolicySet`
    * `RuleMatchResult`
    * `EvaluationDecision`
    * `EvaluationMetrics`
    * `EvaluationResult`
    * `DecisionSummary`
    * `DecisionTrail`
    * *Requisitos: 4.1, 4.2, 5.1, 5.2, 6.1, 6.2, 11.1, 12.1, 12.2, 13.1, 13.2, 24.1, 24.2*

  * [x] 2.4 Escrever testes unitários para AST e modelos

    * `tests/validation_engine/unit/test_policy_ast.py`
    * `tests/validation_engine/unit/test_models.py`
    * imutabilidade, igualdade estrutural, serialização, lookup, payloads
    * *Requisitos: 12.2, 13.2, 23.9*

  * [x] 2.5 Escrever property test para round-trip do bundle

    * `tests/validation_engine/property/test_bundle_roundtrip.py`
    * **Property 13: Round-trip do bundle**
    * *Requisitos: 3.3, 24.2*

---

* [x] 3. Infra mínima incremental — storage local e AWS dev para bundles/snapshots

  * [x] 3.1 Criar módulo Terraform `infra/modules/validation-engine-s3/`

    * bucket principal com:

      * Object Lock
      * versionamento
      * SSE-KMS
      * prefixos `bundles/` e `snapshots/`
    * bucket de erro dedicado
    * IAM least privilege para leitura e escrita
    * `variables.tf`, `outputs.tf`, `README` do módulo
    * *Requisitos: 3.1, 3.2, 3.6, 20.1, 22.1, 22.7*

  * [x] 3.2 Compor storage do Validation Engine no environment dev

    * adicionar módulo em `infra/environments/dev/`
    * configurar state separado ou prefixo distinto
    * executar `terraform fmt`, `terraform validate`, `terraform plan`
    * executar o deploy no dev, terraform apply
    * *Requisitos: 22.1, 22.5, 22.6*

  * [x] 3.3 Implementar `BundleStore` e `SnapshotStore`

    * `src/validation_engine/infrastructure/bundle_store.py`
    * `src/validation_engine/infrastructure/snapshot_store.py`
    * idempotência por chave
    * serialização determinística
    * *Requisitos: 3.1, 3.2, 3.3, 3.4*

  * [x] 3.4 Implementar `BundleLoader` e `SnapshotLoader`

    * cache local em memória
    * verificação de integridade
    * compatibilidade de schema
    * *Requisitos: 3.3, 3.4, 11.6, 17.3, 20.3, 20.4*

  * [x] 3.5 Escrever testes unitários dos stores/loaders

    * armazenamento idempotente
    * carga bem-sucedida
    * hash divergente
    * snapshot incompatível
    * *Requisitos: 3.1, 3.2, 17.3, 20.3*

  * [x] 3.6 Escrever integração local storage em `tests/validation_engine/integration/local/test_bundle_snapshot_loading.py`

    * round-trip bundle store/load
    * round-trip snapshot store/load
    * integridade
    * idempotência
    * *Requisitos: 3.1, 3.2, 3.3, 3.4*

  * [x] 3.7 Escrever integração AWS dev storage em `tests/validation_engine/integration/aws_dev/test_aws_s3_storage.py`

    * bundle store/load real
    * snapshot store/load real
    * KMS ativo
    * prefixes de teste
    * *Requisitos: 3.1, 3.2, 3.6, 20.1*

  * [x] 3.8 Escrever property test de integridade

    * `tests/validation_engine/property/test_integrity.py`
    * **Property 10: Bundle e snapshot só entram em runtime se íntegros**
    * *Requisitos: 17.3, 17.4, 20.3, 20.4*

---

* [x] 4. Compiler, semantic analyzer e cost analyzer

  * [x] 4.1 Implementar `DSLCompiler` e `DSLPrettyPrinter` em `src/validation_engine/domain/compiler.py`

    * parser da DSL
    * geração de `RuleAST`
    * geração de `RuleBundle`
    * geração de `artifact_hash`
    * *Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6*

  * [x] 4.2 Implementar `SemanticAnalyzer`

    * tipos
    * escopos
    * namespaces permitidos
    * referências proibidas
    * construções não-determinísticas
    * *Requisitos: 15.1, 15.2, 15.3, 15.4, 23.6*

  * [x] 4.3 Implementar `PolicyCostAnalyzer` em `src/validation_engine/domain/cost_analyzer.py`

    * limites de:

      * número de rules
      * profundidade
      * agregações por rule
      * tamanho fonte
      * scans por avaliação
      * campos em `policy_context`
    * *Requisitos: 15.5, 15.6, 19.1*

  * [x] 4.4 Escrever testes unitários do compiler/semantic/cost analyzer

    * parsing válido
    * erro sintático
    * erro semântico
    * erro por custo
    * pretty print round-trip
    * *Requisitos: 2.1, 2.2, 2.5, 15.1, 15.5*

  * [x] 4.5 Escrever integração local compile-store-load

    * `tests/validation_engine/integration/local/test_compile_store_load.py`
    * compile DSL → store bundle → load bundle
    * *Requisitos: 2.1, 2.4, 3.1, 3.3*

  * [x] 4.6 Escrever integração AWS dev compile-store-load

    * `tests/validation_engine/integration/aws_dev/test_aws_compile_store_load.py`
    * compile real → armazenar em S3 dev → recarregar → verificar hash
    * *Requisitos: 2.4, 3.1, 3.6*

---

* [x] 5. RuleEvaluator

  * [x] 5.1 Implementar `RuleEvaluator` em `src/validation_engine/domain/evaluator.py`

    * tree-walking interpreter
    * avaliação dos nós
    * resolução de `facts.*`, `policy_context.*`, `ref.*`, `postings.*`
    * semântica `DENY_OVERRIDES`
    * métricas de latência sem participar da decisão semântica
    * rejeição de bundles inválidos/incompatíveis
    * *Requisitos: 9.1, 9.2, 9.3, 9.5, 10.2, 10.3, 10.4, 10.5, 10.6*

  * [x] 5.2 Escrever testes unitários para `RuleEvaluator`

    * deny
    * approve
    * deny_overrides
    * agregações
    * facts/policy_context/ref
    * incompatibilidade de bundle
    * *Requisitos: 9.1, 9.3, 10.3, 10.4, 10.5*

  * [x] 5.3 Escrever property tests

    * `test_determinism.py`
    * `test_deny_overrides.py`
    * *Requisitos: 9.3, 10.3, 10.4*

  * [x] 5.4 Escrever integração local evaluator slice

    * `tests/validation_engine/integration/local/test_local_evaluator_slice.py`
    * bundle compilado + snapshot + contexto → evaluation real
    * *Requisitos: 9.1, 9.3, 10.3*

  * [x] 5.5 Escrever integração AWS dev evaluator slice

    * `tests/validation_engine/integration/aws_dev/test_aws_evaluator_slice.py`
    * carregar bundle/snapshot reais do dev e avaliar localmente com runtime real
    * *Requisitos: 9.1, 9.3, 11.2*

---

* [x] 6. Context builder e property tests de canonicalização

  * [x] 6.1 Implementar `CanonicalValidationContextBuilder`

    * conversão do comando
    * cálculo de `DerivedFacts`
    * isolamento entre `policy_context` e `metadata`
    * *Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6*

  * [x] 6.2 Escrever testes unitários do context builder

    * *Requisitos: 8.1, 8.2, 8.3, 8.4*

  * [x] 6.3 Escrever property test de canonicalização

    * **Property 12: CanonicalValidationContext é estável**
    * *Requisitos: 8.5, 14.1*

  * [x] 6.4 Escrever integração local do context builder com evaluator

    * comando → contexto → avaliação
    * *Requisitos: 8.1, 9.1*

---

* [x] 7. Infra incremental — AppConfig e manifestos

  * [x] 7.1 Criar módulo Terraform `infra/modules/appconfig-validation/`

    * AppConfig Application
    * Environment
    * Configuration Profile
    * estratégia por ambiente
    * IAM roles/policies
    * *Requisitos: 4.1, 22.2, 22.7*

  * [x] 7.2 Compor AppConfig no environment dev

    * integrar ao `infra/environments/dev/`
    * validar plan
    * aplicar no dev
    * *Requisitos: 4.1, 22.2*

  * [x] 7.3 Implementar `ManifestResolver`

    * leitura do manifesto por escopo
    * parsing de payload
    * múltiplos escopos
    * validação estrutural do manifesto
    * *Requisitos: 4.3, 4.4, 5.1, 5.3*

  * [x] 7.4 Implementar `PolicyPublisher`

    * gerar `PolicyActivationManifest`
    * validar compatibilidade bundle/snapshot
    * publicar no AppConfig
    * *Requisitos: 4.1, 4.2, 4.3, 4.5, 24.3, 24.4*

  * [x] 7.5 Escrever testes unitários de resolver/publisher

    * *Requisitos: 4.3, 4.4, 24.4*

  * [x] 7.6 Escrever integração local de manifestos

    * `tests/validation_engine/integration/local/test_manifest_resolution.py`
    * payload mockado/localstack com múltiplos escopos
    * *Requisitos: 4.3, 5.1, 5.3*

  * [x] 7.7 Escrever integração AWS dev de AppConfig

    * `tests/validation_engine/integration/aws_dev/test_aws_appconfig_manifest.py`
    * publicar manifesto real
    * resolver manifesto real
    * validar deployment no dev
    * *Requisitos: 4.3, 22.2*

  * [x] 7.8 Escrever property tests

    * `test_activation_atomicity.py`
    * `test_golden_gate.py`
    * *Requisitos: 4.4, 4.5, 16.5*

---

* [x] 8. Runtime registry, LKG e bootstrap do runtime

  * [x] 8.1 Implementar `LKGStore`

    * salvar/carregar em `/tmp` ou diretório configurável
    * controle de uso só após boot válido
    * *Requisitos: 17.2*

  * [x] 8.2 Implementar `PolicyRuntimeRegistry`

    * cache por `policy_scope_id`
    * refresh por `activation_id`
    * load bundle/snapshot
    * validação de integridade/compatibilidade
    * swap atômico
    * fallback para LKG
    * cold start sem policy válida → erro
    * *Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5, 17.1, 17.2, 17.3, 17.4*

  * [x] 8.3 Escrever testes unitários do runtime registry

    * *Requisitos: 6.1, 6.3, 17.1, 17.2*

  * [x] 8.4 Escrever property test do LKG

    * **Property 11: Last Known Good só é usado após boot válido**
    * *Requisitos: 17.1, 17.2*

  * [x] 8.5 Escrever integração local do runtime registry

    * `tests/validation_engine/integration/local/test_runtime_registry.py`
    * bootstrap válido
    * refresh
    * swap atômico
    * fallback LKG
    * *Requisitos: 6.1, 6.4, 17.1, 17.2*

  * [x] 8.6 Escrever integração AWS dev do runtime registry

    * `tests/validation_engine/integration/aws_dev/test_aws_runtime_registry.py`
    * manifesto real + S3 real + bootstrap real
    * *Requisitos: 6.1, 6.2, 17.1, 17.2*

---

* [x] 9. Facade e trail emitter

  * [x] 9.1 Implementar `DecisionTrailEmitter`

    * serialização
    * best-effort
    * logs de falha
    * *Requisitos: 13.1, 13.3, 13.4*

  * [x] 9.2 Implementar `PolicyValidationFacade`

    * build context
    * resolve scope
    * get active policy set
    * evaluate
    * build summary
    * build trail
    * emit trail
    * return `ValidationResult`
    * sem mutar comando
    * *Requisitos: 7.1, 7.2, 7.3, 7.5, 12.1, 12.3, 13.1, 13.3, 13.4, 17.1*

  * [x] 9.3 Escrever testes unitários da facade e do emitter

    * aprovação
    * rejeição
    * runtime não pronto
    * falha de emitter
    * comando imutável
    * *Requisitos: 7.1, 7.2, 7.3, 13.4, 17.1*

  * [x] 9.4 Escrever property tests

    * `test_no_command_mutation.py`
    * `test_trail_failure_isolation.py`
    * *Requisitos: 7.5, 13.4*

  * [x] 9.5 Escrever integração local da facade

    * `tests/validation_engine/integration/local/test_local_policy_facade.py`
    * pipeline completo local sem ledger ainda
    * *Requisitos: 7.1, 12.1, 13.1*

  * [x] 9.6 Escrever integração AWS dev da facade

    * `tests/validation_engine/integration/aws_dev/test_aws_policy_facade.py`
    * com manifesto real, S3 real e trail emitter real ou isolável
    * *Requisitos: 7.1, 12.1, 13.1*

---

* [x] 10. Infra incremental — Firehose e analytics trail

  * [x] 10.1 Criar módulo Terraform `infra/modules/firehose-decision-trail/`

    * Firehose
    * Glue DB/Table
    * Parquet + Snappy
    * partição por `year/month/day/tenant_id/policy_scope_id`
    * bucket de erro
    * IAM
    * *Requisitos: 13.5, 21.1, 21.3, 21.4, 21.5, 22.3, 22.7*

  * [x] 10.2 Criar módulo Terraform `infra/modules/cloudwatch-validation-alarms/`

    * alarmes:

      * policy engine not ready
      * falha de refresh
      * integridade
      * falha emitter
      * rejeições anômalas
    * *Requisitos: 18.3, 18.5, 22.4, 22.7*

  * [x] 10.3 Compor Firehose e alarmes no environment dev

    * plan/apply em dev
    * *Requisitos: 21.1, 22.3, 22.4*

  * [x] 10.4 Escrever integração local do emitter

    * `tests/validation_engine/integration/local/test_local_decision_trail_emitter.py`
    * payload válido e falha silenciosa
    * *Requisitos: 13.3, 13.4*

  * [x] 10.5 Escrever integração AWS dev do emitter/Firehose

    * `tests/validation_engine/integration/aws_dev/test_aws_firehose_trail.py`
    * emitir trail real
    * verificar chegada ao destino
    * verificar partição
    * *Requisitos: 13.5, 21.1, 21.4*

---

* [x] 11. Evolução do contrato com o ledger

  * [x] 11.1 Evoluir `ValidationResult` e `ValidationArtifacts`

    * *Requisitos: 7.2, 12.3*

  * [x] 11.2 Evoluir `CreateJournalEntryCommand`

    * adicionar `tenant_id`
    * adicionar `policy_context`
    * *Requisitos: 5.1, 8.2*

  * [x] 11.3 Evoluir `JournalEntryFactory`

    * persistir `DecisionSummary`
    * *Requisitos: 12.3, 12.4, 12.5*

  * [x] 11.4 Evoluir `LedgerEngine`

    * passar `ValidationArtifacts`
    * *Requisitos: 12.4*

  * [x] 11.5 Injetar `PolicyValidationFacade` na `ValidationChain`

    * após validadores estruturais
    * *Requisitos: 1.3, 1.4, 7.4*

  * [x] 11.6 Escrever testes unitários/contrato do ledger

    * backward compatibility
    * propagation de artifacts
    * persistência do summary
    * *Requisitos: 7.6, 12.3, 12.4*

  * [x] 11.7 Escrever property tests

    * `test_summary_atomicity.py`
    * `test_structural_isolation.py`
    * `test_scope_isolation.py`
    * *Requisitos: 1.1, 1.3, 5.5, 12.4*

---

* [x] 12. Integração local com ledger

  * [x] 12.1 Escrever teste end-to-end local com DynamoDB Local

    * `tests/validation_engine/integration/local/test_local_ledger_integration.py`
    * fluxo:

      * comando
      * chain
      * facade
      * factory
      * repository
      * persistência
    * verificar:

      * summary no metadata
      * validadores estruturais antes da policy
      * rejeição por policy
      * backward compatibility
    * *Requisitos: 1.3, 1.4, 7.1, 7.2, 7.3, 7.4, 12.4*

  * [x] 12.2 Escrever teste de replay local

    * `tests/validation_engine/integration/local/test_local_replay.py`
    * usar `JournalEntry` + `policy_context` + `DecisionSummary` + `RuleBundle` + `ReferenceSnapshot`
    * *Requisitos: 14.1, 14.3*

---

* [x] 13. Integração AWS dev com ledger

  * [x] 13.1 Provisionar recursos mínimos adicionais no dev, se necessário

    * wiring com lambda/role/variáveis de ambiente
    * permissões AppConfig/S3/KMS/Firehose
    * *Requisitos: 20.5, 22.7*

  * [x] 13.2 Escrever teste end-to-end AWS dev

    * `tests/validation_engine/integration/aws_dev/test_aws_e2e.py`
    * fluxo real:

      * compile
      * store bundle/snapshot
      * publish manifesto
      * bootstrap runtime
      * evaluate
      * persistir summary no DynamoDB
      * emitir trail ao Firehose
    * verificar:

      * summary no ledger
      * trail no S3
      * integridade
      * partição
    * *Requisitos: 2.1, 3.1, 4.3, 9.1, 12.4, 13.5, 21.1*

  * [x] 13.3 Escrever teste AWS dev de rollback

    * publicar manifesto novo
    * reverter para manifesto anterior
    * validar que runtime volta ao bundle anterior
    * *Requisitos: 24.4, 24.5, 24.6*

---

* [ ] 14. Observabilidade, métricas e logs

  * [ ] 14.1 Instrumentar logs estruturados no runtime

    * facade
    * runtime registry
    * bundle/snapshot loaders
    * emitter
    * *Requisitos: 18.1, 18.2, 18.4*

  * [ ] 14.2 Instrumentar métricas

    * avaliação
    * rejeição
    * falhas
    * bootstrap
    * refresh
    * emitter
    * *Requisitos: 18.3, 19.4*

  * [ ] 14.3 Escrever testes unitários/integração de observabilidade

    * presença de campos obrigatórios
    * *Requisitos: 18.1, 18.2, 18.3*

  * [ ] 14.4 Escrever validação em AWS dev de alarmes/métricas

    * smoke test de publicação de métricas e logs
    * *Requisitos: 18.3, 18.5, 22.4*

---

* [ ] 15. Performance e budgets

  * [ ] 15.1 Criar benchmark local do evaluator e facade

    * medir:

      * build context
      * get active policy set
      * evaluate
      * build summary
      * emit trail best-effort
    * *Requisitos: 19.1, 19.2, 19.4*

  * [ ] 15.2 Criar smoke performance em AWS dev

    * avaliar latência sob carga moderada
    * validar budgets
    * *Requisitos: 19.1, 19.2, 19.5*

---

* [ ] 16. Suíte final de rastreabilidade e validação

  * [ ] 16.1 Executar unit + property + integration_local + integration_aws_dev
  * [ ] 16.2 Verificar que o ledger legado continua passando
  * [ ] 16.3 Verificar cobertura dos requisitos e properties
  * [ ] 16.4 Produzir matriz de rastreabilidade:

    * Requirement → Task
    * Requirement → Test
    * Property → Test
  * [ ] 16.5 Revisar cleanup e custo dos testes AWS dev

---

# Correções estruturais aplicadas ao seu plano

Estas foram as mudanças principais feitas no plano original:

1. **Terraform e AWS dev entraram cedo**
   Storage S3 e AppConfig não ficaram para o final. Eles agora aparecem antes do runtime completo, porque sem isso você só valida abstrações.

2. **Integração local e AWS dev por slice**
   Para storage, compiler/store/load, manifestos, runtime registry, facade, Firehose e ledger integration, agora existe:

   * teste unitário,
   * integração local,
   * integração AWS dev.

3. **Tasks opcionais continuam marcadas, mas a espinha dorsal de integração não ficou opcional**
   Eu deixei opcionais principalmente alguns property tests e testes complementares. A validação real de storage/AppConfig/runtime/ledger em local e AWS dev não deve ser tratada como “nice to have”.

4. **Nenhum detalhe crítico do design/requisitos ficou sem task explícita**
   Agora o plano cobre explicitamente:

   * `PolicyActivationManifest`
   * `ActivePolicySet`
   * `LKG`
   * `context_schema_version`
   * `evaluator_version`
   * `DecisionSummary`
   * `DecisionTrail`
   * `DENY_OVERRIDES`
   * replay
   * rollback
   * observabilidade
   * budgets de custo
   * smoke performance
   * alarmes
   * least privilege
   * prefixes e cleanup para AWS dev

5. **A ordem foi ajustada para reduzir risco**
   Você não vai descobrir no final que:

   * serialização do bundle quebra no S3 real,
   * AppConfig real tem diferença de payload/deployment,
   * Firehose/Glue tem incompatibilidade,
   * runtime registry não funciona com recursos reais,
   * o ledger não propaga `DecisionSummary` corretamente.

