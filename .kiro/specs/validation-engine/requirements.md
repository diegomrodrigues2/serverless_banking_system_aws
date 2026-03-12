# Documento de Requisitos — Motor de Validação Customizável (Validation Engine)

## Introdução

O Motor de Validação Customizável é um bounded context separado responsável exclusivamente por **regras configuráveis de policy** aplicadas ao subledger de partidas dobradas existente.

O sistema opera em três camadas claramente separadas:

* **Structural Validation Layer**: invariantes estruturais do ledger, implementadas no bounded context do subledger e fora da DSL.
* **Control Plane**: autoria, compilação, análise semântica, análise de custo, testes e ativação de policies.
* **Data Plane**: avaliação determinística das policies no hot path do Write Path.

A arquitetura garante que a avaliação propriamente dita seja uma **função pura**: sem I/O, sem efeitos colaterais e sem não-determinismo. A política ativa é materializada em memória como um `ActivePolicySet`, derivado de um `PolicyActivationManifest` que referencia conjuntamente:

* `artifact_hash` do `Rule_Bundle`
* `snapshot_version` do `Reference_Snapshot`
* `context_schema_version`
* `evaluator_version`

O motor integra-se ao subledger como uma `ValidationStrategy` injetada na `ValidationChain`, após as validações estruturais. O resultado da policy validation é retornado como artefato explícito (`Decision_Summary`), sem mutação do comando original.

## Glossário

* **Validation_Engine**: bounded context responsável por avaliar policies configuráveis contra transações do subledger.
* **Structural_Validation_Layer**: camada do subledger que executa invariantes estruturais do domínio, como zero-sum, minor units, isolamento de tenant e limites técnicos.
* **Control_Plane**: camada responsável por autoria, compilação, testes, armazenamento e ativação de policies.
* **Data_Plane**: camada responsável pela avaliação determinística das policies no hot path.
* **Policy_Rule_DSL**: linguagem declarativa restrita para definição de rules de policy.
* **Rule_AST**: árvore sintática tipada derivada da DSL.
* **Rule_Bundle**: artefato compilado e imutável contendo AST, plano de execução, metadados e compatibilidade.
* **Artifact_Hash**: hash SHA-256 do conteúdo serializado do `Rule_Bundle`.
* **Reference_Snapshot**: snapshot imutável e versionado com dados auxiliares usados durante a avaliação.
* **Policy_Activation_Manifest**: manifesto atômico que define qual combinação de bundle, snapshot e versões do runtime está ativa para um escopo.
* **Policy_Scope**: escopo de aplicação de uma policy, composto por tenant, operação, produto, canal e ambiente.
* **Active_Policy_Set**: conjunto materializado em memória contendo manifesto, bundle e snapshot compatíveis e já validados.
* **Canonical_Validation_Context**: representação canônica, tipada e estável do comando visível à DSL.
* **Derived_Facts**: fatos derivados calculados a partir do comando canônico para simplificar a DSL e reduzir custo de avaliação.
* **Rule_Evaluator**: componente puro que avalia rules sobre `Canonical_Validation_Context` e `Active_Policy_Set`.
* **ValidationStrategy**: interface implementada pelo Validation Engine para integração com a `ValidationChain`.
* **ValidationChain**: cadeia de validação do subledger.
* **Decision_Summary**: resumo mínimo e persistível da decisão de policy, gravado atomicamente com o `JournalEntry`.
* **Decision_Trail**: trilha detalhada e expandida da avaliação, emitida de forma assíncrona para auditoria e analytics.
* **Last_Known_Good (LKG)**: último `Active_Policy_Set` válido materializado localmente e reutilizável em caso de falha de refresh após bootstrap bem-sucedido.
* **Golden_Test**: teste determinístico executado no Control Plane contra bundle e snapshot específicos.
* **Composition_Mode**: estratégia de composição das rules. Neste sistema, o modo padrão é `DENY_OVERRIDES`.

---

## Requisitos

### Requisito 1: Separação entre validações estruturais e policies configuráveis

**User Story:** Como arquiteto do subledger, eu quero que as invariantes estruturais do ledger permaneçam fora da DSL, para que regras essenciais do domínio não se tornem configuração mutável.

#### Critérios de Aceitação

1. THE Structural_Validation_Layer SHALL continuar responsável por invariantes do ledger, incluindo no mínimo: zero-sum por moeda, minor units válidos, limites técnicos de transação, isolamento de tenant e integridade de reversão.
2. THE Validation_Engine SHALL ser usado apenas para regras configuráveis de policy e SHALL NOT substituir validações estruturais do subledger.
3. THE Validation_Engine SHALL ser executado somente após a aprovação das validações estruturais na `ValidationChain`.
4. IF uma transação violar uma invariante estrutural, THEN a rejeição SHALL ocorrer antes da execução do Validation_Engine.
5. THE documentação de schema da DSL SHALL distinguir explicitamente campos de policy de invariantes do domínio.

### Requisito 2: Parsing e compilação de Policy Rule DSL (Control Plane)

**User Story:** Como especialista de domínio, eu quero escrever policies em uma DSL declarativa e compilá-las em artefatos imutáveis, para que regras sejam validadas semanticamente antes de entrar em produção.

#### Critérios de Aceitação

1. WHEN uma policy válida em `Policy_Rule_DSL` é submetida ao compilador, THE DSL_Compiler SHALL parsear a entrada e gerar um `Rule_AST` tipado.
2. WHEN a DSL contém erro de sintaxe, THE DSL_Compiler SHALL rejeitar a compilação com erro estruturado `POLICY_SYNTAX_ERROR`, incluindo linha e coluna.
3. WHEN a DSL referencia construções não-determinísticas, THEN THE DSL_Compiler SHALL rejeitar a compilação com erro `NON_DETERMINISTIC_FUNCTION`.
4. WHEN o `Rule_AST` passa em análise semântica e análise de custo, THE DSL_Compiler SHALL gerar um `Rule_Bundle` imutável.
5. THE `DSL_Pretty_Printer` SHALL converter um `Rule_AST` válido novamente para DSL textual válida.
6. FOR ALL `Rule_AST` válidos, parsear a saída do `DSL_Pretty_Printer` SHALL produzir AST semanticamente equivalente ao original.

### Requisito 3: Armazenamento imutável de Rule Bundles e Snapshots (Control Plane)

**User Story:** Como engenheiro de plataforma, eu quero armazenar bundles e snapshots de forma imutável e auditável, para que nenhuma policy publicada possa ser alterada retroativamente.

#### Critérios de Aceitação

1. WHEN um `Rule_Bundle` é compilado com sucesso, THE Control_Plane SHALL armazená-lo em S3 com Object Lock (WORM) indexado por `artifact_hash`.
2. WHEN um `Reference_Snapshot` é criado, THE Control_Plane SHALL armazená-lo em S3 com Object Lock (WORM) indexado por `snapshot_version`.
3. THE Control_Plane SHALL garantir que `artifact_hash` seja SHA-256 do conteúdo serializado do `Rule_Bundle`, excluindo o próprio campo de hash.
4. WHEN um artefato com mesmo identificador lógico já existir, THE armazenamento SHALL ser idempotente e SHALL NOT criar duplicatas.
5. THE Control_Plane SHALL armazenar bundles e snapshots em JSON UTF-8 ou outro formato serializado documentado e determinístico.
6. THE buckets SHALL usar KMS Envelope Encryption e versionamento habilitado.

### Requisito 4: Ativação atômica via Policy Activation Manifest (Control Plane + Data Plane)

**User Story:** Como engenheiro de plataforma, eu quero ativar policies por meio de um manifesto atômico, para que bundle, snapshot e versões compatíveis sejam usados em conjunto.

#### Critérios de Aceitação

1. THE sistema SHALL representar a configuração ativa por meio de um `Policy_Activation_Manifest`.
2. THE `Policy_Activation_Manifest` SHALL conter no mínimo: `activation_id`, `policy_scope_id`, `artifact_hash`, `snapshot_version`, `context_schema_version`, `evaluator_version`, `activated_at` e `activated_by`.
3. WHEN uma nova policy é ativada, THE Control_Plane SHALL publicar um novo manifesto, e SHALL NOT publicar apenas `artifact_hash` isoladamente.
4. THE Data_Plane SHALL tratar `artifact_hash`, `snapshot_version`, `context_schema_version` e `evaluator_version` como unidade indivisível de ativação.
5. THE sistema SHALL impedir avaliação com combinação cruzada de bundle e snapshot incompatíveis.
6. THE histórico de manifestos SHALL ser preservado para auditoria e rollback.

### Requisito 5: Resolução de policy ativa por escopo (Data Plane)

**User Story:** Como engenheiro de plataforma, eu quero resolver a policy ativa por escopo operacional, para que tenants, produtos e canais distintos possam ter regras diferentes.

#### Critérios de Aceitação

1. THE Data_Plane SHALL resolver policy ativa por `Policy_Scope`.
2. THE `Policy_Scope` SHALL incluir no mínimo `tenant_id` e `operation_type`, e MAY incluir `product_code`, `channel` e `environment`.
3. WHEN uma transação é avaliada, THE Validation_Engine SHALL determinar o `policy_scope_id` aplicável antes da avaliação.
4. THE Data_Plane SHALL suportar múltiplos escopos ativos simultaneamente em uma mesma implantação.
5. THE sistema SHALL garantir que nenhuma policy de outro tenant ou escopo seja aplicada a uma transação fora de seu escopo.

### Requisito 6: Materialização local do Active Policy Set (Data Plane)

**User Story:** Como engenheiro de plataforma, eu quero que a política ativa seja materializada em memória antes da avaliação, para que o hot path não dependa de resolução dinâmica por request.

#### Critérios de Aceitação

1. THE Data_Plane SHALL materializar a policy ativa como um `Active_Policy_Set` em memória local.
2. THE `Active_Policy_Set` SHALL conter: manifesto ativo, `Rule_Bundle`, `Reference_Snapshot`, `loaded_at` e evidência de integridade validada.
3. IN steady state, THE request path SHALL consumir apenas o `Active_Policy_Set` já carregado em memória.
4. THE troca de `Active_Policy_Set` SHALL ocorrer por swap atômico.
5. THE avaliação em si SHALL NOT carregar bundle ou snapshot diretamente do S3.

### Requisito 7: Integração com ValidationChain via Strategy Pattern (Data Plane)

**User Story:** Como engenheiro de plataforma, eu quero que o motor se integre à cadeia de validação existente como uma `ValidationStrategy`, para que o fluxo do subledger permaneça consistente.

#### Critérios de Aceitação

1. THE Validation_Engine SHALL implementar o protocolo `ValidationStrategy` existente.
2. WHEN a policy valida a transação, THE Validation_Engine SHALL retornar `ValidationResult.success()` com artefatos explícitos.
3. WHEN a policy rejeita a transação, THE Validation_Engine SHALL levantar `DomainError` com código `POLICY_REJECTED`.
4. THE Validation_Engine SHALL ser injetado na `ValidationChain` após os validadores estruturais.
5. THE Validation_Engine SHALL NOT mutar o comando original.
6. THE `ValidationResult` SHALL ser capaz de carregar `Decision_Summary` ou artefatos equivalentes para persistência posterior.

### Requisito 8: Construção de contexto canônico de avaliação (Data Plane)

**User Story:** Como engenheiro de plataforma, eu quero construir um contexto canônico e tipado antes da avaliação, para que a DSL opere sobre dados estáveis, reproduzíveis e independentes do formato externo da API.

#### Critérios de Aceitação

1. THE Data_Plane SHALL construir um `Canonical_Validation_Context` antes da execução do `Rule_Evaluator`.
2. THE `Canonical_Validation_Context` SHALL conter apenas dados explicitamente permitidos para consumo pela DSL.
3. THE `Canonical_Validation_Context` SHALL conter namespace tipado para `postings`, `policy_context`, `facts` e `ref`.
4. THE sistema SHALL calcular `Derived_Facts` antes da avaliação e disponibilizá-los no contexto.
5. THE canonicalização SHALL ser determinística para inputs semanticamente equivalentes.
6. THE `context_schema_version` SHALL ser incluído no contexto e validado contra a compatibilidade do bundle.

### Requisito 9: Execução determinística como função pura (Data Plane)

**User Story:** Como engenheiro de plataforma, eu quero que a avaliação de policy seja uma função pura, para permitir replay e auditoria reproduzível.

#### Critérios de Aceitação

1. THE `Rule_Evaluator` SHALL depender exclusivamente de `Canonical_Validation_Context` e `Active_Policy_Set`.
2. THE `Rule_Evaluator` SHALL executar zero operações de I/O durante a avaliação.
3. FOR ALL combinações válidas de contexto e policy set, duas avaliações SHALL produzir a mesma decisão semântica.
4. Campos efêmeros, como latência, SHALL NOT participar da definição de igualdade semântica.
5. THE `Rule_Evaluator` SHALL rejeitar bundles inválidos ou incompatíveis com erro estruturado apropriado.
6. THE avaliação em memória SHALL atender aos objetivos de latência definidos nos requisitos de performance.

### Requisito 10: Composição de rules com semântica DENY_OVERRIDES (Data Plane)

**User Story:** Como arquiteto, eu quero que a composição de rules tenha semântica simples e segura, para evitar ambiguidades entre regras que aprovam e rejeitam.

#### Critérios de Aceitação

1. THE `Rule_Bundle` SHALL declarar explicitamente seu `Composition_Mode`.
2. THE modo padrão suportado pelo sistema SHALL ser `DENY_OVERRIDES`.
3. IF ao menos uma rule de efeito `DENY` casar, THEN o veredito final SHALL ser `REJECTED`.
4. IF nenhuma rule `DENY` casar, THEN o veredito final SHALL ser `APPROVED`.
5. Regras `ALLOW` MAY existir para classificação, rastreabilidade ou extensibilidade, mas SHALL NOT sobrepor uma negação.
6. O `EvaluationResult` SHALL registrar quais rules casaram e qual `DENY` determinou o veredito final, quando aplicável.

### Requisito 11: Reference Snapshot para dados auxiliares (Data Plane)

**User Story:** Como especialista de domínio, eu quero que policies referenciem dados auxiliares imutáveis e versionados, para que a avaliação seja determinística sem depender de I/O.

#### Critérios de Aceitação

1. THE `Reference_Snapshot` SHALL ser um artefato imutável e versionado.
2. THE Data_Plane SHALL manter o snapshot ativo em memória como parte do `Active_Policy_Set`.
3. THE `Rule_Evaluator` SHALL consultar exclusivamente o snapshot em memória durante a avaliação.
4. THE Control_Plane SHALL armazenar snapshots com as mesmas garantias de imutabilidade e proteção aplicadas aos bundles.
5. THE `snapshot_version` SHALL participar do modelo de replay e da trilha de auditoria.
6. THE sistema SHALL validar compatibilidade entre `snapshot_schema_version`, bundle e contexto antes da ativação local.

### Requisito 12: Decision Summary persistido atomicamente (Integração com Ledger)

**User Story:** Como auditor e engenheiro de plataforma, eu quero que a decisão mínima de policy seja persistida junto com a transação, para que a rastreabilidade exista mesmo se o trail detalhado falhar.

#### Critérios de Aceitação

1. WHEN a policy validation aprova uma transação, THE Validation_Engine SHALL produzir um `Decision_Summary`.
2. THE `Decision_Summary` SHALL conter no mínimo: `final_verdict`, `policy_scope_id`, `activation_id`, `artifact_hash`, `snapshot_version`, `evaluator_version`, `input_hash`, `matched_deny_rule` e `evaluation_latency_ms`.
3. THE Validation_Engine SHALL retornar o `Decision_Summary` ao `LedgerEngine` via `ValidationResult` ou artefato equivalente.
4. THE `LedgerEngine` SHALL persistir o `Decision_Summary` atomicamente junto com o `JournalEntry`.
5. THE Validation_Engine SHALL NOT escrever diretamente no banco de dados.
6. IF a transação for rejeitada antes de persistência, THEN nenhum `Decision_Summary` SHALL ser persistido no ledger.

### Requisito 13: Decision Trail detalhado para auditoria e analytics (Data Plane)

**User Story:** Como auditor financeiro, eu quero uma trilha detalhada da avaliação, para reproduzir decisões e investigar comportamentos históricos.

#### Critérios de Aceitação

1. WHEN uma policy é avaliada, THE Validation_Engine SHALL gerar um `Decision_Trail` detalhado.
2. THE `Decision_Trail` SHALL conter no mínimo: `external_id`, `tenant_id`, `policy_scope_id`, `activation_id`, `artifact_hash`, `snapshot_version`, `evaluator_version`, `input_hash`, `final_verdict`, lista de rules avaliadas, `matched_deny_rule`, `evaluation_latency_ms`, `timestamp` e `error_code` quando aplicável.
3. THE `Decision_Trail` SHALL ser emitido de forma assíncrona e best-effort.
4. IF a emissão do `Decision_Trail` falhar, THEN a transação aprovada SHALL continuar válida desde que o `Decision_Summary` tenha sido entregue ao `LedgerEngine`.
5. THE payload completo SHALL ser enviado ao pipeline de analytics/auditoria para armazenamento em S3 consultável via Athena.
6. THE `Decision_Trail` SHALL NOT ser tratado como requisito de atomicidade da transação contábil.

### Requisito 14: Replay e reprodutibilidade de decisões

**User Story:** Como auditor financeiro, eu quero reproduzir decisões passadas com fidelidade, para validar comportamento histórico do motor.

#### Critérios de Aceitação

1. THE sistema SHALL suportar replay de uma decisão usando: contexto persistido da transação, `Decision_Summary`, `Rule_Bundle` identificado por `artifact_hash` e `Reference_Snapshot` identificado por `snapshot_version`.
2. THE `input_hash` SHALL ser usado para verificação de integridade e correlação, e SHALL NOT ser tratado como mecanismo suficiente para reconstrução do input.
3. FOR ALL replays executados com contexto, bundle e snapshot compatíveis, o veredito SHALL ser semanticamente idêntico ao original.
4. THE Control_Plane SHALL suportar replay em testes e investigações operacionais.
5. THE modelo de replay SHALL registrar divergências caso uma versão incompatível do evaluator seja usada.

### Requisito 15: Análise semântica e análise de custo da DSL (Control Plane)

**User Story:** Como especialista de domínio, eu quero que regras inválidas ou potencialmente caras demais sejam rejeitadas antes da publicação, para preservar corretude e performance.

#### Critérios de Aceitação

1. THE `DSL_Compiler` SHALL executar análise semântica verificando tipos, campos existentes, escopo de variáveis e referências permitidas.
2. WHEN operandos de tipos incompatíveis forem detectados, THEN a compilação SHALL falhar com `POLICY_SEMANTIC_ERROR`.
3. WHEN um campo inexistente ou namespace proibido for referenciado, THEN a compilação SHALL falhar com `POLICY_SEMANTIC_ERROR`.
4. WHEN referência circular ou forma equivalente de dependência inválida for detectada, THEN a compilação SHALL falhar com erro estruturado apropriado.
5. THE Control_Plane SHALL executar análise de custo antes da geração final do bundle.
6. IF uma policy exceder o orçamento permitido de custo estático, profundidade, agregações ou tamanho, THEN a compilação SHALL falhar com `POLICY_COST_BUDGET_EXCEEDED`.

### Requisito 16: Golden Tests e gate de publicação (Control Plane)

**User Story:** Como especialista de domínio, eu quero validar bundles com testes determinísticos antes da ativação, para reduzir risco de regressão.

#### Critérios de Aceitação

1. THE Control_Plane SHALL suportar execução de `Golden_Tests` contra combinações específicas de bundle e snapshot.
2. EACH `Golden_Test` SHALL declarar input canônico, snapshot, bundle e veredito esperado.
3. WHEN o resultado divergir do esperado, THEN o sistema SHALL reportar falha contendo veredito esperado, veredito obtido e detalhes suficientes para diagnóstico.
4. THE Control_Plane SHALL suportar execução em batch de suítes de Golden Tests.
5. THE ativação de um novo manifesto em produção SHALL ser bloqueada enquanto houver Golden Tests obrigatórios falhando.
6. FOR ALL execuções repetidas com mesmos inputs, o resultado SHALL ser idêntico.

### Requisito 17: Resiliência, LKG e fail-closed (Data Plane)

**User Story:** Como engenheiro de plataforma, eu quero que o motor falhe de forma segura e previsível, para que transações nunca sejam aprovadas sem policy válida.

#### Critérios de Aceitação

1. IF não existir `Active_Policy_Set` válido disponível no cold start, THEN o Data_Plane SHALL rejeitar requisições com `HTTP 503` e código `POLICY_ENGINE_NOT_READY`.
2. IF o refresh falhar após ao menos uma inicialização válida, THEN o Data_Plane SHALL continuar usando o `Last_Known_Good`.
3. IF o bundle ou snapshot ativo não puder ser carregado ou validado, THEN a transação SHALL ser rejeitada com erro estruturado apropriado.
4. IF a integridade do bundle falhar, THEN o bundle SHALL ser rejeitado e um alerta operacional SHALL ser emitido.
5. IF ocorrer erro interno durante a avaliação, THEN a transação SHALL ser rejeitada com `POLICY_EVALUATION_ERROR`.
6. THE sistema SHALL ser fail-closed em todos os cenários onde a policy não puder ser avaliada com segurança.

### Requisito 18: Observabilidade, logging e métricas

**User Story:** Como engenheiro de plataforma, eu quero observar o comportamento do motor em produção, para detectar regressões, gargalos e falhas rapidamente.

#### Critérios de Aceitação

1. THE Validation_Engine SHALL emitir logs estruturados para avaliações aprovadas, rejeitadas e falhas internas.
2. Os logs SHALL conter no mínimo: `external_id`, `tenant_id`, `policy_scope_id`, `activation_id`, `artifact_hash`, `snapshot_version`, `final_verdict`, `evaluation_latency_ms` e `operation`.
3. THE sistema SHALL emitir métricas para: total de avaliações, aprovações, rejeições, falhas, tempo de refresh, tempo de avaliação e uso de `Last_Known_Good`.
4. IF uma avaliação exceder o orçamento de latência definido, THEN o sistema SHALL emitir warning com contexto suficiente para diagnóstico.
5. THE sistema SHALL emitir métricas e logs específicos para falhas de integridade, falhas de refresh, falhas de emissão de `Decision_Trail` e falhas de bootstrap.

### Requisito 19: Performance e overhead no Write Path

**User Story:** Como engenheiro de plataforma, eu quero que o motor introduza overhead baixo e previsível, para preservar o SLA do endpoint de escrita do ledger.

#### Critérios de Aceitação

1. THE avaliação do `Rule_Evaluator` SHALL completar em memória com latência inferior a 15ms no p95.
2. THE overhead total introduzido pelo Validation_Engine no endpoint de escrita SHALL ser inferior a 50ms no p99, considerando construção de contexto, obtenção do `Active_Policy_Set`, avaliação e geração de `Decision_Summary`.
3. THE steady state SHALL evitar I/O por request na fase de avaliação.
4. THE sistema SHALL medir separadamente: construção do contexto, lookup do `Active_Policy_Set`, avaliação, geração de summary e emissão de trail.
5. O orçamento de latência SHALL ser validado periodicamente por testes de performance automatizados.

### Requisito 20: Segurança, criptografia e integridade

**User Story:** Como engenheiro de segurança, eu quero que artefatos e trilhas sejam protegidos contra alteração e acesso indevido, para atender compliance e auditoria.

#### Critérios de Aceitação

1. THE sistema SHALL armazenar `Rule_Bundles` e `Reference_Snapshots` em buckets com Object Lock em modo GOVERNANCE e criptografia KMS.
2. THE pipeline de `Decision_Trail` SHALL usar criptografia KMS ponta a ponta onde aplicável.
3. THE Data_Plane SHALL validar integridade criptográfica de bundles antes de aceitá-los no runtime.
4. IF a verificação de integridade falhar, THEN o bundle SHALL ser rejeitado com erro estruturado e alerta operacional.
5. THE sistema SHALL usar IAM least privilege para AppConfig, S3, KMS, Firehose e demais serviços envolvidos.
6. THE acesso a ativação de policies SHALL ser auditável e restrito a identidades autorizadas.

### Requisito 21: Read Path para Decision Trails (CQRS)

**User Story:** Como auditor financeiro, eu quero consultar trilhas históricas de decisão, para analisar padrões e realizar investigações posteriores.

#### Critérios de Aceitação

1. THE `Decision_Trail` SHALL ser entregue ao armazenamento analítico via pipeline assíncrono.
2. THE armazenamento analítico SHALL suportar consulta por período, tenant, `policy_scope_id`, `artifact_hash`, `snapshot_version` e veredito.
3. THE trilha SHALL ser armazenada em formato colunar otimizado para analytics.
4. THE particionamento SHALL incluir pelo menos `year`, `month`, `day` e `tenant_id`.
5. THE pipeline SHALL enviar falhas de conversão ou entrega para área de erro dedicada para reprocessamento.
6. THE modelo analítico SHALL diferenciar claramente `Decision_Summary` persistido no ledger de `Decision_Trail` expandido no lake.

### Requisito 22: Infraestrutura como código (Terraform)

**User Story:** Como engenheiro de plataforma, eu quero provisionar toda a infraestrutura do motor via Terraform, para garantir reprodutibilidade e governança.

#### Critérios de Aceitação

1. THE Terraform SHALL provisionar buckets S3 para bundles, snapshots e erros com Object Lock, versionamento e KMS.
2. THE Terraform SHALL provisionar AppConfig para publicação dos `Policy_Activation_Manifests`.
3. THE Terraform SHALL provisionar o pipeline de ingestão de `Decision_Trail` com schemas, particionamento e criptografia.
4. THE Terraform SHALL provisionar alarmes e métricas mínimas para o runtime.
5. THE Terraform SHALL separar o state do Validation Engine do state do ledger.
6. THE Terraform SHALL usar backend remoto com locking habilitado e autenticação efêmera.
7. THE Terraform SHALL provisionar IAM least privilege para cada componente.

### Requisito 23: Gramática e semântica da Policy Rule DSL

**User Story:** Como especialista de domínio, eu quero uma DSL pequena, clara e restrita, para escrever policies sem risco de criar comportamento imprevisível.

#### Critérios de Aceitação

1. THE DSL SHALL suportar operadores de comparação sobre campos permitidos do `Canonical_Validation_Context`.
2. THE DSL SHALL suportar operadores lógicos `AND`, `OR` e `NOT`.
3. THE DSL SHALL suportar agregações sobre a coleção `postings`, incluindo no mínimo `SUM`, `COUNT`, `MIN`, `MAX`, `ANY` e `ALL`.
4. THE DSL SHALL suportar filtros e projeções explícitas sobre coleções.
5. THE DSL SHALL suportar acesso explícito aos namespaces `facts.*`, `policy_context.*` e `ref.*`.
6. THE DSL SHALL proibir acesso a relógio do sistema, aleatoriedade, rede, disco e qualquer API externa.
7. THE DSL SHALL suportar declaração de rules nomeadas com `effect` explícito (`ALLOW` ou `DENY`) e mensagem descritiva.
8. THE bundle SHALL registrar `Composition_Mode` explicitamente.
9. THE AST gerado SHALL usar nós tipados explícitos para coleção, agregação, acesso a campos, acesso a referência, comparação e composição lógica.

### Requisito 24: Versionamento, compatibilidade e rollback

**User Story:** Como engenheiro de plataforma, eu quero controlar versões de rules, snapshots e runtime, para publicar, validar e reverter mudanças com segurança.

#### Critérios de Aceitação

1. THE `Rule_Bundle` SHALL registrar metadados de compilação, incluindo autor, descrição, timestamp, `source_hash` e identificador lógico do conjunto de policies.
2. THE `Rule_Bundle` SHALL declarar compatibilidade com `context_schema_version`, `snapshot_schema_version` e `evaluator_min_version`.
3. THE publicação em produção SHALL exigir bundle e snapshot compatíveis com o runtime.
4. THE sistema SHALL suportar rollback por meio da publicação de manifesto apontando para versão anterior válida.
5. THE histórico de bundles, snapshots e manifestos SHALL ser mantido integralmente para auditoria.
6. THE ativação e rollback SHALL ser eventos auditáveis.

---

## Observações finais de modelagem

Este conjunto de requisitos assume explicitamente que:

* invariantes do ledger não migram para a DSL;
* a policy ativa é resolvida por manifesto atômico, não por hash solto;
* o hot path usa `Active_Policy_Set` em memória;
* o comando não é mutado pelo validador;
* o que é persistido atomicamente é o `Decision_Summary`;
* o `Decision_Trail` é trilha expandida e assíncrona;
* replay depende de contexto persistido e artefatos versionados, não apenas de hash.

Se quiser, eu posso fazer o próximo passo e converter este documento em uma matriz completa de rastreabilidade `Requirement -> Design -> Property -> Test`.
