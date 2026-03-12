"""
Camada de domínio do Validation Engine.

Contém os conceitos centrais do motor de validação:
- policy_ast.py: AST/IR tipado da DSL de policies
- models.py: RuleBundle, ReferenceSnapshot, DecisionSummary, DecisionTrail, etc.
- context.py: CanonicalValidationContext e DerivedFacts
- errors.py: hierarquia de erros do motor
- evaluator.py: RuleEvaluator — avaliador puro e determinístico
- compiler.py: DSLCompiler e SemanticAnalyzer
- cost_analyzer.py: análise estática de custo de bundles
"""
