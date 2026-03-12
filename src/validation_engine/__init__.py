"""
Bounded context: Validation Engine.

Responsável exclusivamente por regras configuráveis de policy para o subledger.
Não substitui as validações estruturais e invariantes do ledger — estas permanecem
no bounded context do ledger, hardcoded e protegidas pelo modelo de domínio.

O motor permite que especialistas de domínio definam policies declarativas via DSL
restrita. Essas policies são compiladas offline em artefatos imutáveis (RuleBundle),
armazenadas em S3 com Object Lock (WORM), ativadas por manifesto versionado via
AppConfig e avaliadas deterministicamente no write path do subledger.

Camadas:
- domain/: AST, modelos, contexto canônico, evaluator, compiler, erros
- application/: facade, runtime registry, context builder, publisher
- infrastructure/: bundle/snapshot store/loader, manifest resolver, trail emitter, LKG
"""
