"""
Camada de aplicação do Validation Engine.

Orquestra os componentes do domínio e expõe a interface para o ledger:
- facade.py: PolicyValidationFacade — ponto de entrada do Data Plane
- runtime_registry.py: PolicyRuntimeRegistry — cache de ActivePolicySets
- context_builder.py: CanonicalValidationContextBuilder — normalização do comando
- publisher.py: PolicyPublisher — publicação de manifestos no Control Plane
"""
