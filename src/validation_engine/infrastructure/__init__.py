"""
Camada de infraestrutura do Validation Engine.

Adaptadores para serviços externos:
- bundle_store.py: armazenamento de RuleBundle em S3 WORM
- snapshot_store.py: armazenamento de ReferenceSnapshot em S3 WORM
- bundle_loader.py: carregamento de bundle com cache e verificação de integridade
- snapshot_loader.py: carregamento de snapshot com cache e verificação de schema
- manifest_resolver.py: resolução de manifesto ativo via AppConfig Agent
- decision_trail_emitter.py: emissão best-effort de DecisionTrail ao Firehose
- lkg_store.py: persistência do Last Known Good em /tmp ou diretório configurável
"""
